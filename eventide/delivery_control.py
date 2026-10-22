"""Scheduled Web delivery operations; one root lease includes all Writer descendants."""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eventide.delivery_runtime import DeliveryController
from eventide.models import RunRequest, RunResult

if TYPE_CHECKING:
    from eventide.host import RuntimeHost
    from eventide.store import RuntimeStore


def is_delivery_control_run(store: RuntimeStore, run_id: str) -> bool:
    run = store.get_run_identity(run_id)
    if run is None:
        return False
    intent: dict[str, Any] = next(
        (e["payload"] for e in store.run_events(run_id) if e["type"] == "run.admitted"), {}
    )
    session = store.get_session(run["session_id"])
    if session is None or intent.get("source") != "delivery":
        return False
    workspace = store.get_workspace(session["workspace_id"])
    binding = intent.get("execution_binding", {})
    return (
        binding.get("kind") == "delivery"
        and binding.get("operation") in {"prepare", "repair", "apply"}
        and re.fullmatch(r"delivery_[a-f0-9]{16}", binding.get("delivery_id", "")) is not None
        and binding.get("workspace_id") == workspace.workspace_id
        and intent.get("workspace_id") == workspace.workspace_id
        and intent.get("project_id") == workspace.project_id
        and any(
            e["type"] == "control.session"
            and e["payload"]
            == {
                "kind": "delivery",
                "project_id": workspace.project_id,
            }
            for e in store.session_events(run["session_id"])
        )
    )


