"""Bounded local Skill package management; no installation scripts are run."""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventide.http_api.context import ApiContext, require_local_request
from eventide.http_control import (
    MUTATION_OUTCOME_HEADER,
    begin_mutation,
    fail_mutation,
    finish_mutation,
)
from eventide.skills import bundled_skill_directories
from eventide.workspace import digest


class PackageBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    directory: str | None = Field(default=None, min_length=1, max_length=4096)
    archive_base64: str | None = Field(default=None, max_length=14_000_000)
    bundled: str | None = Field(default=None, max_length=64)
    scope: Literal["user", "project"] = "user"
    expected_version: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def exactly_one_source(self) -> PackageBody:
        if (
            sum(
                value is not None
                for value in (
                    self.directory,
                    self.archive_base64,
                    self.bundled,
                )
            )
            != 1
        ):
            raise ValueError("Choose one local directory, archive, or bundled Skill")
        return self

    def source(self) -> tuple[Path | bytes, bool]:
        if self.directory is not None:
            return Path(self.directory).expanduser(), False
        if self.bundled is not None:
            directory = next(
                (p for p in bundled_skill_directories() if p.name == self.bundled), None
            )
            if directory is None:
                raise ValueError("Bundled Skill not found")
            return directory, False
        try:
            return base64.b64decode(self.archive_base64 or "", validate=True), True
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid base64 archive") from exc


SkillId = Annotated[str, Field(min_length=1, max_length=80)]


class SkillConfigurationBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    expected_configuration_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    enabled_user: dict[SkillId, Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]] = Field(
        default_factory=dict,
        max_length=64,
    )
    disabled_project: list[SkillId] = Field(default_factory=list, max_length=64)
    selected: list[SkillId] = Field(default_factory=list, max_length=64)


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(tags=["skills"])
    runtime = context.runtime

    @router.get("/api/skills/bundled")
    async def bundled() -> list[dict[str, Any]]:
        return [
            runtime.skills.packages.preview_directory(p).as_dict()
            for p in bundled_skill_directories()
        ]

    @router.get("/api/workspaces/{workspace_id}/skills")
    async def status(workspace_id: str) -> dict[str, Any]:
        await context.get_workspace(workspace_id)
        return runtime.skill_status(workspace_id)

    @router.post("/api/workspaces/{workspace_id}/skills/preview")
    async def preview(workspace_id: str, body: PackageBody, request: Request) -> dict[str, Any]:
        require_local_request(request)
        await context.get_workspace(workspace_id)
        try:
            source, zipped = body.source()
            return await runtime.preview_skill(source, zipped=zipped)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/api/workspaces/{workspace_id}/skills/import")
    async def import_package(
        workspace_id: str, body: PackageBody, request: Request
    ) -> dict[str, Any]:
        require_local_request(request)
        await context.get_workspace(workspace_id)
        receipt = begin_mutation(
            runtime, request, runtime._skill_owner(workspace_id), "skills.import", body.model_dump()
        )
        if receipt.result is not None:
            return receipt.result
        try:
            source, zipped = body.source()
            # Reject only during this read-only preflight. The actual import can
            # publish a directory before its canonical installation fact commits.
            preview = await runtime.preview_skill(source, zipped=zipped)
            if not preview["valid"]:
                raise ValueError("; ".join(d["message"] for d in preview["diagnostics"]))
            if body.expected_version is not None and preview["version"] != body.expected_version:
                raise ValueError("Skill changed since preview; review it again")
            if body.scope == "project":
                if runtime.workspace_busy(workspace_id):
                    raise ValueError("Workspace is busy")
                root = Path(runtime.store.get_workspace(workspace_id).path) / "skills"
                if root.is_symlink():
                    raise ValueError("Project skills directory cannot be a symlink")
                target = root / preview["name"]
                if target.exists() or target.is_symlink():
                    raise ValueError("Project Skill already exists; manage updates with Git")
        except (ValueError, OSError) as exc:
            code = 409 if "busy" in str(exc) or "already exists" in str(exc) else 422
            fail_mutation(runtime, receipt, str(exc), code, rejected=True)
            raise HTTPException(
                code, str(exc), headers={MUTATION_OUTCOME_HEADER: "rejected"},
            ) from exc
        try:
            result = await runtime.import_skill(
                workspace_id, source, zipped=zipped, scope=body.scope,
                expected_version=preview["version"],
            )
            return finish_mutation(runtime, receipt, result)
        except Exception:
            # Keep the started receipt. A retry may inspect it but cannot publish
            # again, including after cancellation, restart or a lost success reply.
            raise HTTPException(
                503, "Skill import outcome is unknown; "
                "inspect Skills and retain the original request",
            ) from None

    @router.put("/api/workspaces/{workspace_id}/skills/configuration")
    async def configure(
        workspace_id: str,
        body: SkillConfigurationBody,
        request: Request,
    ) -> dict[str, Any]:
        require_local_request(request)
        await context.get_workspace(workspace_id)
        receipt = begin_mutation(
            runtime,
            request,
            runtime._skill_owner(workspace_id),
            "skills.configure",
            body.model_dump(),
        )
        if receipt.result is not None:
            return receipt.result
        if body.expected_configuration_hash is not None and (
            digest(runtime.skills.configuration(workspace_id)) != body.expected_configuration_hash
        ):
            message = "Skill configuration changed; refresh before saving"
            fail_mutation(runtime, receipt, message, 409)
            raise HTTPException(409, message)
        try:
            result = runtime.configure_skills(
                workspace_id, **body.model_dump(exclude={"expected_configuration_hash"})
            )
        except ValueError as exc:
            fail_mutation(runtime, receipt, str(exc))
            raise HTTPException(422, str(exc)) from exc
        return finish_mutation(runtime, receipt, result)

    @router.get("/api/runs/{run_id}/skills")
    async def run_skills(run_id: str) -> dict[str, Any]:
        if not runtime.store.get_run_identity(run_id):
            raise HTTPException(404, "Run not found")
        snapshot = None
        loaded = []
        for event in runtime.store.run_events(run_id):
            if event["type"] == "run.admitted":
                snapshot = event["payload"].get("skill_snapshot")
            elif event["type"] == "skills.snapshot":
                snapshot = event["payload"]
            elif event["type"] == "skill.loaded":
                loaded.append({**event["payload"], "seq": event["session_seq"]})
        return {"run_id": run_id, "snapshot": snapshot, "loaded": loaded}

    @router.get("/api/skills/{name}/versions/diff")
    async def diff(name: str, before: str, after: str) -> dict[str, Any]:
        try:
            return runtime.skills.diff(name, before, after)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/api/skills/{name}/versions/{version}/resource")
    async def resource(
        name: str,
        version: str,
        path: str,
        offset: int = 0,
        limit: int = 12_000,
    ) -> dict[str, Any]:
        try:
            text = runtime.skills.packages.read_resource(
                name, version, path, offset=offset, limit=limit
            )
            return {"name": name, "version": version, "path": path, "content": text}
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    return router
