"""Interactive CLI entry point for Nexus Agent."""

import threading
import time

from nexus_agent.agent import agent_loop
from nexus_agent.config import CLI_ACTIVE, PROMPT, WORKDIR
from nexus_agent.hooks import trigger_hooks
from nexus_agent.memory.context_memory import update_context
from nexus_agent.scheduling.cron import start_cron_scheduler, consume_cron_queue
from nexus_agent.teams.protocol import consume_lead_inbox
from nexus_agent.utils import extract_text, has_tool_use


def _block_type(block):
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def print_turn_assistants(messages: list, turn_start: int) -> None:
    """Print assistant text blocks produced since turn_start."""
    for msg in messages[turn_start:]:
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []):
            if _block_type(block) == "text":
                text = block["text"] if isinstance(block, dict) else block.text
                print(text)


def _inbox_label(msg: dict) -> str:
    req_id = msg.get("metadata", {}).get("request_id", "")
    return f"{msg.get('type', 'message')}{f' req:{req_id}' if req_id else ''}"


def _maybe_inject_inbox(history: list) -> None:
    inbox = consume_lead_inbox(route_protocol=True)
    if inbox:
        inbox_text = "\n".join(
            f"From {m['from']} [{_inbox_label(m)}]: {m['content'][:200]}"
            for m in inbox
        )
        history.append({"role": "user", "content": f"[Inbox]\n{inbox_text}"})


def cron_autorun_loop(history: list, context: dict) -> None:
    """Daemon thread that runs the agent on scheduled cron jobs."""
    from nexus_agent.agent import agent_lock
    while True:
        time.sleep(1)
        fired = consume_cron_queue()
        if not fired:
            continue
        with agent_lock:
            turn_start = len(history)
            for job in fired:
                history.append({"role": "user",
                                "content": f"[Scheduled] {job.prompt}"})
                print(f"  \033[35m[cron auto] {job.prompt[:60]}\033[0m")
            agent_loop(history, context)
            context.update(update_context(context, history))
            print_turn_assistants(history, turn_start)


def main() -> None:
    """Run the interactive Nexus Agent CLI."""
    global CLI_ACTIVE
    CLI_ACTIVE = True
    print("Nexus Agent")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    history: list = []
    context = update_context({"workdir": str(WORKDIR)}, history)

    start_cron_scheduler()
    threading.Thread(target=cron_autorun_loop,
                     args=(history, context), daemon=True).start()

    while True:
        try:
            query = input(PROMPT)
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        trigger_hooks("UserPromptSubmit", query)
        turn_start = len(history)
        history.append({"role": "user", "content": query})

        from nexus_agent.agent import agent_lock
        with agent_lock:
            agent_loop(history, context)
            context = update_context(context, history)
            print_turn_assistants(history, turn_start)

        _maybe_inject_inbox(history)
        print()
