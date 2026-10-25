"""ContextBuilder input limits (MK-04 context slice: truncation is visible)."""

import pytest

from eventide.context_builder import ContextBuilder


def test_instructions_are_capped_with_a_visible_truncation_notice(isolated_workspace):
    (isolated_workspace / "AGENTS.md").write_text("x" * 5000, encoding="utf-8")
    record = ContextBuilder.instructions_record(isolated_workspace)
    assert record.text == "x" * 4000
    assert record.truncated is True
    assert record.loaded_characters == 4000
    assert record.file_bytes == 5000
    assert record.limit == 4000
    text = ContextBuilder.instructions(isolated_workspace)
    assert text == record.visible_text()
    assert "AGENTS.md" in text and "4000" in text and "read_file" in text
    assert record.visible_text().endswith("x" * 100)


def test_complete_instructions_name_their_source(isolated_workspace):
    (isolated_workspace / "AGENTS.md").write_text("rule one\n", encoding="utf-8")
    text = ContextBuilder.instructions(isolated_workspace)
    assert "rule one\n" in text
    assert "complete" in text and "AGENTS.md" in text
    record = ContextBuilder.instructions_record(isolated_workspace)
    assert record.truncated is False
    assert record.loaded_characters == len("rule one\n")


def test_missing_instructions_are_empty(isolated_workspace):
    assert ContextBuilder.instructions(isolated_workspace) == ""
    record = ContextBuilder.instructions_record(isolated_workspace)
    assert record.truncated is False and record.loaded_characters == 0


def test_instructions_outside_the_workspace_are_rejected(isolated_workspace):
    outside = isolated_workspace.parent / "outside-agents.md"
    outside.write_text("escaped", encoding="utf-8")
    link = isolated_workspace / "AGENTS.md"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes workspace"):
        ContextBuilder.instructions(isolated_workspace)
