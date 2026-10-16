"""Run-owned index computation bridges CPU work to governed async embedding attempts."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import CancelledError as FutureCancelled
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from eventide.config import Settings
from eventide.embedding_runtime import embed_remote
from eventide.rag.embedding import EmbeddingProfile, EmbeddingProvider
from eventide.rag.index import BuildStats, IndexStore

if TYPE_CHECKING:
    from eventide.host import RuntimeHost


class IndexBuildCancelled(RuntimeError):
    pass


async def build_index(
    host: RuntimeHost, run_id: str, workspace: Path, *, settings: Settings,
    profile: EmbeddingProfile, embedder: EmbeddingProvider | None = None,
    force: bool = False, code_structure: bool = False,
) -> BuildStats:
    """No second Store or request queue: worker thread waits for the owning event loop."""
    loop = asyncio.get_running_loop()
    stopped = threading.Event()
    pending: set[asyncio.Task[list[list[float]]]] = set()

    def check_cancelled() -> None:
        if stopped.is_set():
            raise IndexBuildCancelled("Index build cancelled before publication")

    async def request(texts: list[str]) -> list[list[float]]:
        task = asyncio.current_task()
        assert task is not None
        pending.add(task)
        try:
            check_cancelled()
            return await embed_remote(host, run_id, texts, settings=settings)
        finally:
            pending.discard(task)

    class Bridge:
        def embed(self, texts: list[str]) -> list[list[float]]:
            vectors = []
            # IndexStore groups 128 chunks; the provider admission accepts at most 64.
            for start in range(0, len(texts), 64):
                check_cancelled()
                batch = texts[start:start + 64]
                if profile.provider == "openai_compatible":
                    future = asyncio.run_coroutine_threadsafe(request(batch), loop)
                    try:
                        vectors.extend(future.result())
                    except FutureCancelled as exc:
                        raise IndexBuildCancelled("Index embedding cancelled") from exc
                else:
                    if embedder is None:
                        raise ValueError("Local index build requires an embedder")
                    vectors.extend(embedder.embed(batch))
                check_cancelled()
            return vectors

    worker = asyncio.create_task(asyncio.to_thread(
        IndexStore(workspace).build, force=force, embedder=Bridge(), profile=profile,
        code_structure=code_structure, check_cancelled=check_cancelled,
    ))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Repeated cancellation must not interrupt the provider's cleanup a second time.
            if not cancelled:
                stopped.set()
                for task in tuple(pending):
                    task.cancel()
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not worker.cancelled():
            worker.exception()
        # A cancelled concurrent future may wake the thread before async cleanup finishes.
        while pending:
            cleanup = asyncio.gather(*tuple(pending), return_exceptions=True)
            while not cleanup.done():
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(cleanup)
        raise asyncio.CancelledError
    return worker.result()
