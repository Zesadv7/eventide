"""CLI parsing and no-key help behavior."""

import pytest

from nexus_agent.cli import build_parser, main


def test_cli_subcommands_parse():
    parser = build_parser()
    assert parser.parse_args(["chat"]).command == "chat"
    run = parser.parse_args(["run", "hello", "--json"])
    assert run.prompt == "hello" and run.json
    serve = parser.parse_args(["serve", "--port", "9000"])
    assert serve.port == 9000
    evaluate = parser.parse_args(["eval", "evals/smoke.yaml", "--live"])
    assert evaluate.live


def test_help_needs_no_api_key(capsys):
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    assert "nexus-agent" in capsys.readouterr().out
