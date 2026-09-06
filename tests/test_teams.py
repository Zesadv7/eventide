"""Tests for team collaboration primitives."""

from nexus_agent.teams.bus import BUS, MessageBus
from nexus_agent.teams.protocol import (
    pending_requests,
    new_request_id,
    match_response,
    consume_lead_inbox,
    ProtocolState,
)


def test_message_bus_roundtrip():
    bus = MessageBus()
    bus.send("alice", "bob", "hello")
    msgs = bus.read_inbox("bob")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "hello"
    assert bus.read_inbox("bob") == []


def test_global_bus():
    BUS.send("lead", "test_agent", "ping")
    msgs = BUS.read_inbox("test_agent")
    assert msgs[0]["content"] == "ping"


def test_protocol_match_response():
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id,
        type="plan_approval",
        sender="alice",
        target="lead",
        status="pending",
        payload="plan",
    )
    match_response("plan_approval_response", req_id, True)
    assert pending_requests[req_id].status == "approved"


def test_consume_lead_inbox_routes_protocol():
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id,
        type="plan_approval",
        sender="alice",
        target="lead",
        status="pending",
        payload="plan",
    )
    BUS.send("alice", "lead", "ok", "plan_approval_response",
             {"request_id": req_id, "approve": True})
    consume_lead_inbox()
    assert pending_requests[req_id].status == "approved"
