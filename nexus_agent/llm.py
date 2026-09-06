"""Anthropic client wrapper with retry, fallback, and recovery state."""

import random
import time

import os

from nexus_agent.config import (
    BASE_DELAY_MS,
    MAX_RETRIES,
    MAX_CONSECUTIVE_529,
    PRIMARY_MODEL,
)


class RecoveryState:
    """Tracks per-conversation recovery decisions."""
    def __init__(self):
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_529 = 0
        self.has_attempted_reactive_compact = False
        self.current_model = PRIMARY_MODEL


def retry_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at 32 seconds."""
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    return base + random.uniform(0, base * 0.25)


def with_retry(fn, state: RecoveryState):
    """Call fn() with retries for rate limits and overloaded errors."""
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_529 = 0
            return result
        except Exception as exc:
            name = type(exc).__name__.lower()
            msg = str(exc).lower()
            if "ratelimit" in name or "429" in msg:
                delay = retry_delay(attempt)
                print(f"  \033[33m[429] retry {attempt + 1}/{MAX_RETRIES} "
                      f"after {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue
            if "overloaded" in name or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                fallback = os.getenv("FALLBACK_MODEL_ID")
                if state.consecutive_529 >= MAX_CONSECUTIVE_529 and fallback:
                    state.current_model = fallback
                    state.consecutive_529 = 0
                    print(f"  \033[31m[529] switching to {fallback}\033[0m")
                delay = retry_delay(attempt)
                print(f"  \033[33m[529] retry {attempt + 1}/{MAX_RETRIES} "
                      f"after {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue
            raise
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")


def is_prompt_too_long_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        ("prompt" in msg and "long" in msg)
        or "context_length_exceeded" in msg
        or "max_context_window" in msg
    )
