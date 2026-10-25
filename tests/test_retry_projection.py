from eventide.projections import RuntimeStateProjection


def event(kind, payload, ts=10):
    return {"type": kind, "payload": payload, "ts": ts, "partial": False}


def test_retry_countdown_is_rebuilt_from_original_event_time():
    events = [
        event("run.started", {}),
        event(
            "model.retry",
            {
                "retryable": True,
                "delay_seconds": 5,
                "next_attempt": 2,
                "attempt": 1,
                "category": "network",
            },
        ),
    ]
    state = RuntimeStateProjection.project(events)
    assert state["status"] == "running"
    assert state["model_retry"]["retry_at"] == 15
    assert RuntimeStateProjection.project(events)["model_retry"] == state["model_retry"]
    assert (
        RuntimeStateProjection.project(events + [event("model.attempt_started", {})])["model_retry"]
        is None
    )
    assert (
        RuntimeStateProjection.project(
            events + [event("run.interrupted", {"reason": "cancelled"})]
        )["model_retry"]
        is None
    )


def test_old_retry_without_delay_and_terminal_retries_never_show_countdown():
    assert (
        RuntimeStateProjection.project([event("model.retry", {"retryable": True})])["model_retry"]
        is None
    )
    assert (
        RuntimeStateProjection.project(
            [event("run.failed", {}), event("model.retry", {"retryable": True, "delay_seconds": 5})]
        )["model_retry"]
        is None
    )
