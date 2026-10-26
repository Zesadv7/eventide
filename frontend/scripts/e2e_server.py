"""Disposable real Runtime API for the Vue journey; no external model/network."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import uvicorn


def main() -> None:
    repository = Path(
        os.environ.get("EVENTIDE_E2E_REPOSITORY", Path(__file__).resolve().parents[2])
    ).resolve()
    sys.path.insert(0, str(repository))
    from eventide.api import create_app
    from eventide.config import Settings
    from eventide.host import RuntimeHost
    from eventide.models import ModelResponse, ToolCall
    from eventide.providers import ProviderError
    from eventide.streaming import ModelStreamEvent
    from tests.e2e_server import E2EUvicornServer, FakeProvider, build_fixtures

    class ConnectionProbeProvider:
        """Exercise the real probe owner/ledger with no SDK or external network."""

        async def complete(self, request):
            await asyncio.sleep(0.3)
            if request.model == "vue-probe-failure":
                raise ProviderError("401 fixture connection refused", retryable=False)
            return ModelResponse("连接成功", usage={"input_tokens": 8, "output_tokens": 2})

        async def close(self):
            pass

    import eventide.host as host_module
    import eventide.native_execution as native_execution_module
    import eventide.native_sandbox.probe as native_probe
    from eventide.native_sandbox.policy import NativeBackendKind
    from eventide.native_sandbox.probe import BackendDetection
    from eventide.native_sandbox.runner import NativeSandboxRunner, _unsandboxed_test_launcher

    # Only this disposable journey server replaces the OS launcher/probe.
    # Product defaults still launch through Seatbelt/bubblewrap without fallback.
    native_workers = []
    native_calls = []

    def native_fixture_probe(kind):
        available = kind is NativeBackendKind.SEATBELT
        return BackendDetection(
            kind=kind, platform="e2e-test-fixture", platform_supported=available,
            binary_path="/test/sandbox-exec" if available else None,
            available=available,
            detail="测试注入：可验证文件接线，内核隔离未验证" if available
            else "本旅程未启用该测试后端",
        )

    class JourneyNativeWorker(NativeSandboxRunner):
        async def call(self, operation, **params):
            result = await super().call(operation, **params)
            native_calls.append({
                "root": str(self.policy.workspace_root), "operation": operation,
                "pid": self._process.pid,
            })
            return result

    def native_fixture_runner(policy, backend=None):
        runner = JourneyNativeWorker(
            policy, backend=backend, launcher=_unsandboxed_test_launcher,
        )
        native_workers.append(runner)
        return runner

    native_probe.detect_backend = native_fixture_probe
    native_execution_module.NativeSandboxRunner = native_fixture_runner

    reconcile_fault_calls: set[str] = set()

    class JourneyProvider(FakeProvider):
        async def complete(self, request):
            if "side-question investigator" in self._prompt(request):
                if "图等待场景" in self._prompt(request):
                    await asyncio.Event().wait()
                if len(request.messages) == 1:
                    return ModelResponse(tool_calls=[self._call("read_file", {
                        "path": "src/demo.py",
                    })], usage={"input_tokens": 40, "output_tokens": 10})
                return ModelResponse(text="旁枝调查完成：已读取独立只读副本。",
                                     usage={"input_tokens": 40, "output_tokens": 10})
            if any(label in self._prompt(request) for label in (
                "交付修复场景", "检索交付场景", "文件操作编辑场景", "文件操作新建场景",

                "原生文件场景",

                "文件核查场景",

            )):
                async for event in self.stream(request):
                    if event.response is not None:
                        return event.response
            return await super().complete(request)

        async def stream(self, request):
            prompt = self._prompt(request)
            step = self.counts[prompt] = self.counts.get(prompt, 0) + 1
            usage = {"input_tokens": 100 + step, "output_tokens": 20}
            if "side-question investigator" in prompt:
                response = await self.complete(request)

            elif "摘要对照场景" in prompt:
                response = ModelResponse(text="摘要对照的原始任务已完成", usage=usage)

            elif "原生文件场景" in prompt:
                names = {tool["name"] for tool in request.tools}
                if step == 1:
                    response = ModelResponse(tool_calls=[self._call("read_file", {
                        "path": "src/demo.py",
                    })], stop_reason="tool_use", usage=usage)
                elif step == 2 and "write_file" in names:
                    response = ModelResponse(tool_calls=[self._call("write_file", {
                        "path": "native/note.txt", "content": "native worker candidate\n",
                    })], stop_reason="tool_use", usage=usage)
                else:
                    response = ModelResponse(text="原生文件检查完成", usage=usage)

            elif "命令日志场景" in prompt:
                if step == 1:
                    response = ModelResponse(tool_calls=[self._call("bash", {
                        "command": "printf '%s' 'BEGIN'; "
                        "i=0; while [ $i -lt 700 ]; do printf 'abcdefghij'; i=$((i+1)); done; "
                        "printf 'PAGE-MIDDLE-MARKER'; printf 'ERR-STREAM-MARKER' >&2",
                    })], stop_reason="tool_use", usage=usage)
                else:
                    response = ModelResponse(text="命令日志检查完成", usage=usage)
            elif "只读并发场景" in prompt:
                if step == 1:
                    response = ModelResponse(tool_calls=[
                        self._call("read_file", {"path": "src/demo.py"}),
                        self._call("glob", {"pattern": "src/*.py"}),
                        self._call("search_text", {"query": "source", "pattern": "src/*.py"}),
                    ], stop_reason="tool_use", usage=usage)
                else:
                    response = ModelResponse(text="并发读取检查完成", usage=usage)
            elif "源码证据场景" in prompt:
                if step == 1:
                    response = ModelResponse(tool_calls=[self._call("read_file", {
                        "path": "src/demo.py", "offset": 1, "limit": 2,
                    })], stop_reason="tool_use", usage=usage)
                elif step == 2:
                    response = ModelResponse(tool_calls=[self._call("search_text", {
                        "query": "source", "pattern": "src/*.py", "context_lines": 1,
                    })], stop_reason="tool_use", usage=usage)
                else:
                    response = ModelResponse(text="源码证据检查完成", usage=usage)
            elif "检索旅程" in prompt or "检索交付场景" in prompt:
                if step == 1:
                    response = ModelResponse(tool_calls=[self._call("search_knowledge", {
                        "query": "demo", "k": 5,
                    })], stop_reason="tool_use", usage=usage)
                elif step == 2:
                    response = ModelResponse(tool_calls=[self._call("read_file", {
                        "path": "src/demo.py",
                    })], stop_reason="tool_use", usage=usage)
                elif step == 3 and "检索交付场景" in prompt:
                    response = ModelResponse(tool_calls=[self._call("write_file", {
                        "path": "delivery-result.txt", "content": "retrieval accepted\n",
                    })], stop_reason="tool_use", usage=usage)
                else:
                    response = ModelResponse(text="检索调查完成：" + prompt, usage=usage)
            elif "文件核查场景" in prompt:
                if step == 1:
                    call = self._call("write_file", {
                        "path": "src/reconciled.txt", "content": "reconciled once\n",
                    })
                    reconcile_fault_calls.add(call.id)
                    response = ModelResponse(tool_calls=[call], stop_reason="tool_use", usage=usage)
                else:
                    response = ModelResponse(text="核查后继续完成", usage=usage)
            elif "文件操作编辑场景" in prompt:
                if step == 1:
                    response = ModelResponse(tool_calls=[self._call("edit_file", {
                        "path": "app.py", "old_text": 'value = "original"',
                        "new_text": 'value = "<script>alert(1)</script>"',
                    })], stop_reason="tool_use", usage=usage)
                else:
                    response = ModelResponse(text="文件操作编辑完成", usage=usage)
            elif "文件操作新建场景" in prompt:
                if step == 1:
                    response = ModelResponse(tool_calls=[self._call("write_file", {
                        "path": "new-created.txt",
                        "content": "created <img src=x onerror=alert(1)>\n",
                    })], stop_reason="tool_use", usage=usage)
                else:
                    response = ModelResponse(text="文件操作新建完成", usage=usage)
            elif "交付修复场景" in prompt:
                if step == 1:
                    response = ModelResponse(tool_calls=[self._call("write_file", {
                        "path": "delivery-result.txt", "content": "independently accepted\n",
                    })], stop_reason="tool_use", usage=usage)
                else:
                    response = ModelResponse(text="交付候选完成", usage=usage)
            elif "队列并发场景" in prompt:
                yield ModelStreamEvent("text_delta", text="正在执行队列隔离验证。" * 50)
                await asyncio.sleep(60)
                response = ModelResponse(text="队列验证结束", usage=usage)
            elif "恢复中心场景" in prompt:
                if step == 1:
                    yield ModelStreamEvent("text_delta", text="等待手动停止。" * 50)
                    await asyncio.sleep(30)
                response = ModelResponse(text="恢复中心续做完成", usage=usage)
            elif "停止场景" in prompt:
                yield ModelStreamEvent("text_delta", text="正在等待工具准备。" * 50)
                await asyncio.sleep(30)
                response = ModelResponse(text="不应完成", usage=usage)
            elif "副本写入场景" in prompt:
                if step == 1:
                    response = ModelResponse(
                        tool_calls=[self._call("write_file", {
                            "path": "result.txt", "content": prompt,
                        })], stop_reason="tool_use", usage=usage,
                    )
                else:
                    yield ModelStreamEvent("text_delta", text="正在核对独立工作副本。" * 50)
                    await asyncio.sleep(8)
                    response = ModelResponse(text="副本已完成：" + prompt, usage=usage)
            elif "模型重试场景" in prompt:
                if step == 1:
                    raise ProviderError(
                        "fixture network interruption", retryable=True,
                        category="network", retry_after=5,
                    )
                response = ModelResponse(text="模型自动恢复完成", usage=usage)
            elif "并发快照场景" in prompt:
                yield ModelStreamEvent("text_delta", text="正在分析独立只读快照。" * 50)
                await asyncio.sleep(8)
                response = ModelResponse(text="快照已完成：" + prompt, usage=usage)
            elif "主动提问场景" in prompt:
                response = ModelResponse(
                    tool_calls=[self._call("ask_user", {"questions": [{
                        "text": "这个项目以后使用哪种语言？", "choices": ["中文", "英文"],
                        "allow_free_text": True,
                    }]})], stop_reason="tool_use", usage=usage,
                ) if step == 1 else ModelResponse(text="已收到你的回答，继续原任务。", usage=usage)
            elif "审批场景" in prompt:
                response = self._approval(step, usage)
            elif "停驻场景" in prompt:
                response = self._park(step, usage)
            elif "技能场景" in prompt:
                response = ModelResponse(
                    tool_calls=[ToolCall("load-review", "load_skill", {"name": "code-review"})],
                    stop_reason="tool_use", usage=usage,
                ) if step == 1 else ModelResponse(text="已加载审查技能", usage=usage)
            elif "流式场景" in prompt:
                for part in ("正在阅读源码与项目约定。" * 30, "已经形成初步结论。" * 30):
                    yield ModelStreamEvent("text_delta", text=part)
                    await asyncio.sleep(1)
                response = ModelResponse(
                    text="流式场景已完成，最终内容以这条回复为准。", usage=usage
                )
            else:
                response = ModelResponse(text="已读取项目：" + prompt, usage=usage)
            yield ModelStreamEvent("response_completed", response=response)

    with tempfile.TemporaryDirectory(prefix="eventide-vue-e2e-") as temporary:
        root = Path(temporary).resolve()
        fixtures = build_fixtures(root / "workspaces", sys.executable)
        os.chdir(root)
        settings = Settings(
            workdir=fixtures["main"],
            state_dir=root / "state",
            provider="openai_responses",
            api_key="fixture-only",
            base_url=None,
            model="vue-fixture-model",
            max_steps=6,
        )
        provider = JourneyProvider(max_steps=6)
        runtime = RuntimeHost(settings, provider)
        provider.attach(runtime)
        original_file_record = runtime.file_operations._record

        def record_with_reconcile_fault(record, status, **extra):
            # Disposable fixture only: fail one known scenario call after its
            # bytes were written, never arbitrary writes in the application.
            if (status == "completed" and record["tool"] == "write_file"
                    and record["path"] == "src/reconciled.txt"
                    and record["call_id"] in reconcile_fault_calls):
                reconcile_fault_calls.remove(record["call_id"])
                raise OSError("controlled E2E completion journal failure")
            return original_file_record(record, status, **extra)

        runtime.file_operations._record = record_with_reconcile_fault

        def build_offline_provider(candidate):
            if candidate.model.startswith("vue-probe-"):
                return ConnectionProbeProvider()
            replacement = JourneyProvider(max_steps=6)
            replacement.attach(runtime)
            return replacement

        host_module.build_provider = build_offline_provider
        app = create_app(runtime)
        held_index_locks: dict[str, asyncio.Lock] = {}


        @app.post("/api/__e2e/summary/{session_id}")
        async def prepare_summary(session_id: str):
            from eventide.context_builder import ContextBuilder
            from eventide.providers import ScriptedProvider

            summary = {"schema_version": 2, "goal": "解释代码", "hard_constraints": [
                {"text": "不修改接口", "src": [{"msg": 0, "quote": "不要修改 API"}]},
            ], "facts": [], "checks": [], "failures": [],
                "unverified": ["尚未确认原因"], "next_steps": ["继续调查"]}
            await ContextBuilder(runtime.store).build(
                session_id, provider=ScriptedProvider([{"text": json.dumps(summary)}]),
                provider_name=settings.provider, model=settings.model, budget=20000, force=True,
            )
            return {"saved": len(runtime.store.checkpoints(session_id))}

        @app.get("/api/__e2e/native-proof/{run_id}")
        async def native_proof(run_id: str):
            # Read only known fixture paths; this is evidence, never a tool fallback.
            intent = runtime._admitted_intent(run_id)
            assert intent["execution_profile_config"]["kind"] == "native"
            binding = runtime._execution_binding(run_id)
            assert binding["kind"] == "worktree"
            candidate, working = runtime._execution_paths(binding)
            source = Path(runtime.store.get_workspace(intent["workspace_id"]).path)
            note = candidate / "native/note.txt"
            from eventide.workspace import git

            calls = [item for item in native_calls if item["root"] == str(working)]
            workers = [worker for worker in native_workers
                       if worker.policy.workspace_root == working]
            return {
                "fixture_launcher": "stdlib-worker-without-os-wrapper",
                "verification": "unverified", "server_pid": os.getpid(),
                "candidate_root": str(candidate), "source_root": str(source),
                "source_status": git(source, "status", "--porcelain").decode(),
                "source_note_exists": (source / "native/note.txt").exists(),
                "candidate_note": note.read_text() if note.exists() else None,
                "source_demo_sha256": hashlib.sha256(
                    (source / "src/demo.py").read_bytes(),
                ).hexdigest(),
                "profile": intent["execution_profile_config"], "worker_calls": calls,
                "workers_closed": all(worker._process is None for worker in workers),
            }


        @app.post("/api/__e2e/retrieval-hold/{workspace_id}")
        async def hold_retrieval(workspace_id: str):
            runtime.store.get_workspace(workspace_id)
            lock = runtime._index_build_locks.setdefault(workspace_id, asyncio.Lock())
            await lock.acquire()
            held_index_locks[workspace_id] = lock
            return {"held": workspace_id}

        @app.post("/api/__e2e/retrieval-release/{workspace_id}")
        async def release_retrieval(workspace_id: str):
            held_index_locks.pop(workspace_id).release()
            return {"released": workspace_id}

        server = E2EUvicornServer(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        )
        print(f"E2E_WORKSPACE={fixtures['main']}", flush=True)
        try:
            server.run()
        finally:
            for lock in held_index_locks.values():
                lock.release()


if __name__ == "__main__":
    main()
