"""User-directed delivery operations; execution and facts remain owned by Host."""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eventide.delivery import (
    apply_delivery,
    build_patch,
    capture_delivery,
    validate_delivery_source,
    verify_source_unchanged,
)
from eventide.executor import CommandExecutor, LocalCommandExecutor, ToolContext, ToolExecutor
from eventide.models import RunRequest, ToolCall
from eventide.worktrees import WorktreeService

if TYPE_CHECKING:
    from eventide.host import RuntimeHost


class DeliveryController:
    def __init__(self, host: RuntimeHost):
        self.host = host

    def load(self, delivery_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"delivery_[a-f0-9]{16}", delivery_id):
            raise ValueError("Invalid delivery identity")
        facts = self.host.store.delivery_facts(delivery_id)
        prepared = next((e["payload"] for e in facts if e["type"] == "delivery.prepared"), None)
        if prepared is None:
            raise ValueError("Delivery not found")
        verified = next(
            (e["payload"] for e in reversed(facts) if e["type"] == "delivery.verified"), None
        )
        return {
            **prepared,
            "verification": verified,
            "applied": any(e["type"] == "delivery.applied" for e in facts),
        }

    def _artifact(self, delivery_id: str, patch_hash: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", patch_hash):
            raise ValueError("Invalid patch identity")
        path = self.host.state_dir / "deliveries" / delivery_id / f"{patch_hash}.patch"
        current = path
        while current != self.host.state_dir:
            if current.is_symlink():
                raise ValueError("Delivery artifact cannot traverse symbolic links")
            current = current.parent
        if not path.resolve().is_relative_to(self.host.state_dir):
            raise ValueError("Delivery artifact escapes state root")
        return path

    async def execute(
        self,
        action: str,
        *,
        target: str | Path | None = None,
        delivery_id: str | None = None,
        name: str | None = None,
        goal: str | None = None,
        checks: list[str] | None = None,
        image: str | None = None,
        protected_patterns: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        host = self.host
        await host.initialize()
        if image is not None and (not isinstance(image, str) or not image.strip()):
            raise ValueError("Container image cannot be empty")
        if action not in {"prepare", "run", "verify", "apply", "show"}:
            raise ValueError("Unknown delivery action")
        value = self.load(delivery_id) if delivery_id else None
        if action == "show":
            if value is None:
                raise ValueError("Delivery identity is required")
            return value
        if action == "prepare":
            workspace = host.resolve_or_register_workspace(
                target or host.default_workspace.workspace_id
            )
            delivery_id = f"delivery_{uuid.uuid4().hex[:16]}"
        else:
            if value is None:
                raise ValueError("Delivery identity is required")
            workspace = host.store.get_workspace(value["workspace_id"])
            if value["applied"]:
                raise ValueError("Delivery was already applied")
        source = Path(workspace.path)
        assert delivery_id is not None
        task = asyncio.current_task()
        assert task is not None
        host._active_tasks.add(task)
        run_id: str | None = None
        try:
            async with host._workspace_execution(workspace.workspace_id):
                if host.store.workspace_has_unknown_outcome(workspace.workspace_id):
                    raise ValueError("Source Workspace has an unresolved tool outcome")
                if action != "prepare":
                    value = self.load(delivery_id)
                    if value["applied"]:
                        raise ValueError("Delivery was already applied")
                session = host.create_session(
                    f"deliveries_{workspace.workspace_id}", workspace_id=workspace.workspace_id
                )
                host.store.update_session(session, title="Delivery 管理", archived=True)
                run_id = f"run_{uuid.uuid4().hex[:16]}"
                host.store.create_run(run_id, session)
                host._run_tasks[run_id] = task
                await host._emit(
                    run_id,
                    "run.started",
                    {
                        "source": "delivery",
                        "workspace_id": workspace.workspace_id,
                        "delivery_id": delivery_id,
                        "action": action,
                    },
                    None,
                    author="user",
                )
                await host._emit(
                    run_id,
                    "delivery.requested",
                    {
                        "delivery_id": delivery_id,
                        "action": action,
                    },
                    None,
                    author="user",
                )
                if action == "prepare":
                    if name is None:
                        raise ValueError("Managed worktree name is required")
                    service = WorktreeService(source, host.state_dir, workspace.workspace_id)
                    await host._admission_check(validate_delivery_source, source)
                    # All worktree mutation finishes before source ownership is released.
                    created: list[Any] = []
                    await host._admission_check(
                        lambda: created.append(service.create(name, "HEAD"))
                    )
                    checkout = Path(created[0].path)
                    receipt = await host._admission_check(
                        lambda: capture_delivery(
                            source, checkout, protected_patterns=protected_patterns
                        )
                    )
                    result = {
                        "delivery_id": delivery_id,
                        "workspace_id": workspace.workspace_id,
                        "name": name,
                        "receipt": receipt,
                    }
                    await host._emit(run_id, "delivery.prepared", result, None)
                else:
                    assert value is not None
                    receipt = dict(value["receipt"])
                    checkout = Path(receipt["checkout"])
                    checkout_ws = host.resolve_or_register_workspace(checkout)
                    await host._admission_check(verify_source_unchanged, source, receipt)
                    if action == "run":
                        if not goal or not goal.strip() or len(goal) > 20_000:
                            raise ValueError("Worker goal must contain 1-20000 characters")
                        child_session = host.create_session(workspace_id=checkout_ws.workspace_id)
                        child_id = f"run_{uuid.uuid4().hex[:16]}"
                        config = {
                            "kind": "container" if image is not None else "local",
                            "disable_mcp": True,
                            "writer_delivery_id": delivery_id,
                            **({"image": image} if image is not None else {}),
                        }
                        await host._reserve_run(
                            child_session,
                            child_id,
                            RunRequest(goal, child_session, child_id),
                            execution_config=config,
                        )
                        await host._emit(
                            run_id,
                            "worker.started",
                            {
                                "child_run_id": child_id,
                                "child_session_id": child_session,
                                "role": "writer",
                                "delivery_id": delivery_id,
                            },
                            None,
                        )
                        child = host.start_admitted_run(child_id)
                        try:
                            completed = await child
                        finally:
                            if not child.done():
                                child.cancel()
                            with suppress(asyncio.CancelledError):
                                await child
                        result = {
                            "delivery_id": delivery_id,
                            "worker_run_id": completed.run_id,
                            "worker_session_id": child_session,
                            "status": completed.status,
                            "output": completed.output,
                            "usage": completed.usage,
                            "checkout": str(checkout),
                        }
                        await host._emit(run_id, "delivery.worker_finished", result, None)
                    else:
                        async with host._workspace_execution(checkout_ws.workspace_id):
                            if host.store.workspace_has_unknown_outcome(checkout_ws.workspace_id):
                                raise ValueError("Checkout has an unresolved tool outcome")
                            if action == "verify":
                                result = await self._verify(
                                    run_id,
                                    delivery_id,
                                    source,
                                    checkout,
                                    receipt,
                                    checks or [],
                                    image,
                                )
                            else:
                                verified = value["verification"]
                                if not verified or verified["status"] != "passed":
                                    raise ValueError(
                                        "Delivery has no passing independent verification"
                                    )
                                receipt = dict(verified["receipt"])
                                patch = self._artifact(
                                    delivery_id, receipt["patch_sha256"]
                                ).read_bytes()
                                applied: list[dict[str, Any]] = []
                                try:
                                    await host._admission_check(
                                        lambda: applied.append(
                                            apply_delivery(source, patch, receipt)
                                        )
                                    )
                                finally:
                                    if applied:
                                        await host._emit(
                                            run_id,
                                            "delivery.applied",
                                            {"delivery_id": delivery_id, **applied[0]},
                                            None,
                                            author="user",
                                        )
                                result = {"delivery_id": delivery_id, **applied[0]}
                await host._emit(
                    run_id,
                    "run.completed",
                    {
                        "output": f"Delivery {action} finished",
                        "steps": 0,
                        "tool_calls": len(checks or []),
                        "usage": {},
                    },
                    None,
                )
                return {**result, "control_run_id": run_id}
        except BaseException as exc:
            if run_id and not host.store.run_is_terminal(run_id):
                await host._emit(
                    run_id,
                    "run.interrupted" if isinstance(exc, asyncio.CancelledError) else "run.failed",
                    {
                        "error": host._clean(str(exc)),
                        "reason": "cancelled" if isinstance(exc, asyncio.CancelledError) else None,
                    },
                    None,
                )
            raise
        finally:
            if run_id:
                host._run_tasks.pop(run_id, None)
            host._active_tasks.discard(task)

    async def _verify(
        self,
        run_id: str,
        delivery_id: str,
        source: Path,
        checkout: Path,
        receipt: dict[str, Any],
        checks: list[str],
        image: str | None,
    ) -> dict[str, Any]:
        host = self.host
        if not 1 <= len(checks) <= 4 or any(not c.strip() or len(c) > 2_000 for c in checks):
            raise ValueError(
                "Verification requires 1-4 explicit commands, at most 2000 characters each"
            )
        patch = await host._admission_check(build_patch, source, checkout, receipt)
        command_executor: CommandExecutor = LocalCommandExecutor(host.settings.command_timeout)
        if image is not None:
            from eventide.container_executor import ContainerCommandExecutor

            command_executor = ContainerCommandExecutor(
                image, timeout=host.settings.command_timeout
            )
        tools = ToolExecutor({"bash": lambda **_args: ""}, command_executor=command_executor)
        results: list[dict[str, Any]] = []
        for index, command in enumerate(checks):
            call = ToolCall(f"check_{index + 1}", "bash", {"command": command})
            await host._emit(
                run_id,
                "tool.prepared",
                {
                    "call_id": call.id,
                    "name": "bash",
                    "arguments": host._semantic(call.arguments),
                    "readonly": False,
                },
                None,
            )
            execution = asyncio.create_task(
                tools.execute(call, ToolContext(checkout, run_id, host.policy))
            )
            try:
                checked = await asyncio.shield(execution)
            except asyncio.CancelledError:
                execution.cancel()
                while not execution.done():
                    with suppress(asyncio.CancelledError):
                        await asyncio.shield(execution)
                if not execution.cancelled():
                    settled = execution.result()
                    if settled.metadata.get("outcome_unknown"):
                        await host._emit(
                            run_id,
                            "tool.outcome_unknown",
                            {
                                "call_id": call.id,
                                "name": "bash",
                                "readonly": image is not None,
                                "workspace_id": host.resolve_or_register_workspace(
                                    checkout
                                ).workspace_id,
                                "error": settled.content,
                            },
                            None,
                        )
                raise
            evidence = {
                "call_id": call.id,
                "name": "bash",
                "content": checked.content,
                "is_error": checked.is_error,
                "metadata": checked.metadata,
            }
            evidence = host._semantic(evidence)
            results.append(evidence)
            if checked.metadata.get("outcome_unknown"):
                await host._emit(
                    run_id,
                    "tool.outcome_unknown",
                    {
                        "call_id": call.id,
                        "name": "bash",
                        "readonly": image is not None,
                        "workspace_id": host.resolve_or_register_workspace(checkout).workspace_id,
                        "error": checked.content,
                    },
                    None,
                )
                break
            await host._emit(run_id, "tool.completed", evidence, None)
        after_receipt = dict(receipt)
        after_patch = await host._admission_check(build_patch, source, checkout, after_receipt)
        if (
            after_receipt["checkout_checkpoint"] != receipt["checkout_checkpoint"]
            or after_patch != patch
        ):
            raise ValueError("Checkout changed during independent verification")
        passed = all(not r["is_error"] and r["metadata"].get("exit_code") == 0 for r in results)
        artifact = self._artifact(delivery_id, receipt["patch_sha256"])
        artifact.parent.mkdir(parents=True, exist_ok=True)
        if artifact.exists() and artifact.read_bytes() != patch:
            raise ValueError("Existing delivery artifact does not match its identity")
        if not artifact.exists():
            with artifact.open("xb") as stream:
                stream.write(patch)
        result = {
            "delivery_id": delivery_id,
            "status": "passed" if passed else "failed",
            "receipt": receipt,
            "patch_sha256": hashlib.sha256(patch).hexdigest(),
            "artifact": str(artifact),
            "checks": results,
            "executor": "container" if image is not None else "local",
        }
        await host._emit(run_id, "delivery.verified", result, None)
        return result
