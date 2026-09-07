"""Tests for cron parsing and scheduling."""

from datetime import datetime

from nexus_agent.scheduling.cron import (
    cancel_job,
    consume_cron_queue,
    cron_matches,
    schedule_job,
    validate_cron,
)


def test_validate_cron_ok():
    assert validate_cron("0 9 * * *") is None


def test_validate_cron_bad():
    assert validate_cron("0 9 * *") is not None
    assert validate_cron("70 9 * * *") is not None


def test_cron_matches():
    dt = datetime(2024, 1, 1, 9, 0)
    assert cron_matches("0 9 * * *", dt)
    assert not cron_matches("30 9 * * *", dt)


def test_schedule_and_cancel():
    job = schedule_job("0 9 * * *", "morning standup", recurring=True, durable=False)
    assert isinstance(job, type(schedule_job("0 9 * * *", "x", durable=False)))
    assert consume_cron_queue() == []
    assert "Cancelled" in cancel_job(job.id)
