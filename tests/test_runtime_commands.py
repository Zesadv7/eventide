"""Real command exit status survives the complete production execution path."""

import sys

from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from tests.test_runtime import settings_for


async def test_command_failure_is_in_model_context_and_canonical_event(isolated_workspace):
    provider = ScriptedProvider([
        {"tool_calls": [{
            "id": "failed-test", "name": "bash",
            "arguments": {"command": f'"{sys.executable}" -c "import sys; sys.exit(7)"'},
        }]},
        {"text": "The command failed."},
    ])
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("run the failing check"))
        event = next(e for e in host.store.session_events(result.session_id)
                     if e["type"] == "tool.completed")
        assert event["payload"]["is_error"] is True
        metadata = event["payload"]["metadata"]
        assert metadata["exit_code"] == 7
        assert metadata["timed_out"] is False
        assert metadata["truncated"] is False
        assert "stdout" not in metadata and "stderr" not in metadata
        model_result = provider.requests[1].messages[-1]["content"][0]
        assert model_result["is_error"] is True
        assert "7" in model_result["content"]
    finally:
        await host.close()
