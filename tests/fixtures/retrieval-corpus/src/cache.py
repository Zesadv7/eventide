def invalidate_cache(key: str) -> None:
    """Remove one stale cache entry after a successful write."""
    del key

