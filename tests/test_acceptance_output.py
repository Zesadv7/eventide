"""Independent acceptance keeps bounded previews and pageable long evidence."""

import asyncio
import os
import shlex
import sys
from dataclasses import replace

import pytest

from eventide.host import RuntimeHost
from eventide.providers import ScriptedProvider
from eventide.tool_results import reader
from tests.test_delivery_runtime import delivery_host


@pytest.mark.parametrize("backend", ["local", "docker"])
async def test_verifier_can_read_middle_beyond_preview_capture_limit(tmp_path, backend):
    image = os.getenv("EVENTIDE_TEST_CONTAINER_IMAGE") if backend == "docker" else None
    if backend == "docker" and not image:
        pytest.skip("real Docker is explicit opt-in")
    host = delivery_host(tmp_path)
    marker = "UNIQUE_MIDDLE_FAILURE"
    program = f"import sys; sys.stdout.write('a'*50_000+'{marker}'+'z'*50_000); sys.exit(1)"
    python = "python" if image else shlex.quote(sys.executable)
    command = python + " -B -c " + shlex.quote(program)
    try:
        prepared = await host.manage_delivery("prepare", name="long-evidence")
        verified = await host.manage_delivery(
            "verify", delivery_id=prepared["delivery_id"], checks=[command], image=image,
        )
        check = verified["checks"][0]
        assert check["metadata"]["stdout_bytes"] > 25_000
        assert marker not in check["content"]
        identity = host.store.get_run_identity(verified["control_run_id"])
        read = reader(host.store, identity["session_id"])
        legacy_pages = "".join(
            read(verified["control_run_id"], check["call_id"], offset=offset)
            for offset in range(0, len(check["content"]), 4_000)
        )
        assert marker not in legacy_pages
        page = read(
            verified["control_run_id"], check["call_id"], stream="stdout",
            offset=49_990, limit=80,
        )
        assert marker in page
        assert "capture_state=complete" in page
        assert verified["status"] == "failed"
    finally:
        await host.close()


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_verifier_interruption_retains_prefix_after_host_restart(tmp_path, stop):
    host = delivery_host(tmp_path)
    try:
        prepared = await host.manage_delivery("prepare", name="interrupted-evidence")
        if stop == "timeout":
            host.settings = replace(host.settings, command_timeout=0.5)
        program = "import time; print('PREFIX BEFORE STOP', flush=True); time.sleep(30)"
        command = shlex.quote(sys.executable) + " -B -c " + shlex.quote(program)
        task = asyncio.create_task(host.manage_delivery(
            "verify", delivery_id=prepared["delivery_id"], checks=[command],
        ))
        if stop == "cancel":
            async def wait_for_prefix():
                while True:
                    if any(path.stat().st_size for path in
                           host.acceptance_output.root.rglob("stdout.part")):
                        return
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_for_prefix(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result["checks"][0]["metadata"]["timed_out"]
        recorded = [event for event in host.store.acceptance_output_events()
                    if event["type"] == "acceptance.output_recorded"][-1]
        payload = recorded["payload"]
        assert payload["streams"]["stdout"]["capture_state"] == "interrupted"
        run_id, call_id = payload["source_run_id"], payload["call_id"]
        if stop == "cancel":
            assert host.store.get_tool_result(recorded["session_id"], run_id, call_id) is None
        settings = host.settings
        await host.close()
        host = RuntimeHost(settings, ScriptedProvider([]))
        page = reader(host.store, recorded["session_id"], outputs=host.acceptance_output)(
            run_id, call_id, stream="stdout",
        )
        assert "PREFIX BEFORE STOP" in page and "capture_state=interrupted" in page
    finally:
        await host.close()
