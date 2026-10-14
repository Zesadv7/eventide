"""Commands report process facts and drain output without unbounded buffering."""

import asyncio
import os
import sys

import pytest

from eventide.executor import LocalCommandExecutor, ToolContext, ToolExecutor
from eventide.models import CommandResult, ToolCall, ToolResult
from eventide.policy import PolicyEngine


def python_command(workspace, source):
    script = workspace / "command.py"
    script.write_text(source, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


@pytest.mark.parametrize("exit_code", [0, 7])
async def test_exit_status_is_preserved_without_output(isolated_workspace, exit_code):
    command = python_command(isolated_workspace, f"import sys; sys.exit({exit_code})")
    executor = ToolExecutor({"bash": lambda **_: None})
    result = await executor.execute(
        ToolCall("exit", "bash", {"command": command}),
        ToolContext(isolated_workspace, "run", PolicyEngine()),
    )
    assert result.is_error is (exit_code != 0)
    assert result.metadata["exit_code"] == exit_code
    assert f"exit_code={exit_code}" in result.content
    assert "(no output)" in result.content


async def test_output_text_does_not_decide_process_success(isolated_workspace):
    command = python_command(
        isolated_workspace,
        "import sys\nprint('Error: an example string')\nsys.stderr.write('diagnostic')\n",
    )
    executor = ToolExecutor({"bash": lambda **_: None})
    result = await executor.execute(
        ToolCall("success", "bash", {"command": command}),
        ToolContext(isolated_workspace, "run", PolicyEngine()),
    )
    assert not result.is_error
    assert result.metadata["exit_code"] == 0
    assert "stdout:\nError: an example string" in result.content
    assert "stderr:\ndiagnostic" in result.content


async def test_large_stdout_and_stderr_retain_head_tail_and_byte_counts(isolated_workspace):
    command = python_command(
        isolated_workspace,
        "import sys\n"
        "sys.stdout.write('HEAD' + 'x' * 1_000_000 + 'TAIL')\n"
        "sys.stderr.write('START' + 'y' * 1_000_000 + 'END')\n",
    )
    result = await LocalCommandExecutor(output_limit=64).execute(command, isolated_workspace)
    assert result.exit_code == 0 and result.truncated
    assert result.stdout_bytes == result.stderr_bytes == 1_000_008
    assert result.stdout_omitted_bytes == result.stderr_omitted_bytes == 1_000_008 - 64
    assert result.stdout.startswith("HEAD") and result.stdout.endswith("TAIL")
    assert result.stderr.startswith("START") and result.stderr.endswith("END")
    assert "999944 output bytes omitted" in result.stdout
    assert len(result.stdout) < 150 and len(result.stderr) < 150


async def test_utf8_output_preserved_when_not_truncated(isolated_workspace):
    command = python_command(
        isolated_workspace,
        "import sys\nsys.stdout.buffer.write('中文输出'.encode('utf-8'))\n",
    )
    result = await LocalCommandExecutor(output_limit=14).execute(command, isolated_workspace)
    assert result.stdout == "中文输出" and result.stdout_bytes == 12
    assert not result.truncated


async def test_utf8_truncation_does_not_misdetect_console_encoding(isolated_workspace, monkeypatch):
    monkeypatch.setattr("eventide.utils.console_encoding", lambda: "cp936")
    command = python_command(
        isolated_workspace,
        "import sys\nsys.stdout.buffer.write(('中文' * 100).encode('utf-8'))\n",
    )
    result = await LocalCommandExecutor(output_limit=14).execute(command, isolated_workspace)
    assert result.stdout.startswith("中文") and result.stdout.endswith("中文")
    assert result.stdout_bytes == 600 and result.stdout_omitted_bytes == 586


async def test_timeout_keeps_partial_output_and_process_facts(isolated_workspace):
    command = python_command(
        isolated_workspace,
        "import time\nprint('before timeout', flush=True)\ntime.sleep(30)\n",
    )
    result = await LocalCommandExecutor(timeout=0.5).execute(command, isolated_workspace)
    assert result.timed_out and result.is_error
    assert result.exit_code is not None
    assert result.stdout == "before timeout\n"
    assert result.stdout_bytes == len(result.stdout)
    assert result.duration_ms >= 500


async def test_structured_custom_executor_and_model_block(isolated_workspace):
    class FakeCommandExecutor:
        async def execute(self, command, cwd):
            return CommandResult(9, stderr="failed", duration_ms=10)

    result = await ToolExecutor(
        {"bash": lambda **_: None}, command_executor=FakeCommandExecutor()
    ).execute(
        ToolCall("custom", "bash", {"command": "irrelevant"}),
        ToolContext(isolated_workspace, "run", PolicyEngine()),
    )
    assert result.is_error and result.metadata["exit_code"] == 9
    assert "failed" in result.content
    assert "metadata" not in result.as_block()
    assert ToolResult("id", "custom", "ok").metadata == {}


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup")
async def test_cancel_terminates_command_and_descendant(isolated_workspace):
    command = python_command(
        isolated_workspace,
        "import os, pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "pathlib.Path('pids').write_text(f'{os.getpid()} {child.pid}')\n"
        "time.sleep(30)\n",
    )
    task = asyncio.create_task(LocalCommandExecutor().execute(command, isolated_workspace))
    pid_file = isolated_workspace / "pids"
    for _ in range(200):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    # The descendant might briefly remain as a zombie; it must not be running.
    pids = pid_file.read_text().split()
    process = await asyncio.create_subprocess_exec(
        "ps", "-o", "stat=", "-p", ",".join(pids), stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    assert all(state.startswith(b"Z") for state in stdout.split())


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell and process-group cleanup")
async def test_timeout_kills_descendant_after_shell_exit(isolated_workspace):
    command = python_command(isolated_workspace, "import time; time.sleep(30)") + " &"
    result = await LocalCommandExecutor(timeout=0.1).execute(command, isolated_workspace)
    assert result.timed_out and result.is_error
    assert result.exit_code == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal escalation")
async def test_repeated_cancel_cannot_interrupt_cleanup(isolated_workspace):
    command = python_command(
        isolated_workspace,
        "import os, pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path('pid').write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
    )
    task = asyncio.create_task(LocalCommandExecutor().execute(command, isolated_workspace))
    pid_file = isolated_workspace / "pid"
    for _ in range(200):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    process = await asyncio.create_subprocess_exec(
        "ps", "-o", "stat=", "-p", pid_file.read_text(), stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    assert all(state.startswith(b"Z") for state in stdout.split())


@pytest.mark.parametrize("kwargs", [{"timeout": 0}, {"output_limit": 1}])
def test_command_limits_must_be_valid(kwargs):
    with pytest.raises(ValueError):
        LocalCommandExecutor(**kwargs)
