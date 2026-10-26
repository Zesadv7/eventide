"""Read-only projection of one run's frozen tool directory (MK-16).

The projection reads only persisted run facts: the ``tool.catalog.frozen``
snapshot the Host wrote before the first model request and the ``tool.loaded`` /
``tool.load_failed`` receipts this module's own load path recorded. It never
re-enumerates the current MCP manager, so a restarted store or a changed MCP
server can never rewrite history: an old run keeps exactly the directory and
load facts it froze, and a run without frozen facts says so instead of
pretending.
"""

from __future__ import annotations

from typing import Any

FROZEN_EVENT = "tool.catalog.frozen"
LOADED_EVENT = "tool.loaded"
LOAD_FAILED_EVENT = "tool.load_failed"

_STATUS_FILTERS = {"all", "loaded", "unloaded", "failed"}
_NO_CATALOG_NOTE = (
    "此 Run 没有冻结工具目录事实（旧历史 Run，或当时没有按需工具目录）。"
    "投影只读持久事实，不会重新枚举当前 MCP 来伪造历史目录。"
)
_STALE_NOTE = (
    "存在目录版本不一致的加载事实；它们按各自冻结版本保留，不套用到当前目录。"
)


def project_run_tools(
    events: list[dict[str, Any]], run_id: str | None = None
) -> dict[str, Any]:
    """Fold run events into the high-water frozen catalog plus load facts."""
    frozen: dict[str, Any] | None = None
    frozen_seq: int | None = None
    load_records: list[dict[str, Any]] = []
    for event in events:
        event_type = event["type"]
        if event_type == FROZEN_EVENT:
            # One admission freezes one catalog; the last fact is the
            # high-water mark if a future path ever re-freezes.
            frozen = event["payload"]
            frozen_seq = event["session_seq"]
        elif event_type in (LOADED_EVENT, LOAD_FAILED_EVENT):
            load_records.append(
                {
                    "outcome": "loaded" if event_type == LOADED_EVENT else "failed",
                    "seq": event["session_seq"],
                    **event["payload"],
                }
            )

    entries: list[dict[str, Any]] = []
    catalog_version: str | None = None
    if frozen is not None:
        catalog_version = frozen.get("catalog_version")
        for entry in frozen.get("entries", []):
            entries.append(
                {
                    "name": entry["name"],
                    "source": entry["source"],
                    "version": entry["version"],
                    "description": entry.get("description", ""),
                    "effect": entry.get("effect", "effect"),
                    "environment": entry.get("environment", "local"),
                    "status": entry.get("status", "available"),
                    "disabled_reason": entry.get("disabled_reason"),
                    "provided": bool(entry.get("provided")),
                    "input_schema": entry.get("input_schema"),
                    "schema_status": entry.get("schema_status", "full"),
                    "schema_chars": entry.get("schema_chars"),
                    "loaded": False,
                    "loaded_seq": None,
                    "load_record": None,
                }
            )
    by_name = {entry["name"]: entry for entry in entries}

    stale_receipts = 0
    matched_receipts = 0
    failed_records: list[dict[str, Any]] = []
    for record in load_records:
        if record["outcome"] == "loaded":
            if record.get("catalog_version") != catalog_version:
                stale_receipts += 1
                continue
            entry = by_name.get(record.get("tool", ""))
            if entry is None or record.get("schema_digest") != entry["version"]:
                # A receipt that does not bind to the frozen entry is kept as
                # an unmatched fact below, never applied as a loaded state.
                stale_receipts += 1
                continue
            matched_receipts += 1
            entry["loaded"] = True
            entry["loaded_seq"] = record["seq"]
            entry["load_record"] = {
                key: record[key]
                for key in ("catalog_version", "schema_digest", "source", "effect")
                if key in record
            }
        else:
            failed_records.append(
                {
                    "tool": record.get("tool"),
                    "code": record.get("code"),
                    "reason": record.get("reason"),
                    "seq": record["seq"],
                    "catalog_version": record.get("catalog_version"),
                }
            )

    notes: list[str] = []
    if frozen is None:
        notes.append(_NO_CATALOG_NOTE)
    if stale_receipts:
        notes.append(_STALE_NOTE)
    return {
        "run_id": run_id,
        "catalog_frozen": frozen is not None,
        "catalog_version": catalog_version,
        "frozen_seq": frozen_seq,
        "entries": entries,
        "failed_loads": failed_records,
        "matched_loads": matched_receipts,
        "stale_receipts": stale_receipts,
        "notes": notes,
    }


def filter_run_tools(
    projection: dict[str, Any],
    *,
    query: str = "",
    source: str = "",
    status: str = "all",
    offset: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    """Bounded deterministic page over the projected directory."""
    if status not in _STATUS_FILTERS:
        raise ValueError("status must be one of all/loaded/unloaded/failed")
    needle = query.strip().lower()
    source_needle = source.strip().lower()

    def keep(entry: dict[str, Any]) -> bool:
        if needle and needle not in entry["name"].lower() and needle not in entry[
            "description"
        ].lower():
            return False
        if source_needle and source_needle not in entry["source"].lower():
            return False
        if status == "loaded" and not entry["loaded"]:
            return False
        if status == "unloaded" and (
            entry["loaded"] or entry["provided"] or entry["status"] != "available"
        ):
            return False
        if status == "failed":
            return any(
                failed.get("tool") == entry["name"] for failed in projection["failed_loads"]
            )
        return True

    matched = [entry for entry in projection["entries"] if keep(entry)]
    # List pages stay bounded: the precise parameter contract is served per
    # tool by the detail endpoint, not repeated on every row.
    page = [
        {key: value for key, value in entry.items() if key != "input_schema"}
        for entry in matched[offset : offset + limit]
    ]
    return {
        "run_id": projection["run_id"],
        "catalog_frozen": projection["catalog_frozen"],
        "catalog_version": projection["catalog_version"],
        "frozen_seq": projection["frozen_seq"],
        "total": len(matched),
        "catalog_total": len(projection["entries"]),
        "offset": offset,
        "limit": limit,
        "items": page,
        "loaded_count": sum(1 for entry in projection["entries"] if entry["loaded"]),
        "failed_loads": projection["failed_loads"],
        "stale_receipts": projection["stale_receipts"],
        "notes": projection["notes"],
    }


def run_tool_detail(projection: dict[str, Any], name: str) -> dict[str, Any] | None:
    """One entry with its frozen parameter contract; None when never frozen."""
    for entry in projection["entries"]:
        if entry["name"] == name:
            return entry
    return None
