"""Workspace-scoped production skill loading."""

from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.skills import DESCRIPTION_LIMIT, SkillCatalog
from tests.test_runtime import settings_for


def add_skill(root, directory="review", *, name="code-review", description="Review code"):
    target = root / "skills" / directory
    target.mkdir(parents=True)
    content = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        "# Review instructions\n\nCheck correctness and tests.\n"
    )
    (target / "SKILL.md").write_text(content, encoding="utf-8")
    return content


def test_catalog_discovers_metadata_and_loads_a_snapshot(isolated_workspace):
    content = add_skill(isolated_workspace)
    catalog = SkillCatalog.discover(isolated_workspace)

    assert [skill.name for skill in catalog.skills] == ["code-review"]
    assert catalog.skills[0].source == "skills/review/SKILL.md"
    assert "- code-review: Review code" in catalog.prompt()
    assert catalog.tool()["input_schema"]["properties"]["name"]["enum"] == ["code-review"]
    assert catalog.load("code-review") == content

    (isolated_workspace / "skills" / "review" / "SKILL.md").write_text(
        "changed", encoding="utf-8"
    )
    assert catalog.load("code-review") == content
    assert SkillCatalog.discover(isolated_workspace).catalog_hash != catalog.catalog_hash


def test_catalog_is_optional_and_bounds_prompt_metadata(isolated_workspace):
    assert not SkillCatalog.discover(isolated_workspace)
    add_skill(isolated_workspace, description="word " * DESCRIPTION_LIMIT)
    catalog = SkillCatalog.discover(isolated_workspace)
    assert len(catalog.skills[0].description) == DESCRIPTION_LIMIT
    assert catalog.skills[0].description.endswith("…")
    assert catalog.load("missing").startswith("Error: Skill not found")


async def test_runtime_exposes_and_audits_workspace_skill_loading(isolated_workspace):
    content = add_skill(isolated_workspace)
    provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {
                        "id": "skill-call",
                        "name": "load_skill",
                        "arguments": {"name": "code-review"},
                    }
                ]
            },
            {"text": "review complete"},
        ]
    )
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("review this workspace"))
        assert result.status == "completed"
        assert "code-review: Review code" in provider.requests[0].system
        assert "Check correctness and tests" not in provider.requests[0].system
        assert {tool["name"] for tool in provider.requests[0].tools} == {
            "bash",
            "read_file",
            "write_file",
            "edit_file",
            "glob",
            "compact",
            "read_tool_result",
            "load_skill",
        }
        assert provider.requests[1].messages[-1]["content"][0]["content"] == content

        events = host.store.run_events(result.run_id)
        configured = next(event for event in events if event["type"] == "context.configured")
        assert configured["payload"]["skill_count"] == 1
        assert configured["payload"]["skill_catalog_hash"]
        prepared = next(event for event in events if event["type"] == "tool.prepared")
        assert prepared["payload"]["name"] == "load_skill"
        assert prepared["payload"]["readonly"] is True
        completed = next(event for event in events if event["type"] == "tool.completed")
        assert completed["payload"]["content"] == content
        assert completed["payload"]["is_error"] is False
    finally:
        await host.close()


async def test_runtime_does_not_advertise_skill_tool_without_catalog(isolated_workspace):
    provider = ScriptedProvider([{"text": "done"}])
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("hello"))
        assert result.status == "completed"
        assert "load_skill" not in {tool["name"] for tool in provider.requests[0].tools}
        configured = next(
            event
            for event in host.store.run_events(result.run_id)
            if event["type"] == "context.configured"
        )
        assert configured["payload"]["skill_count"] == 0
    finally:
        await host.close()
