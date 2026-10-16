"""Retrieval previews retain citations and announce bounded source content."""

import pytest

from eventide.rag.index import IndexedChunk
from eventide.rag.search import SearchResult, format_results


def result(index, text):
    return SearchResult(IndexedChunk(index, f"source-{index}.py", 4, 9, "", text),
                        0.1, ("sparse",))


def test_preview_cap_and_explicit_truncation_keep_source_citations():
    rendered = format_results([result(i, "X" * 1200) for i in range(8)], mode="sparse")
    assert "Found 5 passages" in rendered
    assert rendered.count("X") == 4000
    assert rendered.count("Snippet truncated: 800/1200") == 5
    assert "source-4.py:4-9" in rendered and "source-5.py" not in rendered
    assert "Omitted 3 additional passages" in rendered
    assert "use read_file" in rendered


def test_small_total_source_budget_does_not_drop_references_or_change_scores():
    rendered = format_results([result(1, "A" * 80), result(2, "B" * 80)],
                              mode="dense+sparse", source_budget=100)
    assert rendered.count("A") == 80 and rendered.count("B") == 20
    assert "source-2.py:4-9" in rendered and "Snippet truncated: 20/80" in rendered
    assert rendered.count("score 0.1000") == 2


def test_exact_small_passage_is_not_marked_incomplete():
    rendered = format_results([result(1, "exact line")], mode="sparse")
    assert "exact line" in rendered and "truncated" not in rendered
    assert "Omitted" not in rendered


@pytest.mark.parametrize("value", [-1, 4001, True, 1.5])
def test_invalid_source_budget_is_not_silently_coerced(value):
    with pytest.raises(ValueError, match="source_budget"):
        format_results([], mode="sparse", source_budget=value)