class WebDeliveryController:
    def __init__(self, host: RuntimeHost):
        self.host, self.store = host, host.store
        self.delivery = DeliveryController(host)

    def owner(self, project_id: str, delivery_id: str | None = None, *, create: bool = True) -> str:
        project = self.store.get_project(project_id)
        if delivery_id:
            saved = self.delivery.load(delivery_id)
            if saved["workspace_id"] != project.source_workspace_id:
                raise ValueError("Delivery belongs to another project")
        owner = f"deliveries_{delivery_id}" if delivery_id else f"delivery_prepare_{project_id}"
        marker = {"kind": "delivery", "project_id": project_id}
        session = self.store.get_session(owner)
        if session is None and create:
            self.host.create_session(owner, workspace_id=project.source_workspace_id)
            self.store.append_fact(owner, "control.session", marker)
            self.store.update_session(owner, title="交付管理", archived=True)
        elif session is not None:
            events = self.store.session_events(owner)
            if session["workspace_id"] != project.source_workspace_id:
                raise ValueError("Delivery Session identity conflict")
            if not any(e["type"] == "control.session" and e["payload"] == marker for e in events):
                # Upgrade only a genuine old DeliveryController-owned Session.
                legacy = delivery_id and any(
                    e["type"].startswith("delivery.")
                    and e["payload"].get("delivery_id") == delivery_id
                    for e in events
                )
                if not legacy:
                    raise ValueError("Delivery Session identity conflict")
                if create:
                    self.store.append_fact(owner, "control.session", marker)
        return owner

    async def admit(
        self,
        project_id: str,
        run_id: str,
        *,
        action: str,
        delivery_id: str | None = None,
        name: str | None = None,
        goal: str | None = None,
        checks: list[str] | None = None,
        image: str | None = None,
        max_rounds: int = 3,
        observed_token_limit: int = 60_000,
        knowledge_index: str = "existing",
        patch_sha256: str | None = None,
        request_fingerprint: str | None = None,
    ) -> str:
        from eventide.host import _Admission

        await self.host.initialize()
        if action not in {"prepare", "repair", "apply"}:
            raise ValueError("Unsupported Web delivery operation")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ValueError("Invalid operation identity")
        if action == "prepare" and (not name or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", name)):
            raise ValueError("Worktree name must be 1-48 lowercase letters, digits or hyphens")
        if action != "prepare" and not delivery_id:
            raise ValueError("Delivery identity required")
        if action == "repair":
            from eventide.retrieval_runtime import freeze_configuration, validate_preparation

            retrieval = freeze_configuration(self.host.settings, knowledge_index)
            if not goal or not goal.strip() or len(goal) > 20_000:
                raise ValueError("Repair goal must contain 1-20000 characters")
            if (
                not checks
                or not 1 <= len(checks) <= 4
                or any(not c.strip() or len(c) > 2000 for c in checks)
            ):
                raise ValueError("Provide 1-4 explicit acceptance commands")
            if type(max_rounds) is not int or not 1 <= max_rounds <= 3:
                raise ValueError("Repair permits 1-3 candidate verifications")
            if type(observed_token_limit) is not int or not 1 <= observed_token_limit <= 1_000_000:
                raise ValueError("Observed Token limit must be an integer between 1 and 1000000")
            if image is not None and not image.strip():
                raise ValueError("Container image cannot be empty")
        owner = self.owner(project_id, delivery_id)
        if self.host.session_busy(owner):
            raise ValueError("Delivery operation is busy")
        if delivery_id:
            value = self.delivery.load(delivery_id)
            if action == "repair" and knowledge_index == "prepare":
                checkout = self.host.resolve_or_register_workspace(
                    Path(value["receipt"]["checkout"]),
                )
                validate_preparation(self.host, checkout)
            if value["applied"]:
                raise ValueError("Delivery already applied")
            if action == "repair" and value["repair"]:
                raise ValueError("Delivery repair already started")
            if action == "apply" and (
                not value["verification"]
                or value["verification"]["status"] != "passed"
                or value["verification"]["patch_sha256"] != patch_sha256
            ):
                raise ValueError("Passing patch changed; review the latest evidence")
        else:
            delivery_id = f"delivery_{uuid.uuid4().hex[:16]}"
        project = self.store.get_project(project_id)
        binding = {
            "kind": "delivery",
            "operation": action,
            "delivery_id": delivery_id,
            "workspace_id": project.source_workspace_id,
        }
        intent = {
            "source": "delivery",
            "project_id": project_id,
            "workspace_id": project.source_workspace_id,
            "execution_binding": binding,
            "request_fingerprint": request_fingerprint,
            **(
                {"execution_config": {"writer_delivery_id": delivery_id}}
                if action == "repair"
                else {}
            ),
        }
        accepted = {
            "delivery_id": delivery_id,
            "action": action,
            "name": name,
            "goal": goal,
            "checks": list(checks or []),
            "image": image,
            "max_rounds": max_rounds,
            **(retrieval if action == "repair" else {}),
            **({"observed_token_limit": observed_token_limit}
               if action == "repair" and observed_token_limit != 60_000 else {}),
            "patch_sha256": patch_sha256,
        }
        ticket = self.host.dispatch.scheduler.reserve_admission(run_id, owner, startable=True)
        try:
            self.store.admit_run(
                run_id,
                owner,
                [
                    ("run.admitted", intent, "system", "runtime"),
                    ("delivery.operation_accepted", accepted, "system", "user"),
                ],
            )
            self.host._admission_done[run_id] = _Admission(owner, asyncio.Event())
            self.host._admissions[self.host._execution_key(run_id, intent)] = run_id
            self.host.dispatch.admitted(ticket)
        except BaseException as exc:
            self.host._finish_admitted(run_id, exc)
            self.host._release_admission(run_id)
            self.host.dispatch.scheduler.release_ticket(ticket)
            raise
        finally:
            self.host.dispatch.dispatch()
        return delivery_id

    def start(self, run_id: str) -> asyncio.Task[RunResult]:
        if (
            not is_delivery_control_run(self.store, run_id)
            or run_id not in self.host._admission_done
            or self.store.run_is_terminal(run_id)
            or run_id in self.host._run_tasks
            or self.host._closed
        ):
            raise ValueError("Delivery operation is not awaiting execution")
        try:
            task = self.host.dispatch.start(run_id, lambda: self._execute(run_id))
        except BaseException as exc:
            self.host._finish_admitted(run_id, exc)
            self.host._release_admission(run_id)
            self.host.dispatch.finished(run_id)
            raise
        self.host._track_run_task(run_id, task)
        return task

    async def _execute(self, run_id: str) -> RunResult:
        accepted = self.operation(run_id)["accepted"]
        run = self.store.get_run_identity(run_id)
        assert run is not None
        async with self.host._session_execution(run["session_id"], run_id):
            if accepted["action"] == "apply":
                value = self.delivery.load(accepted["delivery_id"])
                if not value["verification"] or (
                    value["verification"]["patch_sha256"] != accepted["patch_sha256"]
                ):
                    raise ValueError("Reviewed patch changed before adoption")
            await self.delivery.execute(
                accepted["action"],
                delivery_id=(None if accepted["action"] == "prepare" else accepted["delivery_id"]),
                target=self.host.sessions.workspace(run["session_id"]).workspace_id,
                name=accepted["name"],
                goal=accepted["goal"],
                checks=accepted["checks"],
                image=accepted["image"],
                max_rounds=accepted["max_rounds"],
                observed_token_limit=accepted.get("observed_token_limit", 60_000),
                knowledge_index=accepted.get("knowledge_index", "existing"),
                _retrieval_configuration=accepted.get("retrieval_configuration"),
                _control_run_id=run_id,
            )
            summary = self.store.get_run_summary(run_id)
            assert summary is not None
            return RunResult(
                run_id,
                run["session_id"],
                "completed",
                summary["output"] or "",
                0,
                0,
                0,
                turn_id=run["turn_id"],
            )

    def operation(self, run_id: str) -> dict[str, Any]:
        if not is_delivery_control_run(self.store, run_id):
            raise ValueError("Delivery operation not found")
        run = self.store.get_run_summary(run_id)
        assert run is not None
        accepted = next(
            e["payload"]
            for e in self.store.run_events(run_id)
            if e["type"] == "delivery.operation_accepted"
        )
        return {
            "run_id": run_id,
            "session_id": run["session_id"],
            "status": run["status"],
            "accepted": accepted,
            "error": run["error"],
            "reason": run["reason"],
        }

    def operations(
        self, project_id: str, *, limit: int = 20, before: str | None = None
    ) -> list[dict[str, Any]]:
        self.store.get_project(project_id)
        identities = []
        for event in self.store.facts_by_type(("delivery.operation_accepted",)):
            run_id = event["run_id"]
            intent = self.host._admitted_intent(run_id)
            if intent and intent.get("project_id") == project_id:
                identities.append(run_id)
        identities.reverse()
        if before is not None:
            identities = identities[identities.index(before) + 1 :] if before in identities else []
        return [self.operation(identity) for identity in identities[:limit]]

    def list(self, project_id: str) -> list[dict[str, Any]]:
        project = self.store.get_project(project_id)
        identities = {
            e["payload"]["delivery_id"]
            for e in self.store.facts_by_type(("delivery.prepared",))
            if e["payload"]["workspace_id"] == project.source_workspace_id
        }
        return [self.delivery.load(identity) for identity in sorted(identities)]

    def patch(self, delivery_id: str) -> str:
        value = self.delivery.load(delivery_id)
        verification = value["verification"]
        if not verification:
            raise ValueError("Delivery has no candidate verification")
        artifact = self.delivery._artifact(delivery_id, verification["patch_sha256"])
        from eventide.delivery import MAX_PATCH_BYTES

        with artifact.open("rb") as stream:
            body = stream.read(MAX_PATCH_BYTES + 1)
        if len(body) > MAX_PATCH_BYTES:
            raise ValueError("Patch exceeds the delivery size limit")
        import hashlib

        if hashlib.sha256(body).hexdigest() != verification["patch_sha256"]:
            raise ValueError("Patch artifact changed")
        return body.decode("utf-8", errors="replace")


def validate_writer_parent(
    host: RuntimeHost,
    parent_run_id: str,
    workspace_id: str,
    config: dict[str, Any],
) -> None:
    if not is_delivery_control_run(host.store, parent_run_id):
        raise ValueError("Writer requires a trusted delivery controller")
    intent = host._admitted_intent(parent_run_id)
    assert intent is not None
    binding = intent["execution_binding"]
    if (
        binding["operation"] != "repair"
        or parent_run_id not in host.dispatch._leases
        or parent_run_id not in host._run_tasks
        or host.store.run_is_terminal(parent_run_id)
        or config.get("writer_delivery_id") != binding["delivery_id"]
    ):
        raise ValueError("Writer parent has no live root ownership")
    value = DeliveryController(host).load(binding["delivery_id"])
    if host.store.get_workspace(workspace_id).path != value["receipt"]["checkout"]:
        raise ValueError("Writer checkout does not belong to its delivery")


async def start_owned_writer(
    host: RuntimeHost,
    parent_run_id: str,
    session: str,
    run_id: str,
    request: RunRequest,
    config: dict[str, Any],
) -> asyncio.Task[RunResult]:
    workspace = host.sessions.workspace(session)
    validate_writer_parent(host, parent_run_id, workspace.workspace_id, config)
    await host._prepare_run(
        session, run_id, request, execution_config=config, parent_run_id=parent_run_id
    )

    async def execute() -> RunResult:
        async with (
            host._session_execution(session, run_id),
            host._workspace_execution(workspace.workspace_id, run_id),
        ):
            validate_writer_parent(host, parent_run_id, workspace.workspace_id, config)
            accepted = host.store.get_run(run_id)
            observed = await host._admission_check(host._workspace_checkpoint, Path(workspace.path))
            if not accepted or observed != accepted["checkpoint"]:
                raise ValueError("Writer changed after admission")
            return await host._execute(
                session,
                run_id,
                workspace,
                request,
                None,
                None,
                None,
                existing_turn_id=accepted["turn_id"],
                execution_config=config,
            )

    try:
        task = asyncio.create_task(execute())
    except BaseException as exc:
        host._finish_admitted(run_id, exc)
        host._release_admission(run_id)
        raise
    host._track_run_task(run_id, task)
    return task
