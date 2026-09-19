"""End-to-end sparse RAG CLI behavior."""

import subprocess
from pathlib import Path

from eventide.cli import build_parser, main


def _workspace(root: Path) -> Path:
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / ".git" / "info" / "exclude").write_text(".eventide/\n", encoding="utf-8")
    (root / "knowledge.txt").write_text("deployment uses blue-green rollout", encoding="utf-8")
    return root


def test_rag_arguments_parse() -> None:
    parser = build_parser()
    build = parser.parse_args(["rag", "build", "--workspace", ".", "--force"])
    assert build.rag_action == "build" and build.force
    search = parser.parse_args(["rag", "search", "rollout", "-k", "3"])
    assert search.rag_action == "search" and search.k == 3


def test_rag_build_and_search_cli(isolated_workspace: Path, capsys) -> None:
    root = _workspace(isolated_workspace)

    assert main(["rag", "build", "--workspace", str(root)]) == 0
    assert "1 files / 1 chunks" in capsys.readouterr().out
    assert main(["rag", "search", "blue-green", "--workspace", str(root)]) == 0
    output = capsys.readouterr().out
    assert "mode: dense" in output
    assert "knowledge.txt:1-1" in output


def test_rag_cli_validates_k(isolated_workspace: Path, capsys) -> None:
    root = _workspace(isolated_workspace)
    assert main(["rag", "build", "--workspace", str(root)]) == 0
    capsys.readouterr()
    assert main(["rag", "search", "rollout", "--workspace", str(root), "-k", "11"]) == 1
    assert "k must be between 1 and 10" in capsys.readouterr().err
