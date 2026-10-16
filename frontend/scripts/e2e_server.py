"""Disposable real Runtime API for the Vue journey; no external model/network."""

from __future__ import annotations

import asyncio
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

    class JourneyProvider(FakeProvider):
        async def complete(self, request):
            if "交付修复场景" in self._prompt(request):
                async for event in self.stream(request):
                    if event.response is not None:
                        return event.response
            return await super().complete(request)

        async def stream(self, request):
            prompt = self._prompt(request)
            step = self.counts[prompt] = self.counts.get(prompt, 0) + 1
            usage = {"input_tokens": 100 + step, "output_tokens": 20}
            if "交付修复场景" in prompt:
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
            elif "并发快照场景" in prompt:
                yield ModelStreamEvent("text_delta", text="正在分析独立只读快照。" * 50)
                await asyncio.sleep(8)
                response = ModelResponse(text="快照已完成：" + prompt, usage=usage)
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
        root = Path(temporary)
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

        def build_offline_provider(candidate):
            if candidate.model.startswith("vue-probe-"):
                return ConnectionProbeProvider()
            replacement = JourneyProvider(max_steps=6)
            replacement.attach(runtime)
            return replacement

        host_module.build_provider = build_offline_provider
        app = create_app(runtime)
        server = E2EUvicornServer(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        )
        print(f"E2E_WORKSPACE={fixtures['main']}", flush=True)
        server.run()


if __name__ == "__main__":
    main()
