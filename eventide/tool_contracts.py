"""Strict built-in contracts and local-only validation of dynamic tool schemas.

Validation never grants permission. The executor validates the original JSON
arguments before it consults policy, injects runtime-owned values, or calls a
handler. Schemas are copied at construction so later catalog changes cannot
change an in-flight executor's contract.
"""

from __future__ import annotations

import copy
import math
from typing import Annotated, Any, Literal

from jsonschema import Draft202012Validator, FormatChecker, validators
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import specification_with


class ToolArgumentsError(ValueError):
    """Arguments were rejected before any handler or permission interaction."""


class ToolContractError(ValueError):
    """A missing, invalid, or unsupported tool definition is not executable."""


class _Arguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


NonemptyText = Annotated[str, Field(min_length=1)]


class BashArguments(_Arguments):
    command: NonemptyText


class ReadFileArguments(_Arguments):
    path: NonemptyText
    offset: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(ge=1)] | None = None
    continuation: (
        Annotated[str, Field(pattern=r"^evtfile1:\d+:\d+:[0-9a-f]{16}:[0-9a-f]{64}$")] | None
    ) = None


class WriteFileArguments(_Arguments):
    path: NonemptyText
    content: str


class EditFileArguments(_Arguments):
    path: NonemptyText
    old_text: NonemptyText
    new_text: str
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


class GlobArguments(_Arguments):
    pattern: NonemptyText


class SearchTextArguments(_Arguments):
    query: Annotated[str, Field(min_length=1, max_length=500)]
    pattern: NonemptyText = "**/*"
    offset: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(ge=1, le=50)] = 50


class CompactArguments(_Arguments):
    focus: str = ""


class ReadToolResultArguments(_Arguments):
    run_id: NonemptyText
    call_id: NonemptyText
    offset: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(ge=1, le=12_000)] = 4_000
    stream: Literal["stdout", "stderr"] | None = None


class TodoItem(_Arguments):
    id: Annotated[str, Field(pattern=r"^t[1-9][0-9]{0,3}$")] | None = None
    content: Annotated[str, Field(min_length=1, max_length=200)]
    status: Literal["pending", "in_progress", "completed", "blocked"]
    summary: Annotated[str, Field(max_length=300)] | None = None
    depends_on: Annotated[list[str], Field(max_length=20)] | None = None


class TodoWriteArguments(_Arguments):
    todos: Annotated[list[TodoItem], Field(max_length=20)]


class LoadSkillArguments(_Arguments):
    name: NonemptyText


class ReadSkillResourceArguments(LoadSkillArguments):
    path: NonemptyText
    offset: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(ge=1, le=12_000)] = 12_000


class RunSkillScriptArguments(LoadSkillArguments):
    path: NonemptyText
    args: Annotated[list[Annotated[str, Field(max_length=4096)]], Field(max_length=32)] = []


class SearchKnowledgeArguments(_Arguments):
    query: NonemptyText
    k: Annotated[int, Field(ge=1, le=10)] = 5


class WorkerTask(_Arguments):
    role: Literal["researcher", "reviewer"]
    goal: Annotated[str, Field(min_length=1, max_length=2_000)]


class DelegateReadonlyArguments(_Arguments):
    tasks: Annotated[list[WorkerTask], Field(min_length=1, max_length=2)]


class ToolDiscoverArguments(_Arguments):
    query: str = ""
    offset: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(ge=1, le=50)] = 20


class ToolLoadArguments(_Arguments):
    name: NonemptyText
    expected_catalog_version: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


BUILTIN_ARGUMENTS: dict[str, type[BaseModel]] = {
    "bash": BashArguments,
    "read_file": ReadFileArguments,
    "write_file": WriteFileArguments,
    "edit_file": EditFileArguments,
    "glob": GlobArguments,
    "search_text": SearchTextArguments,
    "compact": CompactArguments,
    "read_tool_result": ReadToolResultArguments,
    "todo_write": TodoWriteArguments,
    "load_skill": LoadSkillArguments,
    "read_skill_resource": ReadSkillResourceArguments,
    "run_skill_script": RunSkillScriptArguments,
    "search_knowledge": SearchKnowledgeArguments,
    "delegate_readonly": DelegateReadonlyArguments,
    "tool_discover": ToolDiscoverArguments,
    "tool_load": ToolLoadArguments,
}


def _compact_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_compact_schema(item) for item in value]
    if isinstance(value, dict):
        # Cosmetic titles and defaults consume model context. Excluding defaults
        # also makes it clear that validation does not fill omitted arguments.
        return {
            key: _compact_schema(item)
            for key, item in value.items()
            if key not in {"title", "default"}
        }
    return value


def builtin_schema(name: str) -> dict[str, Any]:
    """Generate the public schema from the same strict model used at execution."""
    return dict(_compact_schema(BUILTIN_ARGUMENTS[name].model_json_schema()))


