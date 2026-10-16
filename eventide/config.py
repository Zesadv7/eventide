"""Runtime configuration without process-global clients or workspace state."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from eventide.workspace import user_state_dir

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
    max_tokens: int = 8_000
    max_steps: int = 30
    task_max_steps: int = 12
    context_limit: int = 50_000
    approval_timeout: float = 60.0
    model_timeout: float = 300.0
    mcp_timeout: float = 30.0
    command_timeout: float = 120.0
    run_token_limit: int = 0
    workers_enabled: bool = False
    embedding_provider: str = "hash"
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_dim: int = 256
    embedding_model_path: Path | None = None
    reranker_model_path: Path | None = None
    local_proxy_peers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.run_token_limit < 0:
            raise ValueError("run_token_limit must be zero or positive")
        if not isinstance(self.local_proxy_peers, tuple) or len(self.local_proxy_peers) > 32:
            raise ValueError("local_proxy_peers must be a tuple of at most 32 exact IP addresses")
        peers = []
        for value in self.local_proxy_peers:
            try:
                if not isinstance(value, str) or "%" in value:
                    raise ValueError
                address = ipaddress.ip_address(value)
                if address.is_unspecified or address.is_multicast:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("local_proxy_peers requires exact unicast IP addresses") from exc
            peers.append(str(address))
        if len(set(peers)) != len(peers):
            raise ValueError("local_proxy_peers contains duplicate IP addresses")
        object.__setattr__(self, "local_proxy_peers", tuple(peers))

    @classmethod
    def from_env(cls, workdir: Path | None = None) -> Settings:
        root = (workdir or Path.cwd()).resolve()
        provider = normalize_provider(os.getenv("EVENTIDE_PROVIDER", "anthropic"))
        api_key = os.getenv("EVENTIDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        base_url = os.getenv("EVENTIDE_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL")
        model = os.getenv("EVENTIDE_MODEL") or os.getenv("MODEL_ID") or DEFAULT_MODEL
        state_value = Path(os.getenv("EVENTIDE_STATE_DIR", str(user_state_dir())))
        state_dir = state_value if state_value.is_absolute() else root / state_value
        peers_value = os.getenv("EVENTIDE_LOCAL_PROXY_PEERS", "").strip()
        return cls(
            workdir=root,
            state_dir=state_dir.resolve(),
            provider=provider,
            api_key=api_key,
            base_url=base_url or None,
            model=model,
            max_tokens=int(os.getenv("EVENTIDE_MAX_TOKENS", "8000")),
            max_steps=int(os.getenv("EVENTIDE_MAX_STEPS", "30")),
            task_max_steps=int(os.getenv("EVENTIDE_TASK_MAX_STEPS", "12")),
            context_limit=int(os.getenv("EVENTIDE_CONTEXT_LIMIT", "50000")),
            approval_timeout=float(os.getenv("EVENTIDE_APPROVAL_TIMEOUT", "60")),
            model_timeout=float(os.getenv("EVENTIDE_MODEL_TIMEOUT", "300")),
            mcp_timeout=float(os.getenv("EVENTIDE_MCP_TIMEOUT", "30")),
            command_timeout=float(os.getenv("EVENTIDE_COMMAND_TIMEOUT", "120")),
            run_token_limit=int(os.getenv("EVENTIDE_RUN_TOKEN_LIMIT", "0")),
            workers_enabled=os.getenv("EVENTIDE_WORKERS", "0").lower() in {"1", "true"},
            embedding_provider=os.getenv("EVENTIDE_EMBEDDING_PROVIDER", "hash"),
            embedding_model=os.getenv("EVENTIDE_EMBEDDING_MODEL") or None,
            embedding_base_url=os.getenv("EVENTIDE_EMBEDDING_BASE_URL") or None,
            embedding_api_key=(
                os.getenv("EVENTIDE_EMBEDDING_API_KEY")
                or os.getenv("EVENTIDE_API_KEY")
                or None
            ),
            embedding_dim=int(os.getenv("EVENTIDE_EMBEDDING_DIM", "256")),
            embedding_model_path=(
                Path(os.environ["EVENTIDE_EMBEDDING_MODEL_PATH"])
                if os.getenv("EVENTIDE_EMBEDDING_MODEL_PATH") else None
            ),
            reranker_model_path=(
                Path(os.environ["EVENTIDE_RERANKER_MODEL_PATH"])
                if os.getenv("EVENTIDE_RERANKER_MODEL_PATH") else None
            ),
            local_proxy_peers=(
                tuple(value.strip() for value in peers_value.split(",")) if peers_value else ()
            ),
        )

    def require_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "No model API key configured. Set EVENTIDE_API_KEY (preferred) "
                "or ANTHROPIC_API_KEY. Offline eval and --help do not need a key."
            )
        return self.api_key
