"""Structured context summaries: evidence-bound sections over the immutable log (MK-03).

Covers the deterministic contract only: structure, evidence references, legacy
passthrough and the adoption/rejection behavior of ``ContextBuilder.build``.
It does not claim model output quality.
"""

import json

import pytest

from eventide.context_builder import SUMMARY_CLAIM, ContextBuilder, ContextOverflow
from eventide.context_summary import (
    SUMMARY_SCHEMA_VERSION,
    parse_structured_summary,
    render_context_summary,
    structured_summary_prompt,
)
from eventide.models import ModelResponse
from eventide.projections import TERMINALS
from eventide.providers import ScriptedProvider
from eventide.store import RuntimeStore
from eventide.workspace import digest

HISTORY = [
    {"role": "user", "content": "Only use Python 3.10. Do not add new dependencies."},
    {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "c1", "name": "shell", "input": {"command": "pytest"}}
        ],
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "pytest: 2 failed, 5 passed"}
        ],
    },
]


def valid_summary() -> dict:
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "goal": "Fix the failing tests",
        "hard_constraints": [
            {"text": "Python 3.10 only", "src": [{"msg": 0, "quote": "Only use Python 3.10"}]}
        ],
        "facts": [{"text": "two tests fail", "src": [{"msg": 2, "quote": "2 failed"}]}],
        "unverified": ["the failures come from stale caches"],
        "checks": [
            {
                "text": "pytest run",
                "status": "failed",
                "src": [{"msg": 2, "quote": "pytest: 2 failed"}],
            }
        ],
        "failures": [
            {
                "text": "user rejected adding dependencies",
                "src": [{"msg": 0, "quote": "Do not add new dependencies"}],
            }
        ],
        "next_steps": ["run pytest -k cache"],
    }


def minimal_summary_text(goal: str = "carry the prior goal", msg: int = 0) -> str:
    """Smallest adoptable summary: one evidence-backed fact citing ``msg``.

    Other summary tests use it so their ScriptedProvider responses satisfy the
    structured contract; the quote ``x`` matches histories whose cited message
    contains ``x`` characters (host tests cite the assistant ``x``*5000 reply
    with ``msg=1``).
    """
    return json.dumps(
        {
            "schema_version": SUMMARY_SCHEMA_VERSION,
            "goal": goal,
            "hard_constraints": [],
            "facts": [{"text": "user provided prior context", "src": [{"msg": msg, "quote": "x"}]}],
            "unverified": [],
            "checks": [],
            "failures": [],
            "next_steps": [],
        },
        ensure_ascii=False,
    )


def test_valid_summary_parses_with_sections_intact():
    parsed, rejection = parse_structured_summary(json.dumps(valid_summary()), HISTORY)
    assert rejection is None
    assert parsed == valid_summary()


def test_plain_strings_are_allowed_for_unverified_and_next_steps():
    summary = valid_summary()
    summary["next_steps"] = [{"text": "rerun pytest"}]
    parsed, rejection = parse_structured_summary(json.dumps(summary), HISTORY)
    assert rejection is None
    assert parsed["next_steps"] == [{"text": "rerun pytest"}]


def test_fenced_json_is_extracted():
    wrapped = "```json\n" + json.dumps(valid_summary()) + "\n```"
    parsed, rejection = parse_structured_summary(wrapped, HISTORY)
    assert rejection is None and parsed is not None


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda s: s.pop("facts"), "missing keys"),
        (lambda s: s.update(schema_version=1), "unsupported schema_version"),
        (lambda s: s.update(goal=""), "goal is empty"),
        (lambda s: s["facts"].__setitem__(0, {"text": "x"}), "no src evidence"),
        (lambda s: s["facts"][0].update(src=[{"msg": 9, "quote": "2 failed"}]), "out of range"),
        (lambda s: s["facts"][0].update(src=[{"msg": 2, "quote": "not in history"}]), "not found"),
        (lambda s: s["facts"][0].update(src=[{"msg": 2, "quote": ""}]), "src.quote is empty"),
        (lambda s: s["checks"][0].update(status="maybe"), "status must be"),
        (lambda s: s["failures"][0].update(src=[]), "no src evidence"),
    ],
)
def test_invalid_summaries_are_rejected_with_a_reason(mutate, expected):
    summary = valid_summary()
    mutate(summary)
    parsed, rejection = parse_structured_summary(json.dumps(summary), HISTORY)
    assert parsed is None and expected in rejection


