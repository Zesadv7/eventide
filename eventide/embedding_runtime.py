"""Remote embeddings share the Host's model slots and canonical attempt budget.

The synchronous developer adapter is not used here: hidden SDK retries and
background-thread cancellation must not escape a production Run's ownership.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from eventide.budgets import BudgetExceeded
from eventide.config import Settings
from eventide.normalization import normalize

if TYPE_CHECKING:
    from eventide.host import RuntimeHost


class EmbeddingRequestError(RuntimeError):
    """A stable, credential-free remote request failure."""


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def embedding_usage(value: Any) -> dict[str, int]:
    """OpenAI-compatible embeddings report prompt_tokens, not generation output.

    total_tokens is a duplicate total, never an additional charge or a substitute
    for an absent prompt counter. An inconsistent total cannot settle the bill.
    """
    count = _field(value, "prompt_tokens")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return {}
    total = _field(value, "total_tokens")
    if total is not None and (
        not isinstance(total, int) or isinstance(total, bool) or total != count
    ):
        return {}
    return {"input_tokens": count}


def _vectors(response: Any, count: int) -> list[list[float]]:
    data = _field(response, "data")
    if not isinstance(data, (list, tuple)) or len(data) != count:
        raise EmbeddingRequestError("Embedding response count does not match the request")
    ordered: dict[int, list[float]] = {}
    dimension = None
    for item in data:
        index = _field(item, "index")
        vector = _field(item, "embedding")
        if (type(index) is not int or not 0 <= index < count or index in ordered
                or not isinstance(vector, (list, tuple)) or not vector):
            raise EmbeddingRequestError("Embedding response has invalid indices or vectors")
        if any(isinstance(number, bool) or not isinstance(number, (int, float))
               or not math.isfinite(number) for number in vector):
            raise EmbeddingRequestError("Embedding response contains invalid vector values")
        if dimension is not None and len(vector) != dimension:
            raise EmbeddingRequestError("Embedding response dimensions are inconsistent")
        dimension = len(vector)
        ordered[index] = [float(number) for number in vector]
    return [ordered[index] for index in range(count)]


def _client(settings: Settings) -> Any:
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        api_key=settings.embedding_api_key, base_url=settings.embedding_base_url,
        timeout=settings.model_timeout, max_retries=0,
    )


async def embed_remote(
    host: RuntimeHost, run_id: str, texts: list[str], *, settings: Settings | None = None,
) -> list[list[float]]:
    """Send one explicit attempt; callers already own the active Run and tool policy.

    Cancellation waits for the actual asynchronous client close. No retry occurs;
    errors preserve any returned usage, even when the vectors cannot be consumed.
    """
    selected = settings or host.settings
    if selected.embedding_provider.strip().lower().replace("-", "_") != "openai_compatible":
        raise ValueError("Remote embedding dispatch requires openai_compatible")
    if not selected.embedding_model or not selected.embedding_api_key:
        raise ValueError("Embedding model and API key must be configured")
    if (not isinstance(texts, list) or not 1 <= len(texts) <= 64
            or any(not isinstance(text, str) or not text.strip() for text in texts)):
        raise ValueError("Embedding input must contain 1–64 non-empty strings")
    # An explicit byte bound, not a model-specific tokenizer promise.
    if sum(len(text.encode("utf-8")) for text in texts) > 1024 * 1024:
        raise ValueError("Embedding input exceeds the one MiB request bound")
    identity = host._clean({
        "request_id": f"request_{uuid.uuid4().hex}",
        "attempt_id": f"attempt_{uuid.uuid4().hex}", "attempt": 1,
        "kind": "embedding", "usage_basis": "input_only",
        "provider": "openai_compatible", "model": selected.embedding_model,
    })
    # The candidate credential can differ from the normal generation key.
    identity = normalize(identity, (selected.embedding_api_key,))
    async with host.dispatch.scheduler.model_slot():
        try:
            client = _client(selected)
        except Exception:
            raise EmbeddingRequestError("Embedding client initialization failed") from None
        reserved = False
        usage: dict[str, int] = {}
        result: list[list[float]] | None = None
        failure: BaseException | None = None
        request_task: asyncio.Task[Any] | None = None
        started = time.perf_counter()
        try:
            try:
                receipt = host.model_budget.begin_embedding(run_id, texts, identity)
            except BudgetExceeded as exc:
                host.store.append_event(run_id, "budget.blocked", {
                    "request_id": identity["request_id"], "violations": exc.violations,
                })
                raise
            reserved = True
            if receipt.warnings:
                host.store.append_event(run_id, "budget.warning", {
                    "attempt_id": identity["attempt_id"], "budgets": receipt.warnings,
                })
            request_task = asyncio.create_task(client.embeddings.create(
                model=selected.embedding_model, input=texts, encoding_format="float",
            ))
            response = await asyncio.wait_for(
                asyncio.shield(request_task), timeout=selected.model_timeout,
            )
            usage = embedding_usage(_field(response, "usage"))
            result = _vectors(response, len(texts))
        except BaseException as exc:
            failure = exc
        finally:
            if request_task is not None:
                if not request_task.done():
                    request_task.cancel()
                while not request_task.done():
                    try:
                        await asyncio.shield(request_task)
                    except asyncio.CancelledError as exc:
                        failure = failure or exc
                    except BaseException as exc:
                        failure = failure or exc
                if not request_task.cancelled():
                    try:
                        # A response returned during cancellation cleanup still
                        # supplies real usage, even though its vectors are unused.
                        usage = embedding_usage(_field(request_task.result(), "usage"))
                    except BaseException as exc:
                        failure = failure or exc
            try:
                cleanup = asyncio.ensure_future(client.close())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError as exc:
                        failure = failure or exc
                await cleanup
            except BaseException as exc:
                failure = failure or exc
            if reserved:
                settled = host.model_budget.finish(run_id, {
                    **identity,
                    "status": "cancelled" if isinstance(failure, asyncio.CancelledError)
                    else "failed" if failure else "completed",
                    "usage": usage, "usage_source": "reported" if usage else "unknown",
                    "duration_ms": (time.perf_counter() - started) * 1000,
                })
                if settled.warnings:
                    host.store.append_event(run_id, "budget.warning", {
                        "attempt_id": identity["attempt_id"], "budgets": settled.warnings,
                    })
        if isinstance(failure, (asyncio.CancelledError, BudgetExceeded)):
            raise failure
        if isinstance(failure, asyncio.TimeoutError):
            raise TimeoutError("Embedding request timed out") from None
        if isinstance(failure, EmbeddingRequestError):
            raise failure from None
        if isinstance(failure, Exception):
            raise EmbeddingRequestError("Embedding request or client cleanup failed") from None
        if failure is not None:
            raise failure
        assert result is not None
        return result
