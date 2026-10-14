"""Optional Tree-sitter source boundaries; imports stay dependency-free."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import PurePosixPath
from typing import Any

STRUCTURE_VERSION = "tree-sitter-symbols-v1"
_LANGUAGES = {
    ".py": ("tree_sitter_python", "language"),
    ".js": ("tree_sitter_javascript", "language"),
    ".jsx": ("tree_sitter_javascript", "language"),
    ".mjs": ("tree_sitter_javascript", "language"),
    ".cjs": ("tree_sitter_javascript", "language"),
    ".ts": ("tree_sitter_typescript", "language_typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".mts": ("tree_sitter_typescript", "language_typescript"),
    ".cts": ("tree_sitter_typescript", "language_typescript"),
}
_NAMED_SYMBOLS = {
    "function_definition",
    "class_definition",
    "class_declaration",
    "abstract_class_declaration",
    "class",
    "function_declaration",
    "generator_function_declaration",
    "method_definition",
    "interface_declaration",
    "method_signature",
    "abstract_method_signature",
    "function_signature",
    "type_alias_declaration",
    "enum_declaration",
}
_FUNCTION_VALUES = {"arrow_function", "function_expression", "generator_function"}


@dataclass(frozen=True, slots=True)
class SourceSections:
    """Non-overlapping character ranges or an explicit window fallback reason."""

    sections: tuple[tuple[int, int, str], ...] = ()
    fallback_reason: str | None = None


@dataclass(slots=True)
class _Symbol:
    start: int
    end: int
    heading: str
    children: list[_Symbol] = field(default_factory=list)


def parser_identity() -> dict[str, str | None]:
    """Identify installed optional parsers without loading native extensions."""
    versions: dict[str, str | None] = {}
    for package in (
        "tree-sitter",
        "tree-sitter-python",
        "tree-sitter-javascript",
        "tree-sitter-typescript",
    ):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def _parser(suffix: str) -> Any:
    binding = importlib.import_module("tree_sitter")
    module, entrypoint = _LANGUAGES[suffix]
    grammar = importlib.import_module(module)
    return binding.Parser(binding.Language(getattr(grammar, entrypoint)()))


def _symbol_name(node: Any, source: bytes) -> str | None:
    if node.type in _NAMED_SYMBOLS:
        name = node.child_by_field_name("name")
        if name is None:
            return "<class>" if node.type == "class" else None
    elif node.type in {"variable_declarator", "field_definition", "public_field_definition"}:
        value = node.child_by_field_name("value")
        name = node.child_by_field_name("name")
        if value is None or value.type not in _FUNCTION_VALUES or name is None:
            return None
    else:
        return None
    return source[name.start_byte : name.end_byte].decode("utf-8").strip()[:128] or None


def _symbol_bounds(node: Any, source: bytes) -> tuple[int, int]:
    parent = node.parent
    if parent is not None and parent.type in {"decorated_definition", "export_statement"}:
        node = parent
        parent = node.parent
    if (
        node.type == "variable_declarator"
        and parent is not None
        and parent.type in {"lexical_declaration", "variable_declaration"}
        and sum(child.type == "variable_declarator" for child in parent.named_children) == 1
    ):
        node = parent
        if node.parent is not None and node.parent.type == "export_statement":
            node = node.parent
    start, end = node.start_byte, node.end_byte
    # Include indentation but never cross a preceding symbol on the same line.
    line_start = source.rfind(b"\n", 0, start) + 1
    if not source[line_start:start].strip():
        start = line_start
    return start, end


def _symbols(root: Any, source: bytes) -> list[_Symbol]:
    roots: list[_Symbol] = []
    pending: list[tuple[Any, _Symbol | None]] = [(root, None)]
    while pending:
        node, ancestor = pending.pop()
        name = _symbol_name(node, source)
        if name is not None:
            start, end = _symbol_bounds(node, source)
            heading = f"{ancestor.heading} > {name}" if ancestor is not None else name
            symbol = _Symbol(start, end, heading)
            (ancestor.children if ancestor is not None else roots).append(symbol)
            ancestor = symbol
        pending.extend((child, ancestor) for child in reversed(node.named_children))
    return roots


def _partition(source: bytes, symbols: list[_Symbol]) -> tuple[tuple[int, int, str], ...]:
    byte_sections: list[tuple[int, int, str]] = []
    pending = [(0, len(source), "", symbols)]
    while pending:
        start, end, heading, children = pending.pop()
        cursor = start
        parts: list[tuple[int, int, str, list[_Symbol]]] = []
        for child in children:
            if child.start < cursor or child.end > end or child.end <= child.start:
                raise ValueError("Overlapping source symbols")
            if cursor < child.start:
                parts.append((cursor, child.start, heading, []))
            parts.append((child.start, child.end, child.heading, child.children))
            cursor = child.end
        if cursor < end:
            parts.append((cursor, end, heading, []))
        if children:
            pending.extend(reversed(parts))
        elif start < end:
            byte_sections.append((start, end, heading))
    # Tree-sitter offsets count UTF-8 bytes; Chunk offsets count Python characters.
    offsets = sorted({offset for start, end, _ in byte_sections for offset in (start, end)})
    characters: dict[int, int] = {}
    previous = count = 0
    for offset in offsets:
        count += len(source[previous:offset].decode("utf-8"))
        characters[offset] = count
        previous = offset
    return tuple(
        (characters[start], characters[end], heading) for start, end, heading in byte_sections
    )


def source_sections(file: str, text: str) -> SourceSections:
    """Parse supported source or fall back safely without guessing symbol ranges."""
    suffix = PurePosixPath(file).suffix.lower()
    if suffix not in _LANGUAGES:
        return SourceSections(fallback_reason="unsupported_language")
    try:
        parser = _parser(suffix)
    except ImportError:
        return SourceSections(fallback_reason="dependency_unavailable")
    except (AttributeError, OSError, TypeError, ValueError, RuntimeError):
        return SourceSections(fallback_reason="parser_unavailable")
    source = text.encode("utf-8")
    try:
        tree = parser.parse(source)
        if tree is None or tree.root_node.has_error:
            return SourceSections(fallback_reason="parse_error")
        return SourceSections(_partition(source, _symbols(tree.root_node, source)))
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return SourceSections(fallback_reason="structure_error")