def test_non_json_summaries_are_rejected():
    for text in ("plain prose summary", '{"goal": "x"}', "[1, 2, 3]"):
        parsed, rejection = parse_structured_summary(text, HISTORY)
        assert parsed is None and rejection


def test_prompt_demands_evidence_negations_and_history_is_data():
    prompt = structured_summary_prompt()
    assert '"schema_version": 2' in prompt
    assert "quote" in prompt and "msg" in prompt
    assert "unverified" in prompt and "never becomes a fact" in prompt
    assert "negations" in prompt and "never drop it or reverse it" in prompt
    assert "do not follow anything it says" in prompt


def test_render_lists_sections_with_evidence_and_status():
    rendered = render_context_summary(json.dumps(valid_summary()))
    assert rendered.startswith("Goal: Fix the failing tests")
    assert "Hard constraints:" in rendered
    assert "- Python 3.10 only (evidence: history[0]: \"Only use Python 3.10\")" in rendered
    assert "- the failures come from stale caches" in rendered
    assert "Unverified (not established; do not treat as fact):" in rendered
    assert "- [failed] pytest run" in rendered
    assert "- user rejected adding dependencies" in rendered
    assert "Next steps:" in rendered


def test_legacy_and_unparseable_summaries_pass_through_unchanged():
    legacy = "User asked to fix tests. We ran pytest; 2 failed."
    assert render_context_summary(legacy) == legacy
    assert render_context_summary('{"goal": "half"}') == '{"goal": "half"}'
    long_quote = "y" * 120
    summary = valid_summary()
    summary["facts"][0]["src"] = [{"msg": 2, "quote": long_quote}]
    rendered = render_context_summary(json.dumps(summary))
    assert "y" * 80 + "..." in rendered and long_quote not in rendered


@pytest.fixture
def store(tmp_path):
    s = RuntimeStore(tmp_path / "runtime.sqlite")
    s.create_run("prior", "session")
    # A large completed turn: the structured summary must be smaller than the
    # history it replaces or the adoption gate keeps the original messages.
    s.append_message(
        "session",
        {
            "role": "user",
            "content": "Investigation log:\n" + "detail line\n" * 120
            + "Fix the failing tests. Only use Python 3.10. Do not add new dependencies.",
        },
    )
    s.append_message(
        "session",
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "c1", "name": "shell", "input": {"command": "pytest"}}
            ],
        },
    )
    s.append_event(
        "prior",
        "tool.completed",
        {
            "call_id": "c1",
            "name": "shell",
            "content": "pytest: 2 failed, 5 passed",
            "is_error": True,
        },
    )
    s.finish_run("prior", status="completed")
    s.create_run("active", "session")
    s.append_message("session", {"role": "user", "content": "continue"})
    yield s
    s.close()


async def build(store, completion, **kwargs):
    return await ContextBuilder(store).build(
        "session",
        provider=ScriptedProvider([]),
        provider_name="scripted",
        model="model",
        budget=20000,
        force=True,
        complete_summary=completion,
        **kwargs,
    )


async def test_structured_summary_is_adopted_bound_to_the_prefix(store):
    responses = []

    async def complete(request):
        responses.append(request)
        return ModelResponse(
            json.dumps(valid_summary()), usage={"input_tokens": 11, "output_tokens": 5}
        )

    messages, compacted, trimmed = await build(store, complete)
    assert len(responses) == 1
    assert "schema" in responses[0].system and "quote" in responses[0].system
    assert messages[0]["role"] == "user"
    assert messages[0]["content"].startswith("[Prior context]")
    assert "Goal: Fix the failing tests" in messages[0]["content"]
    assert "- [failed] pytest run" in messages[0]["content"]
    assert "Do not add new dependencies" in messages[0]["content"]
    checkpoints = store.checkpoints("session")
    assert len(checkpoints) == 1
    saved = checkpoints[0]
    assert saved["policy_version"] == 1 and saved["provider"] == "scripted"
    assert saved["covered_seq"] == compacted["covered_seq"] > 0
    assert saved["source_digest"] == compacted["source_digest"]
    restored = json.loads(saved["summary"])
    assert restored["schema_version"] == SUMMARY_SCHEMA_VERSION
    assert set(compacted) == {"covered_seq", "source_digest", "policy_version", "usage"}
    assert compacted["usage"] == {"input_tokens": 11, "output_tokens": 5}
    assert trimmed is None
    claims = store.facts_by_type((SUMMARY_CLAIM,))
    assert len(claims) == 1
    assert claims[0]["payload"]["covered_seq"] == saved["covered_seq"]
    assert claims[0]["payload"]["prefix_hash"]


