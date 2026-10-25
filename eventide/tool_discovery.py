"""Deterministic on-demand tool discovery (MK-16).

A run freezes one versioned catalog snapshot built from the tools the Host can
actually execute. Base tools stay advertised; large catalogs (MCP) are offered
through ``tool_discover`` / ``tool_load`` and only enter the model-visible
catalog after an explicit load. The catalog fingerprint binds loads to one
version: a rebuild after MCP reconnection produces a different version, so a
receipt can never silently re-enable a same-named different implementation.

Discovery is not authorization. Loading only unlocks a schema that was already
frozen in the snapshot; execution still passes ToolExecutor contracts and the
PolicyEngine on every call.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from typing import Any

from eventide.models import ToolOutput
from eventide.tool_contracts import builtin_schema

DISCOVER_TOOL = "tool_discover"
LOAD_TOOL = "tool_load"

_PAGE_LIMIT_MAX = 50


class ToolDiscoveryError(ValueError):
    """A discovery lookup or load was rejected with a stable reason code."""

    def __init__(self, code: str, message: str, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.extra = extra or {}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def schema_digest(schema: Any) -> str:
    return hashlib.sha256(_canonical(schema).encode("utf-8")).hexdigest()


def _mcp_server(name: str) -> str | None:
    parts = name.split("__")
    if len(parts) == 3 and parts[0] == "mcp":
        return parts[1]
    return None


def _entry(
    tool: dict[str, Any],
    *,
    readonly: bool,
    provided: bool,
    environment: str,
) -> dict[str, Any]:
    name = tool["name"]
    server = _mcp_server(name)
    return {
        "name": name,
        "source": f"mcp:{server}" if server else "builtin",
        "version": schema_digest(tool.get("input_schema")),
        "description": tool.get("description", ""),
        "effect": "readonly" if readonly else "effect",
        "environment": environment,
        "status": "available",
        "disabled_reason": None,
        "provided": provided,
    }


class ToolCatalog:
    """A frozen, deterministically ordered catalog snapshot."""

    def __init__(
        self,
        base_tools: list[dict[str, Any]],
        on_demand_tools: list[dict[str, Any]],
        *,
        readonly_names: set[str] | frozenset[str] = frozenset(),
        environment: str = "local",
    ):
        entries: dict[str, dict[str, Any]] = {}
        for tool in base_tools:
            name = tool["name"]
            if name in entries:
                raise ToolDiscoveryError(
                    "duplicate_tool_name", f"Duplicate tool name in catalog: {name}"
                )
            entries[name] = _entry(
                tool,
                readonly=name in readonly_names,
                provided=True,
                environment=environment,
            )
        for tool in on_demand_tools:
            name = tool["name"]
            if name in entries:
                raise ToolDiscoveryError(
                    "duplicate_tool_name", f"Duplicate tool name in catalog: {name}"
                )
            entries[name] = _entry(
                tool,
                readonly=name in readonly_names,
                provided=False,
                environment=environment,
            )
        self._ordered = [entries[name] for name in sorted(entries)]
        self._entries = entries
        self._schemas = {
            tool["name"]: copy.deepcopy(tool.get("input_schema"))
            for tool in [*base_tools, *on_demand_tools]
        }
        fingerprint_source = [
            {
                "name": item["name"],
                "source": item["source"],
                "version": item["version"],
                "effect": item["effect"],
                "environment": item["environment"],
                "status": item["status"],
            }
            for item in self._ordered
        ]
        self.catalog_version = hashlib.sha256(
            _canonical(fingerprint_source).encode("utf-8")
        ).hexdigest()

    def __len__(self) -> int:
        return len(self._ordered)

    @property
    def on_demand_names(self) -> list[str]:
        return [item["name"] for item in self._ordered if not item["provided"]]

    def search(self, query: str, offset: int, limit: int) -> dict[str, Any]:
        """Deterministic substring search with stable rank and pagination."""
        needle = query.strip().lower()
        if needle:

            def rank(item: dict[str, Any]) -> tuple[int, str]:
                name = item["name"].lower()
                description = item["description"].lower()
                if name == needle:
                    return (0, item["name"])
                if name.startswith(needle):
                    return (1, item["name"])
                if needle in name:
                    return (2, item["name"])
                return (3, item["name"]) if needle in description else (4, item["name"])

            matched = sorted((item for item in self._ordered if rank(item)[0] < 4), key=rank)
        else:
            matched = list(self._ordered)
        page = matched[offset : offset + limit]
        return {
            "catalog_version": self.catalog_version,
            "total": len(matched),
            "offset": offset,
            "limit": limit,
            "items": [copy.deepcopy(item) for item in page],
        }

    def detail(self, name: str) -> dict[str, Any]:
        if name not in self._entries:
            raise ToolDiscoveryError("tool_not_found", f"Unknown tool: {name}", {"name": name})
        entry = copy.deepcopy(self._entries[name])
        entry["input_schema"] = copy.deepcopy(self._schemas[name])
        return entry

    def schema_for(self, name: str) -> dict[str, Any] | None:
        return self._schemas.get(name)


class ToolDiscovery:
    """Run-scoped discovery binding: frozen catalog plus loaded receipts."""

    def __init__(
        self,
        base_tools: list[dict[str, Any]],
        on_demand_tools: list[dict[str, Any]],
        on_demand_handlers: dict[str, Callable[..., Any]],
        *,
        readonly_names: set[str] | frozenset[str] = frozenset(),
        environment: str = "local",
    ):
        self.catalog = ToolCatalog(
            base_tools,
            on_demand_tools,
            readonly_names=readonly_names,
            environment=environment,
        )
        self._on_demand_handlers = dict(on_demand_handlers)
        self.loaded: dict[str, dict[str, Any]] = {}
        self._executor: Any = None

    # -- host wiring -------------------------------------------------------
    def bind_executor(self, executor: Any) -> None:
        """Give loads a path to unlock their frozen schema in the run executor."""
        self._executor = executor

    def model_tools(self) -> list[dict[str, Any]]:
        if not self.catalog.on_demand_names:
            return []
        return [
            {
                "name": DISCOVER_TOOL,
                "description": DISCOVER_DESCRIPTION,
                "input_schema": builtin_schema(DISCOVER_TOOL),
            },
            {
                "name": LOAD_TOOL,
                "description": LOAD_DESCRIPTION,
                "input_schema": builtin_schema(LOAD_TOOL),
            },
            *[
                {
                    "name": name,
                    "description": self.catalog._entries[name]["description"],
                    "input_schema": self.catalog.schema_for(name),
                }
                for name in sorted(self.loaded)
            ],
        ]

    def handlers(self) -> dict[str, Callable[..., Any]]:
        if not self.catalog.on_demand_names:
            return {}
        return {
            DISCOVER_TOOL: self._handle_discover,
            LOAD_TOOL: self._handle_load,
            **{
                name: self._on_demand_handlers[name]
                for name in sorted(self.loaded)
                if name in self._on_demand_handlers
            },
        }

    # -- model-facing handlers ---------------------------------------------
    async def _handle_discover(
        self, query: str = "", offset: int = 0, limit: int = 20
    ) -> ToolOutput:
        page = self.catalog.search(query, offset, limit)
        return ToolOutput(
            json.dumps(page, ensure_ascii=False, indent=2),
            False,
            {
                "code": "tool_catalog_page",
                "catalog_version": page["catalog_version"],
                "total": page["total"],
                "offset": page["offset"],
                "returned": len(page["items"]),
            },
        )

    async def _handle_load(
        self, name: str, expected_catalog_version: str | None = None
    ) -> ToolOutput:
        try:
            receipt = self.load(name, expected_catalog_version)
        except ToolDiscoveryError as exc:
            payload: dict[str, Any] = {"code": exc.code, "reason": str(exc), **exc.extra}
            return ToolOutput(
                json.dumps({"error": payload}, ensure_ascii=False, indent=2),
                True,
                payload,
            )
        return ToolOutput(
            json.dumps(receipt, ensure_ascii=False, indent=2),
            False,
            {
                "code": "tool_loaded",
                "tool": receipt["tool"],
                "catalog_version": receipt["catalog_version"],
                "schema_digest": receipt["schema_digest"],
            },
        )

    # -- core load path ------------------------------------------------------
    def load(self, name: str, expected_catalog_version: str | None = None) -> dict[str, Any]:
        if expected_catalog_version is not None and (
            expected_catalog_version != self.catalog.catalog_version
        ):
            raise ToolDiscoveryError(
                "catalog_version_mismatch",
                "Catalog version changed; re-run tool_discover before loading. "
                f"Expected {expected_catalog_version}, current {self.catalog.catalog_version}.",
                {"expected": expected_catalog_version, "current": self.catalog.catalog_version},
            )
        if name not in self.catalog._entries:
            raise ToolDiscoveryError(
                "tool_not_found",
                f"Unknown tool: {name}. Use tool_discover to list the catalog.",
                {"name": name},
            )
        entry = self.catalog._entries[name]
        if entry["status"] != "available":
            raise ToolDiscoveryError(
                "tool_unavailable",
                f"Tool {name} is not available: {entry['disabled_reason']}",
                {"name": name, "disabled_reason": entry["disabled_reason"]},
            )
        if entry["provided"]:
            raise ToolDiscoveryError(
                "already_provided",
                f"Tool {name} is a base tool and is already advertised; call it directly.",
                {"name": name},
            )
        if name not in self._on_demand_handlers:
            raise ToolDiscoveryError(
                "handler_unavailable",
                f"Tool {name} has no executable handler in this run.",
                {"name": name},
            )
        schema = self.catalog.schema_for(name)
        receipt = {
            "tool": name,
            "catalog_version": self.catalog.catalog_version,
            "schema_digest": schema_digest(schema),
            "source": entry["source"],
            "effect": entry["effect"],
            "environment": entry["environment"],
            "input_schema": copy.deepcopy(schema),
        }
        self.loaded[name] = receipt
        if self._executor is not None:
            self._executor.register_discovered_tool(
                name,
                schema,
                self._on_demand_handlers[name],
                readonly=entry["effect"] == "readonly",
            )
        return copy.deepcopy(receipt)


DISCOVER_DESCRIPTION = (
    "Search the versioned tool catalog (name, source, effect, availability). "
    "Returns a bounded page; empty query browses the full catalog. Loading a "
    "tool with tool_load is required before an on-demand tool can be called."
)
LOAD_DESCRIPTION = (
    "Load one on-demand tool from the catalog and return its precise input "
    "schema receipt bound to the catalog version. Pass expected_catalog_version "
    "from the discovery page to fail instead of loading against a changed "
    "catalog. Loading does not grant permission; execution is still policy "
    "checked."
)
