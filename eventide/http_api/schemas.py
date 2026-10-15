"""HTTP request schemas shared by domain routers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from eventide.generation import GenerationCapabilities, GenerationPatch


class RunBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    mode: str = "auto"
    execution_target: Literal["source", "snapshot", "worktree"] = "source"
    attachment_ids: list[str] = Field(default_factory=list)
    model: str | None = Field(default=None, max_length=256)
    generation: GenerationPatch = Field(default_factory=GenerationPatch)
    selected_skills: list[str] | None = Field(default=None, max_length=64)


class GenerationBody(BaseModel):
    model: str = Field(min_length=1, max_length=256)
    generation: GenerationPatch = Field(default_factory=GenerationPatch)
    capabilities: GenerationCapabilities | None = None


class AttachmentBody(BaseModel):
    name: str = Field(min_length=1)
    content_base64: str = ""
    media_type: str | None = Field(default=None, max_length=128)


class SessionBody(BaseModel):
    workspace_id: str | None = None
    working_directory: str | None = Field(default=None, min_length=1)


class SessionUpdateBody(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=96)
    archived: bool | None = None


class WorkspaceBody(BaseModel):
    path: str = Field(min_length=1)


class ApprovalBody(BaseModel):
    approved: bool


class ProviderConfigBody(BaseModel):
    provider: Literal["anthropic", "openai_compatible", "openai-compatible", "openai_responses"]
    api_key: SecretStr | None = None
    base_url: str | None = Field(default=None, max_length=2_048)
    model: str = Field(min_length=1, max_length=256)
    clear_api_key: bool = False
