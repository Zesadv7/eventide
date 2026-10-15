"""Fixed benchmark package resources, independent of the checkout and current directory."""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from collections import Counter
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import Path as ZipPath

import yaml

from eventide.eval_suite import BenchmarkManifest, Split, load_benchmark_data, relative_file

if sys.version_info >= (3, 11):
    from importlib.resources.abc import Traversable
else:
    from importlib.abc import Traversable

BUILTIN_SUITE_IDS = ("engineering-v1",)
MAX_RESOURCE_BYTES = 3 * 1024 * 1024


def _reject_links(resource: Traversable) -> None:
    if isinstance(resource, Path):
        if resource.absolute().resolve() != resource.absolute():
            raise ValueError("Built-in evaluation resources must not be linked")
    elif isinstance(resource, ZipPath):
        # zipfile.Path.is_file treats UNIX symbolic-link entries as ordinary files.
        # Inspect every ancestor as well as the entry, without extracting the archive.
        parts = PurePosixPath(resource.at.rstrip("/")).parts
        for count in range(1, len(parts) + 1):
            prefix = "/".join(parts[:count])
            for name in (prefix, prefix + "/"):
                try:
                    metadata = resource.root.getinfo(name)
                except KeyError:
                    continue
                if stat.S_ISLNK(metadata.external_attr >> 16):
                    raise ValueError("Built-in evaluation resources must not be linked")
    else:
        raise ValueError("Unsupported built-in evaluation resource provider")


def _read_resource(root: Traversable, name: str) -> bytes:
    relative_file(name)
    _reject_links(root)
    if not root.is_dir():
        raise ValueError("Built-in evaluation resources are missing")
    node = root
    parts = PurePosixPath(name).parts
    for index, part in enumerate(parts):
        node = node.joinpath(part)
        _reject_links(node)
        if not (node.is_file() if index == len(parts) - 1 else node.is_dir()):
            raise ValueError("Built-in evaluation reference missing or not a regular file")
    with node.open("rb") as stream:
        body = stream.read(MAX_RESOURCE_BYTES + 1)
    if len(body) > MAX_RESOURCE_BYTES:
        raise ValueError("Built-in evaluation resource exceeds its size limit")
    return body


def load_builtin_suite(suite_id: str, *, split: Split | None = None) -> dict[str, Any]:
    """Return fresh, fully validated JSON inputs for admission to freeze in its Store.

    The ID is an allowlist key, never a caller-selected resource path. Validate all
    twenty tasks before selecting a split. Hashes bind the raw manifest, canonical
    manifest and selected parsed inputs respectively; no files are reopened later.
    """
    if type(suite_id) is not str or suite_id not in BUILTIN_SUITE_IDS:
        raise ValueError("Unknown built-in evaluation suite")
    if split is not None and split not in ("development", "holdout"):
        raise ValueError("Unknown built-in evaluation split")
    root = resources.files("eventide").joinpath("bundled_evals").joinpath(suite_id)
    body = _read_resource(root, "manifest.yaml")
    data = yaml.safe_load(body)
    manifest = BenchmarkManifest.model_validate(data)
    if (manifest.id, manifest.version) != ("eventide-engineering-benchmark", 1):
        raise ValueError("Built-in evaluation suite identity or version does not match its ID")
    cases = load_benchmark_data(data, lambda name: _read_resource(root, name))
    selected = [case for case in cases if split is None or case["benchmark"]["split"] == split]
    encoded = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "id": suite_id, "benchmark_id": manifest.id, "version": manifest.version,
        "description": manifest.description, "split": split, "total": len(selected),
        "splits": dict(Counter(case["benchmark"]["split"] for case in cases)),
        "manifest_sha256": hashlib.sha256(body).hexdigest(),
        "suite_sha256": cases[0]["benchmark"]["suite_sha256"],
        "inputs_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "cases": selected,
    }


def builtin_suite_catalog() -> list[dict[str, Any]]:
    """Describe only installed, verified built-ins; never silently hide corruption."""
    return [{key: value for key, value in load_builtin_suite(suite_id).items() if key != "cases"}
            for suite_id in BUILTIN_SUITE_IDS]


def load_builtin_alias(data: dict[str, Any]) -> dict[str, Any]:
    """The compatibility YAML is an exact alias, with no path or input overrides."""
    if set(data) != {"builtin_suite"} or type(data["builtin_suite"]) is not str:
        raise ValueError("Built-in evaluation alias must contain only builtin_suite")
    return load_builtin_suite(data["builtin_suite"])
