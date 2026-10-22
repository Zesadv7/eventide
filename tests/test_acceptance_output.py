"""Independent acceptance keeps bounded previews and pageable long evidence."""

import shlex
import sys

from eventide.tool_results import reader
from tests.test_delivery_runtime import delivery_host


async def test_verifier_can_read_middle_beyond_preview_capture_limit(tmp_path):
    host = delivery_host(tmp_path)
    marker = "UNIQUE_MIDDLE_FAILURE"
    program = f"import sys; sys.stdout.write('a'*50_000+'{marker}'+'z'*50_000); sys.exit(1)"
    command = shlex.quote(sys.executable) + " -B -c " + shlex.quote(program)
    try:
        prepared = await host.manage_delivery("prepare", name="long-evidence")
        verified = await host.manage_delivery(
            "verify", delivery_id=prepared["delivery_id"], checks=[command],
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
        assert verified["status"] == "failed"
    finally:
        await host.close()