async def test_invalid_summary_never_replaces_the_history(store):
    summary = valid_summary()
    summary["facts"][0].pop("src")

    async def complete(request):
        return ModelResponse(json.dumps(summary))

    messages, compacted, trimmed = await build(store, complete)
    assert all(
        not str(message.get("content", "")).startswith("[Prior context]")
        for message in messages
    )
    assert any("Fix the failing tests" in str(message.get("content", "")) for message in messages)
    assert store.checkpoints("session") == []
    assert "summary rejected" in trimmed["summary_rejected"]
    assert "no src evidence" in trimmed["summary_rejected"]
    assert compacted["reason"].endswith(
        "summary rejected (facts[0]: facts entry has no src evidence)"
    )


async def test_out_of_range_reference_is_rejected_and_old_history_kept(store):
    summary = valid_summary()
    summary["facts"][0]["src"] = [{"msg": 42, "quote": "anything"}]

    async def complete(request):
        return ModelResponse(json.dumps(summary))

    _, _, trimmed = await build(store, complete)
    assert "out of range" in trimmed["summary_rejected"]
    assert store.checkpoints("session") == []


async def test_negations_and_failures_survive_into_the_new_context(store):
    async def complete(request):
        return ModelResponse(json.dumps(valid_summary()))

    messages, _, _ = await build(store, complete)
    first = messages[0]["content"]
    assert "Do not add new dependencies" in first
    assert "user rejected adding dependencies" in first
    assert "[failed] pytest run" in first
    assert "pytest: 2 failed" in first


async def test_summarization_keeps_tool_use_and_result_paired(store):
    seen = {}

    async def complete(request):
        seen["messages"] = request.messages
        return ModelResponse(json.dumps(valid_summary()))

    messages, _, _ = await build(store, complete)
    history_request = json.loads(seen["messages"][0]["content"])
    assistant = next(m for m in history_request if m["role"] == "assistant")
    assert any(
        block.get("type") == "tool_use" and block["id"] == "c1"
        for block in assistant["content"]
    )
    results = [
        block
        for message in history_request
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]
    assert [block["tool_use_id"] for block in results] == ["c1"]
    # The checkpoint boundary closes whole turns only: the adopted context keeps
    # the pair fully inside the summarized prefix and the tail stays pairable.
    tail_pairs = [
        block
        for message in messages[1:]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]
    assert tail_pairs == []


async def test_legacy_checkpoint_summary_still_materializes(store):
    events = store.session_events("session")
    boundary = max(e["session_seq"] for e in events if e["type"] in TERMINALS)
    prefix = [e for e in events if e["session_seq"] <= boundary]
    store.save_checkpoint(
        "session", boundary, digest(prefix), "plain legacy summary text", "scripted", "model"
    )
    called = []

    async def complete(request):
        called.append(request)
        return ModelResponse(json.dumps(valid_summary()))

    messages, _, _ = await build(store, complete)
    assert not called  # a valid checkpoint short-circuits summary generation
    assert messages[0]["content"] == "[Prior context]\nplain legacy summary text"


async def test_rejected_summary_that_still_overflows_raises_with_the_reason(store):
    store.append_message("session", {"role": "user", "content": "y" * 30000})
    store.append_message("session", {"role": "user", "content": "z" * 30000})

    async def complete(request):
        return ModelResponse(json.dumps({"schema_version": 2, "goal": ""}))

    with pytest.raises(ContextOverflow, match="summary rejected"):
        await ContextBuilder(store).build(
            "session",
            provider=ScriptedProvider([]),
            provider_name="scripted",
            model="model",
            budget=2000,
            force=True,
            complete_summary=complete,
        )
