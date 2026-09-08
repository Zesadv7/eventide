"""Configuration and backwards-compatible lazy client access."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from nexus_agent.workspace import user_state_dir

load_dotenv(override=True)

_PROVIDER_ALIASES = {"openai": "openai_compatible"}
SUPPORTED_PROVIDERS = frozenset({"anthropic", "openai_compatible", "openai_responses"})
DEFAULT_MODEL = "claude-sonnet-5"


def normalize_provider(name: str) -> str:
    """把 provider 标识规范化为稳定的下划线形式（含别名映射）。"""
    normalized = name.strip().lower().replace("-", "_")
    return _PROVIDER_ALIASES.get(normalized, normalized)


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings loaded without constructing a network client."""

    workdir: Path
    state_dir: Path
    provider: str
    api_key: str | None
    base_url: str | None
    model: str
    fallback_model: str | None
    max_tokens: int = 8_000
    max_steps: int = 30
    context_limit: int = 50_000
    approval_timeout: float = 60.0

    @classmethod
    def from_env(cls, workdir: Path | None = None) -> Settings:
        root = (workdir or Path.cwd()).resolve()
        provider = normalize_provider(os.getenv("NEXUS_PROVIDER", "anthropic"))
        api_key = os.getenv("NEXUS_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        base_url = os.getenv("NEXUS_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL")
        model = os.getenv("NEXUS_MODEL") or os.getenv("MODEL_ID") or DEFAULT_MODEL
        state_value = Path(os.getenv("NEXUS_STATE_DIR", str(user_state_dir())))
        state_dir = state_value if state_value.is_absolute() else root / state_value
        return cls(
            workdir=root,
            state_dir=state_dir.resolve(),
            provider=provider,
            api_key=api_key,
            base_url=base_url or None,
            model=model,
            fallback_model=os.getenv("NEXUS_FALLBACK_MODEL") or os.getenv("FALLBACK_MODEL_ID"),
            max_tokens=int(os.getenv("NEXUS_MAX_TOKENS", "8000")),
            max_steps=int(os.getenv("NEXUS_MAX_STEPS", "30")),
            context_limit=int(os.getenv("NEXUS_CONTEXT_LIMIT", "50000")),
            approval_timeout=float(os.getenv("NEXUS_APPROVAL_TIMEOUT", "60")),
        )

    def require_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "No model API key configured. Set NEXUS_API_KEY (preferred) "
                "or ANTHROPIC_API_KEY. Offline eval and --help do not need a key."
            )
        return self.api_key


WORKDIR = Path.cwd().resolve()
TASKS_DIR = WORKDIR / ".tasks"
WORKTREES_DIR = WORKDIR / ".worktrees"
MAILBOX_DIR = WORKDIR / ".mailboxes"
MEMORY_DIR = WORKDIR / ".memory"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
SKILLS_DIR = WORKDIR / "skills"

MODEL = os.getenv("NEXUS_MODEL") or os.getenv("MODEL_ID", DEFAULT_MODEL)
PRIMARY_MODEL = MODEL
FALLBACK_MODEL = os.getenv("NEXUS_FALLBACK_MODEL") or os.getenv("FALLBACK_MODEL_ID")
DEFAULT_MAX_TOKENS = 8_000
ESCALATED_MAX_TOKENS = 16_000
MAX_RETRIES = 3
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
CONTEXT_LIMIT = 50_000
KEEP_RECENT_TOOL_RESULTS = 3
PERSIST_THRESHOLD = 30_000
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."
PROMPT = "\033[36mnexus >> \033[0m"
CLI_ACTIVE = False


_legacy_client: Any | None = None


def get_anthropic_client() -> Any:
    """Construct the legacy synchronous Anthropic client only when used."""
    global _legacy_client
    if _legacy_client is None:
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install Nexus Agent dependencies before live model use") from exc
        settings = Settings.from_env()
        _legacy_client = Anthropic(
            api_key=settings.require_api_key(),
            base_url=settings.base_url,
        )
    return _legacy_client


class _LazyClient:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_anthropic_client(), name)


client = _LazyClient()
