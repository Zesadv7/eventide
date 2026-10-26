"""Read-only outcome projections over immutable Runtime facts."""

from __future__ import annotations

from typing import Any

EVENT = "tool.outcome_reconciled"


def verified_outcomes(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        event["payload"]["unknown_event_id"]: event["payload"]
        for event in events
        if event["type"] == EVENT
        and not event["partial"]
        and event["payload"]["status"] == "verified_after"
    }


def unresolved_outcomes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    verified = verified_outcomes(events)
    return [
        event
        for event in events
        if event["type"] == "tool.outcome_unknown"
        and not event["partial"]
        and not (
            event["event_id"] in verified
            and verified[event["event_id"]]["source_run_id"] == event["run_id"]
            and verified[event["event_id"]]["call_id"] == event["payload"]["call_id"]
        )
    ]
