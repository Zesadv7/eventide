"""ContextBuilder input limits."""

from eventide.context_builder import ContextBuilder


def test_instructions_are_capped_at_the_documented_limit(isolated_workspace):
    (isolated_workspace / "AGENTS.md").write_text("x" * 5000, encoding="utf-8")
    assert len(ContextBuilder.instructions(isolated_workspace)) == 4000


def test_missing_instructions_are_empty(isolated_workspace):
    assert ContextBuilder.instructions(isolated_workspace) == ""
