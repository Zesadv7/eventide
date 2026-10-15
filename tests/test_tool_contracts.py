"""Pre-execution contracts must reject invalid input without side effects."""

from types import SimpleNamespace

import pytest

from eventide.executor import ToolContext, ToolExecutor
from eventide.mcp.client import _tool_handler
from eventide.models import ToolCall
from eventide.policy import PolicyEngine
from eventide.tool_contracts import (
    BUILTIN_ARGUMENTS,
    ToolArgumentsError,
    ToolContract,
    ToolContractError,
    builtin_schema,
    contract_catalog,
    production_tool_catalog,
)
from eventide.tools.runtime_catalog import HANDLERS, TOOLS


def tool(name, schema):
    return {"name": name, "input_schema": schema}


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "file", "offset": "1"},
        {"path": "file", "offset": True},
        {"path": "file", "offset": -1},
        {"path": "file", "limit": 0},
        {"path": "file", "limit": 1.0},
        {"path": "file", "cwd": "/other"},
        {"path": ""},
        {},
        [],
        None,
    ],
)
async def test_invalid_builtin_never_reaches_policy_or_handler(isolated_workspace, arguments):
    called = []

    class NoPolicy:
        def evaluate(self, *args, **kwargs):
            raise AssertionError("Invalid parameters reached policy")

    executor = ToolExecutor({"read_file": lambda **kwargs: called.append(kwargs)})
    result = await executor.execute(
        ToolCall("bad", "read_file", arguments),
        ToolContext(isolated_workspace, "run", NoPolicy()),
    )
    assert result.is_error and result.metadata["code"] == "invalid_arguments"
    assert not called


async def test_runtime_cwd_is_injected_after_validation(isolated_workspace):
    seen = []
    executor = ToolExecutor({"read_file": lambda **kwargs: seen.append(kwargs) or "ok"})
    original = {"path": "a", "offset": 0, "limit": None}
    result = await executor.execute(
        ToolCall("ok", "read_file", original),
        ToolContext(isolated_workspace, "run", PolicyEngine()),
    )
    assert not result.is_error
    assert seen == [{**original, "cwd": isolated_workspace}]
    assert "cwd" not in original


async def test_validated_arguments_still_require_policy_and_approval(isolated_workspace):
    calls = []

    async def approve(*args):
        calls.append("approval")
        return False

    executor = ToolExecutor({"bash": lambda **kwargs: calls.append("handler")})
    result = await executor.execute(
        ToolCall("delete", "bash", {"command": "rm a"}),
        ToolContext(isolated_workspace, "run", PolicyEngine(), approve),
    )
    assert calls == ["approval"]
    assert result.metadata["code"] == "permission_denied"


async def test_custom_handler_requires_an_explicit_contract(isolated_workspace):
    calls = []
    executor = ToolExecutor({"custom": lambda **kwargs: calls.append(kwargs)})
    result = await executor.execute(
        ToolCall("bad", "custom", {}), ToolContext(isolated_workspace, "run", PolicyEngine())
    )
    assert result.metadata["code"] == "invalid_tool_contract"
    assert not calls


async def test_dynamic_contract_validates_nested_arguments_and_freezes_schema(isolated_workspace):
    calls = []
    schema = {
        "type": "object",
        "properties": {"values": {"type": "array", "items": {"type": "integer"}}},
        "required": ["values"],
        "additionalProperties": False,
    }
    executor = ToolExecutor(
        {"custom": lambda **kwargs: calls.append(kwargs) or "ok"},
        tools=[tool("custom", schema)],
    )
    schema["properties"]["values"]["items"] = {}
    context = ToolContext(isolated_workspace, "run", PolicyEngine())
    rejected = await executor.execute(ToolCall("bad", "custom", {"values": [True]}), context)
    accepted = await executor.execute(ToolCall("ok", "custom", {"values": [1]}), context)
    assert rejected.metadata["code"] == "invalid_arguments"
    assert not accepted.is_error and calls == [{"values": [1]}]


@pytest.mark.parametrize(
    "reference",
    ["https://example.invalid/schema", "file:///private/schema.json", "other.json#/$defs/item"],
)
def test_external_schema_references_are_rejected_without_fetch(reference, monkeypatch):
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("Network lookup attempted")
    )
    schema = {"type": "object", "properties": {"value": {"$ref": reference}}}
    with pytest.raises(ToolContractError, match="document-local"):
        ToolContract("mcp__s__tool", schema)


