"""Main agent loop: LLM + tool_use dispatch + context management."""

import threading

from eventide.config import (
    CONTEXT_LIMIT,
    CONTINUATION_PROMPT,
    DEFAULT_MAX_TOKENS,
    ESCALATED_MAX_TOKENS,
    client,
)
from eventide.context import (
    compact_history,
    estimate_size,
    micro_compact,
    reactive_compact,
    snip_compact,
    tool_result_budget,
)
from eventide.hooks import trigger_hooks
from eventide.llm import RecoveryState, is_prompt_too_long_error, with_retry
from eventide.mcp.client import assemble_tool_pool, mcp_clients
from eventide.memory.context_memory import update_context
from eventide.scheduling.background import (
    collect_background_results,
    should_run_background,
    start_background_task,
)
from eventide.scheduling.cron import consume_cron_queue
from eventide.tools.dispatch import call_tool_handler
from eventide.utils import has_tool_use

rounds_since_todo = 0
agent_lock = threading.Lock()


def assemble_system_prompt(context: dict) -> str:
    """Build the system prompt from live context each turn."""
    from datetime import datetime

    from eventide.memory.skills import list_skills

    sections = [
        "You are a coding agent. Act, don't explain.",
        "Available tools: bash, read_file, write_file, edit_file, glob, "
        "todo_write, task, load_skill, compact, "
        "create_task, list_tasks, get_task, claim_task, complete_task, "
        "schedule_cron, list_crons, cancel_cron, "
        "spawn_teammate, send_message, check_inbox, "
        "request_shutdown, request_plan, review_plan, "
        "create_worktree, remove_worktree, keep_worktree, "
        "connect_mcp. MCP tools are prefixed mcp__{server}__{tool}.",
        f"Working directory: {context.get('workdir', '')}",
        f"Current time: {datetime.now().isoformat(timespec='seconds')}",
        "Skills catalog:\n" + list_skills() + "\nUse load_skill(name) when a skill is relevant.",
    ]
    if context.get("memories"):
        sections.append(f"Relevant memories:\n{context['memories']}")
    mcp_names = list(mcp_clients.keys())
    if mcp_names:
        sections.append(f"Connected MCP servers: {', '.join(mcp_names)}")
    return "\n\n".join(sections)


def prepare_context(messages: list) -> list:
    """Run the full pre-LLM compaction pipeline in-place."""
    messages[:] = tool_result_budget(messages)
    messages[:] = snip_compact(messages)
    messages[:] = micro_compact(messages)
    if estimate_size(messages) > CONTEXT_LIMIT:
        messages[:] = compact_history(messages)
    return messages


def build_user_content(results: list[dict]) -> list[dict]:
    """Combine tool results and background notifications into user content."""
    content = list(results)
    for note in collect_background_results():
        content.append({"type": "text", "text": note})
    return content


def inject_background_notifications(messages: list) -> None:
    """Append any completed background task notifications to messages."""
    notes = collect_background_results()
    if notes:
        messages.append(
            {"role": "user", "content": [{"type": "text", "text": note} for note in notes]}
        )


def call_llm(messages: list, context: dict, tools: list, state: RecoveryState, max_tokens: int):
    """Call the LLM through the recovery wrapper."""
    system = assemble_system_prompt(context)
    return with_retry(
        lambda: client.messages.create(
            model=state.current_model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
        ),
        state,
    )


def agent_loop(messages: list, context: dict) -> None:
    """The core agent loop: model, tool_use, result, repeat."""
    global rounds_since_todo
    tools, handlers = assemble_tool_pool()
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    while True:
        fired = consume_cron_queue()
        for job in fired:
            messages.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})
            print(f"  \033[35m[cron inject] {job.prompt[:60]}\033[0m")

        inject_background_notifications(messages)

        if rounds_since_todo >= 3:
            messages.append({"role": "user", "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        prepare_context(messages)
        context = update_context(context, messages)
        tools, handlers = assemble_tool_pool()

        try:
            response = call_llm(messages, context, tools, state, max_tokens)
        except Exception as exc:
            if is_prompt_too_long_error(exc) and not state.has_attempted_reactive_compact:
                messages[:] = reactive_compact(messages)
                state.has_attempted_reactive_compact = True
                continue
            messages.append(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"[Error] {type(exc).__name__}: {exc}"}],
                }
            )
            return

        if response.stop_reason == "max_tokens":
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] retry with {max_tokens}\033[0m")
                continue
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < 2:  # MAX_RECOVERY_RETRIES
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                continue
            return

        max_tokens = DEFAULT_MAX_TOKENS
        state.has_escalated = False
        messages.append({"role": "assistant", "content": response.content})
        if not has_tool_use(response.content):
            trigger_hooks("Stop", messages)
            return

        results = []
        compacted_now = False
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")

            if block.name == "compact":
                messages[:] = compact_history(messages)
                messages.append(
                    {"role": "user", "content": "[Compacted. Continue with summarized context.]"}
                )
                compacted_now = True
                break

            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": str(blocked)}
                )
                continue

            if should_run_background(block.name, block.input):
                bg_id = start_background_task(block, handlers)
                output = (
                    f"[Background task {bg_id} started] Result will arrive as a task_notification."
                )
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                continue

            handler = handlers.get(block.name)
            output = call_tool_handler(handler, block.input, block.name)
            trigger_hooks("PostToolUse", block, output)
            print(str(output)[:300])

            if block.name == "todo_write":
                rounds_since_todo = 0
            else:
                rounds_since_todo += 1

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        if compacted_now:
            continue

        messages.append({"role": "user", "content": build_user_content(results)})