def production_tool_catalog(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy a catalog and replace built-in schemas with their type contracts.

    The available Skill names are runtime-specific, so retain that narrower enum.
    Stateful plan dependency rules and filesystem checks still run in handlers.
    """
    catalog = copy.deepcopy(tools)
    for tool in catalog:
        name = tool.get("name")
        if name not in BUILTIN_ARGUMENTS:
            continue
        schema = builtin_schema(name)
        if name in {"load_skill", "read_skill_resource", "run_skill_script"}:
            names = tool.get("input_schema", {}).get("properties", {}).get("name", {}).get("enum")
            if isinstance(names, list):
                schema["properties"]["name"]["enum"] = list(names)
        tool["input_schema"] = schema
    return catalog


def _check_json(value: Any, depth: int = 0) -> None:
    """Reject non-JSON Python values, booleans-as-integers, NaN and huge depth."""
    if depth > 64:
        raise ToolArgumentsError("Argument nesting exceeds 64 levels")
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _check_json(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _check_json(item, depth + 1)
        return
    raise ToolArgumentsError("Arguments must contain only finite JSON values")


class ToolContract:
    """A frozen schema whose references can resolve only inside this document."""

    def __init__(self, name: str, schema: object):
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ToolContractError("Tool input_schema must declare an object")
        self.name = name
        self.schema = copy.deepcopy(schema)
        try:
            _check_json(self.schema)
            declared = self.schema.get("$schema")
            if declared is not None and not isinstance(declared, str):
                raise ToolContractError("Unsupported tool schema dialect")
            validator_type = validators.validator_for(self.schema, default=Draft202012Validator)
            dialect = validator_type.META_SCHEMA.get(
                "$id", validator_type.META_SCHEMA.get("id", "")
            )
            if declared is not None and declared.rstrip("#") != dialect.rstrip("#"):
                raise ToolContractError("Unsupported tool schema dialect")
            validator_type.check_schema(self.schema)
            registry: Registry[Any] = Registry()
            resource = Resource.from_contents(
                self.schema, default_specification=specification_with(dialect)
            )
            pending = [(resource, registry.resolver_with_root(resource))]
            while pending:
                current, resolver = pending.pop()
                node = current.contents
                if isinstance(node, dict):
                    for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                        if key not in node:
                            continue
                        reference = node[key]
                        if not isinstance(reference, str) or not reference.startswith("#"):
                            raise ToolContractError("Tool schema references must be document-local")
                        if key not in validator_type.VALIDATORS:
                            raise ToolContractError("Unsupported tool schema reference keyword")
                        try:
                            resolver.lookup(reference)
                        except Exception as exc:
                            raise ToolContractError(
                                "Tool schema could not be resolved locally"
                            ) from exc
                pending.extend(
                    (child, resolver.in_subresource(child)) for child in current.subresources()
                )
            self.validator = validator_type(
                self.schema,
                # An explicitly empty Registry refuses missing resources; unlike
                # jsonschema's compatibility default it never retrieves URLs.
                registry=registry,
                format_checker=FormatChecker(),
            )
        except ToolArgumentsError as exc:
            raise ToolContractError("Tool schema is not finite JSON") from exc
        except (SchemaError, RecursionError, TypeError, ValueError) as exc:
            if isinstance(exc, ToolContractError):
                raise
            raise ToolContractError("Invalid tool input schema") from exc

    def validate(self, arguments: object) -> dict[str, Any]:
        if type(arguments) is not dict:
            raise ToolArgumentsError("Tool arguments must be a JSON object")
        _check_json(arguments)
        result = copy.deepcopy(arguments)
        model = BUILTIN_ARGUMENTS.get(self.name)
        if model is not None:
            try:
                checked = model.model_validate(result, strict=True)
            except ValidationError as exc:
                issues = exc.errors(include_input=False, include_url=False, include_context=False)
                labels = [
                    f"{'.'.join(str(part) for part in item['loc']) or '$'}: {item['msg']}"
                    for item in issues[:4]
                ]
                raise ToolArgumentsError("; ".join(labels)) from exc
            result = checked.model_dump(exclude_unset=True)
        try:
            error = next(self.validator.iter_errors(result), None)
        except Exception as exc:
            # Missing local refs and any unsupported resolution path are contract
            # failures, never permission to skip validation or fetch a resource.
            raise ToolContractError("Tool schema could not be resolved locally") from exc
        if error is not None:
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            raise ToolArgumentsError(f"{path}: schema constraint '{error.validator}' failed")
        return result


def contract_catalog(tools: list[dict[str, Any]]) -> dict[str, ToolContract | ToolContractError]:
    """Compile each tool independently; a bad remote tool cannot disable good ones."""
    contracts: dict[str, ToolContract | ToolContractError] = {}
    for tool in production_tool_catalog(tools):
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise ToolContractError("Tool name must be non-empty text")
        if name in contracts:
            contracts[name] = ToolContractError("Duplicate tool name in catalog")
            continue
        try:
            contracts[name] = ToolContract(name, tool.get("input_schema"))
        except ToolContractError as exc:
            contracts[name] = exc
    return contracts