def test_local_schema_refs_work_and_unresolvable_refs_fail_closed():
    schema = {
        "type": "object",
        "$defs": {"value": {"type": "integer", "minimum": 1}},
        "properties": {"value": {"$ref": "#/$defs/value"}},
        "required": ["value"],
    }
    contract = ToolContract("custom", schema)
    assert contract.validate({"value": 1}) == {"value": 1}
    with pytest.raises(ToolArgumentsError):
        contract.validate({"value": 0})
    schema["properties"]["value"]["$ref"] = "#/$defs/missing"
    with pytest.raises(ToolContractError, match="locally"):
        ToolContract("custom", schema)


@pytest.mark.parametrize(
    "schema",
    [None, {}, {"type": "array"}, {"type": "object", "required": "wrong"}],
)
async def test_invalid_schema_never_calls_handler(isolated_workspace, schema):
    calls = []
    executor = ToolExecutor(
        {"custom": lambda **kwargs: calls.append(kwargs)}, tools=[tool("custom", schema)]
    )
    result = await executor.execute(
        ToolCall("bad", "custom", {}), ToolContext(isolated_workspace, "run", PolicyEngine())
    )
    assert result.metadata["code"] == "invalid_tool_contract" and not calls


def test_unknown_dialect_and_duplicate_names_are_not_silently_accepted():
    with pytest.raises(ToolContractError, match="dialect"):
        ToolContract("custom", {"type": "object", "$schema": "https://invalid.test/draft"})
    contracts = contract_catalog([tool("x", {"type": "object"})] * 2)
    assert isinstance(contracts["x"], ToolContractError)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1, 2}, (1, 2)])
def test_non_json_arguments_are_not_forwarded(value):
    with pytest.raises(ToolArgumentsError, match="finite JSON"):
        ToolContract("custom", {"type": "object"}).validate({"value": value})


def test_omitted_fields_stay_omitted_and_explicit_null_is_preserved():
    contract = ToolContract("read_file", builtin_schema("read_file"))
    assert contract.validate({"path": "x"}) == {"path": "x"}
    assert contract.validate({"path": "x", "limit": None}) == {"path": "x", "limit": None}


def test_all_builtin_catalog_schemas_come_from_strict_models():
    assert {item["name"] for item in TOOLS} == set(HANDLERS)
    for name in BUILTIN_ARGUMENTS:
        schema = builtin_schema(name)
        assert schema["additionalProperties"] is False
        ToolContract(name, schema)
    for item in TOOLS:
        assert item["input_schema"] == builtin_schema(item["name"])


def test_available_skill_names_are_retained_in_generated_contract():
    catalog = production_tool_catalog(
        [tool("load_skill", {"properties": {"name": {"enum": ["review"]}}})]
    )
    contract = contract_catalog(catalog)["load_skill"]
    assert not isinstance(contract, ToolContractError)
    assert contract.validate({"name": "review"}) == {"name": "review"}
    with pytest.raises(ToolArgumentsError):
        contract.validate({"name": "missing"})


async def test_mcp_transport_binding_cannot_be_replaced_by_model_keywords():
    calls = []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(content=[], structured_content=None, is_error=False)

    invoke = _tool_handler(Session(), "actual", None)
    injected = {"_session": "bad", "_tool_name": "other", "_timeout": 0}
    await invoke(**injected)
    assert calls == [("actual", injected)]


def test_validation_errors_do_not_echo_argument_secrets():
    with pytest.raises(ToolArgumentsError) as caught:
        ToolContract("read_file", builtin_schema("read_file")).validate(
            {"path": "x", "offset": "sensitive-value"}
        )
    assert "sensitive-value" not in str(caught.value)


@pytest.mark.parametrize("dialect", [17, [], {}])
def test_malformed_dialect_is_an_isolated_contract_failure(dialect):
    catalog = contract_catalog([
        tool("bad", {"type": "object", "$schema": dialect}),
        tool("good", {"type": "object"}),
    ])
    assert isinstance(catalog["bad"], ToolContractError)
    assert isinstance(catalog["good"], ToolContract)


def test_reference_keyword_must_be_supported_by_its_dialect():
    with pytest.raises(ToolContractError, match="reference keyword"):
        ToolContract("custom", {"type": "object", "$recursiveRef": "#"})


def test_reference_like_data_is_not_treated_as_a_schema_reference():
    schema = {
        "type": "object",
        "properties": {"data": {"const": {"$ref": "https://example.invalid/data"}}},
    }
    data = {"data": {"$ref": "https://example.invalid/data"}}
    assert ToolContract("custom", schema).validate(data) == data


def test_local_reference_resolves_with_nested_schema_scope():
    schema = {
        "type": "object",
        "$id": "https://example.invalid/root",
        "properties": {
            "item": {
                "$id": "nested",
                "type": "object",
                "$defs": {"count": {"type": "integer"}},
                "properties": {"count": {"$ref": "#/$defs/count"}},
            },
        },
    }
    assert ToolContract("custom", schema).validate({"item": {"count": 2}}) == {
        "item": {"count": 2}
    }
