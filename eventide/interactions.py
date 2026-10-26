"""Persistent in-run user interactions on the single Event Log.

Supplements are received only through the trusted local HTTP path and are
consumed exactly once at the next safe model boundary of their own run.
Executed tools are never rewound; a supplement that arrives after a terminal
run outcome is recorded as ``late`` and never resumes or schedules anything.
"""

import hashlib
import json
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


QUESTION_OPENED_EVENT = "interaction.question.opened"
QUESTION_ANSWERED_EVENT = "interaction.question.answered"
QUESTION_CLOSED_EVENT = "interaction.question.closed"

QUESTION_COUNT_LIMIT = 3
QUESTION_TEXT_LIMIT = 500
QUESTION_CHOICE_LIMIT = 6
QUESTION_CHOICE_TEXT_LIMIT = 200
ANSWER_TEXT_LIMIT = 4000
QUESTION_REVISION = 1

# Scope choice for MK06: ask_user serves live main-agent runs (auto/agent).
# Plan mode and read-only workers stay question-free; planning-time
# questioning is a separate decision, not claimed as supported here.
ASK_USER_TOOL: dict[str, Any] = {
    "name": "ask_user",
    "description": (
        "Ask the user up to 3 short clarifying questions and wait for their "
        "answer. Use this only when a concrete, blocking detail is missing; "
        "each question may offer short choices and may allow free text. The "
        "run pauses while waiting and the answer returns here as the tool "
        "result. An answer is never a tool-permission approval."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": QUESTION_COUNT_LIMIT,
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": QUESTION_TEXT_LIMIT,
                        },
                        "choices": {
                            "type": "array",
                            "minItems": 0,
                            "maxItems": QUESTION_CHOICE_LIMIT,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": QUESTION_CHOICE_TEXT_LIMIT,
                            },
                        },
                        "allow_free_text": {"type": "boolean"},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    },
}


def normalize_questions(questions: object) -> list[dict[str, Any]]:
    """Validate the ask_user argument into the persisted question shape.

    The executor schema check already rejected malformed input; this bound is
    the single authority for what enters the Event Log.
    """
    if not isinstance(questions, list) or not 1 <= len(questions) <= QUESTION_COUNT_LIMIT:
        raise ValueError(f"ask_user requires 1-{QUESTION_COUNT_LIMIT} questions")
    normalized: list[dict[str, Any]] = []
    for item in questions:
        if not isinstance(item, dict):
            raise ValueError("Each question must be an object")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Question text must not be empty")
        if len(text) > QUESTION_TEXT_LIMIT:
            raise ValueError(f"Question text exceeds {QUESTION_TEXT_LIMIT} characters")
        raw_choices = item.get("choices", [])
        if not isinstance(raw_choices, list) or len(raw_choices) > QUESTION_CHOICE_LIMIT:
            raise ValueError(f"choices must hold at most {QUESTION_CHOICE_LIMIT} options")
        choices: list[str] = []
        for choice in raw_choices:
            if not isinstance(choice, str) or not choice.strip():
                raise ValueError("Question choices must be non-empty strings")
            if len(choice) > QUESTION_CHOICE_TEXT_LIMIT:
                raise ValueError(
                    f"Question choice exceeds {QUESTION_CHOICE_TEXT_LIMIT} characters"
                )
            if choice in choices:
                raise ValueError("Question choices must be distinct")
            choices.append(choice)
        allow_free = item.get("allow_free_text")
        if allow_free is None:
            allow_free = not choices
        if not isinstance(allow_free, bool):
            raise ValueError("allow_free_text must be a boolean")
        if not choices and not allow_free:
            raise ValueError("A question without choices must allow free text")
        normalized.append(
            {
                "text": text,
                "choices": choices,
                "allow_free_text": allow_free,
            }
        )
    return normalized


ANSWER_FIELDS = frozenset({"choice", "text"})


def normalize_answers(
    opened: dict[str, Any], answers: object
) -> list[dict[str, Any]]:
    """Validate a user answer against the frozen opened question payload.

    Only declared fields, offered choices, and the frozen free-text permission
    are accepted; free text is rejected whenever the opened question withheld
    it, regardless of whether a choice was also picked.
    """
    questions = opened.get("questions", [])
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise ValueError(
            f"Expected {len(questions)} answers, one per question"
        )
    normalized: list[dict[str, Any]] = []
    for question, answer in zip(questions, answers, strict=True):
        if not isinstance(answer, dict):
            raise ValueError("Each answer must be an object")
        unknown = set(answer) - ANSWER_FIELDS
        if unknown:
            raise ValueError(f"Unknown answer fields: {sorted(unknown)}")
        choice = answer.get("choice")
        text = answer.get("text")
        if choice is not None and (
            not isinstance(choice, str) or choice not in question.get("choices", [])
        ):
            raise ValueError("Answer choice must be one of the offered options")
        if text is not None:
            if not isinstance(text, str):
                raise ValueError("Answer text must be a string")
            if len(text) > ANSWER_TEXT_LIMIT:
                raise ValueError(f"Answer text exceeds {ANSWER_TEXT_LIMIT} characters")
            if not text.strip():
                text = None
        if choice is None and text is None:
            raise ValueError("Each answer needs a choice or free text")
        if text is not None and not question.get("allow_free_text", False):
            raise ValueError("This question does not accept free text")
        normalized.append({"choice": choice, "text": text})
    return normalized


def answer_digest(answers: list[dict[str, Any]]) -> str:
    """Stable content identity for idempotent answer replay detection."""
    canonical = json.dumps(answers, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def question_projected_status(
    record: dict[str, Any], run_status: str
) -> str:
    """One answered or closed fact wins; only live runs keep a question waiting."""
    if record.get("answer_status"):
        return record["answer_status"]
    if record.get("closed_reason"):
        return "closed"
    if run_status in ACTIVE_RUN_STATUSES:
        return "waiting"
    return "expired"


def merge_questions(records: list[dict[str, Any]], run_status: str) -> list[dict[str, Any]]:
    for record in records:
        record["status"] = question_projected_status(record, run_status)
    return records
