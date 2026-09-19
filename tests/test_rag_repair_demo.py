"""Exercise the reproducible repair story using real tools and subprocess tests."""

from examples.rag_repair import run_demo


async def test_repair_demo_uses_evidence_and_recovers_persisted_session():
    report = await run_demo()
    assert report["passed"], report
    assert report["before_exit_code"] != 0
    assert report["after_exit_code"] == 0
    assert report["regression_smoke"] is True
    assert report["tool_sequence"] == [
        "search_knowledge",
        "read_file",
        "edit_file",
        "read_file",
        "bash",
    ]
