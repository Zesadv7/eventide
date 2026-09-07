# Nexus Agent — Development Plan

## Goal

Turn Nexus Agent into a resume-worthy, demonstrable Agent Harness project.
The core harness is already complete and tested. The next phase is polish,
extension, and packaging for GitHub.

## Phase 1: Hardening (Priority: High)

| Task | Why | Files likely touched |
|---|---|---|
| Move test temp files to isolated dirs | Avoid polluting project root | `tests/test_tools.py`, `tests/conftest.py` |
| Add CI via GitHub Actions | Resume credibility | `.github/workflows/ci.yml` |
| Pin dependency versions | Reproducibility | `requirements.txt`, `pyproject.toml` |
| Add CLI `--help` and non-interactive mode | Easier demos | `nexus_agent/cli.py` |
| Improve error messages when API key is missing | Better UX | `nexus_agent/config.py` |

## Phase 2: Resume Polish (Priority: High)

| Task | Why | Files likely touched |
|---|---|---|
| Add architecture diagram | README visual appeal | `docs/architecture.md`, `README.md` |
| Record a short demo GIF/asciinema | Shows it actually runs | `README.md` |
| Write a "Why this project" section | Interview storytelling | `README.md` |
| Add GitHub issue templates | Open-source polish | `.github/ISSUE_TEMPLATE/` |
| Add a `CONTRIBUTING.md` | Looks professional | `CONTRIBUTING.md` |

## Phase 3: Feature Extensions (Priority: Medium)

| Task | Why | Files likely touched |
|---|---|---|
| Real MCP server support | Move beyond mocks | `nexus_agent/mcp/client.py` |
| SQLite / vector memory backend | More robust than MEMORY.md | `nexus_agent/memory/` |
| Web UI (Gradio/Streamlit) | Easier demo | new `web/` or `ui/` package |
| Support multiple model providers | Shows provider-agnostic design | `nexus_agent/llm.py` |
| Persistent transcript replay | Debugging and transparency | `nexus_agent/context.py` |

## Phase 4: Domain Pivot (Optional)

Pick one concrete domain to prove the harness generalizes beyond coding:

- **Research Assistant**: PDF reading, web search, citation tracking.
- **Data Analyst**: CSV/Excel tools, matplotlib charts, SQL queries.
- **DevOps Agent**: Docker, kubectl, Terraform wrappers.

This would involve adding new tools in `nexus_agent/tools/` and domain skills in `skills/`.

## Suggested Next Immediate Step

1. Add `.github/workflows/ci.yml` running `python -m pytest -q` on Python 3.10/3.11/3.12.
2. Clean up `tests/test_tools.py` to use temporary directories.
3. Push to GitHub.

## Decision Log

- **Project name**: `nexus-agent` (codename; can be renamed later based on final features).
- **License**: MIT, with shareAI Lab attribution preserved.
- **Scope**: Keep it a harness, not a workflow orchestrator. The model decides; the harness executes.
