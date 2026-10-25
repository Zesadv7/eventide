"""Persistent in-run user interactions on the single Event Log.

Supplements are received only through the trusted local HTTP path and are
consumed exactly once at the next safe model boundary of their own run.
Executed tools are never rewound; a supplement that arrives after a terminal
run outcome is recorded as ``late`` and never resumes or schedules anything.
"""

from typing import Any

SUPPLEMENT_EVENT = "interaction.supplement"
SUPPLEMENT_CONSUMED_EVENT = "interaction.supplement.consumed"
SUPPLEMENT_MESSAGE_PREFIX = "[User supplement for the running task]"

ACTIVE_RUN_STATUSES = {"queued", "running", "waiting_for_user"}
SUPPLEMENT_BODY_LIMIT = 8000


def normalize_supplement_body(body: str) -> str:
    text = body.strip()
    if not text:
        raise ValueError("Supplement body must not be empty")
    if len(text) > SUPPLEMENT_BODY_LIMIT:
        raise ValueError(
            f"Supplement body exceeds {SUPPLEMENT_BODY_LIMIT} characters"
        )
    return text


def supplement_status(run_status: str) -> str:
    """Receipt status at receive time; late supplements never auto-resume."""
    return "pending" if run_status in ACTIVE_RUN_STATUSES else "late"


def supplement_message(body: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": f"{SUPPLEMENT_MESSAGE_PREFIX}\n{body}",
    }


def merge_supplements(
    receipts: list[dict[str, Any]], consumed: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Project receipt status; consumption is append-only and never re-derived."""
    projected: list[dict[str, Any]] = []
    for receipt in receipts:
        supplement_id = receipt["supplement_id"]
        record = {**receipt, "status": receipt.get("status", "pending")}
        if supplement_id in consumed:
            record["status"] = "consumed"
            record["consumed_step"] = consumed[supplement_id].get("step")
        projected.append(record)
    return projected
