"""Installed benchmark resources share the frozen suite contract and no source tree."""

import copy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from collections import Counter
from pathlib import Path
from zipfile import Path as ZipPath
from zipfile import ZipFile, ZipInfo

import pytest
import yaml

import eventide.bundled_evaluations as bundled
from eventide.evaluation import load_suite

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "eventide"
SUITE = PACKAGE / "bundled_evals/engineering-v1"


def _fingerprint(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _copy_package(tmp_path, monkeypatch):
    package = tmp_path / "package"
    shutil.copytree(SUITE, package / "bundled_evals/engineering-v1")
    monkeypatch.setattr(bundled.resources, "files", lambda _: package)
    return package / "bundled_evals/engineering-v1"


def test_catalog_and_alias_use_one_unchanged_packaged_benchmark(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    frozen = bundled.load_builtin_suite("engineering-v1")
    assert frozen["cases"] == load_suite(ROOT / "evals/benchmark.yaml")
    assert frozen["id"] == "engineering-v1" and frozen["version"] == 1
    assert frozen["total"] == 20 and frozen["splits"] == {"development": 12, "holdout": 8}
    manifest_hash = hashlib.sha256((SUITE / "manifest.yaml").read_bytes()).hexdigest()
    assert frozen["manifest_sha256"] == manifest_hash
    assert frozen["suite_sha256"] == frozen["cases"][0]["benchmark"]["suite_sha256"]
    assert frozen["inputs_sha256"] == _fingerprint(frozen["cases"])
    assert bundled.builtin_suite_catalog() == [
        {key: value for key, value in frozen.items() if key != "cases"}
    ]
    assert not (ROOT / "evals/fixtures/benchmark").exists()
    assert len([path for path in SUITE.rglob("*") if path.is_file()]) == 21


@pytest.mark.parametrize("split,count", [("development", 12), ("holdout", 8)])
def test_split_is_validated_then_frozen_with_independent_contents(split, count):
    frozen = bundled.load_builtin_suite("engineering-v1", split=split)
    original = copy.deepcopy(frozen)
    assert frozen["split"] == split and frozen["total"] == count
    assert Counter(case["benchmark"]["split"] for case in frozen["cases"]) == {split: count}
    assert frozen["inputs_sha256"] == _fingerprint(frozen["cases"])
    frozen["cases"][0]["workspace"]["injected.txt"] = "modified caller copy"
    assert bundled.load_builtin_suite("engineering-v1", split=split) == original


@pytest.mark.parametrize("suite_id", ["unknown", "../engineering-v1", "/engineering-v1",
                                      "engineering-v1/", "engineering-v1\\..", "", None, 1, []])
def test_unknown_or_path_alias_rejected_before_resource_access(suite_id, monkeypatch):
    monkeypatch.setattr(bundled.resources, "files", lambda _: pytest.fail("resources accessed"))
    with pytest.raises(ValueError, match="Unknown built-in"):
        bundled.load_builtin_suite(suite_id)


@pytest.mark.parametrize("split", ["all", "dev", "../holdout", "", 0, []])
def test_invalid_split_rejected_before_resource_access(split, monkeypatch):
    monkeypatch.setattr(bundled.resources, "files", lambda _: pytest.fail("resources accessed"))
    with pytest.raises(ValueError, match="split"):
        bundled.load_builtin_suite("engineering-v1", split=split)


@pytest.mark.parametrize("payload", [
    {"builtin_suite": "engineering-v1", "cases": []},
    {"builtin_suite": "engineering-v1", "path": "../other"},
    {"builtin_suite": "engineering-v1", "split": "holdout"},
    {"builtin_suite": None}, {"builtin_suite": ["engineering-v1"]},
    {"builtin_suite": "other"},
])
def test_compatibility_alias_rejects_overrides_and_unknown_ids(payload, tmp_path):
    alias = tmp_path / "benchmark.yaml"
    alias.write_text(yaml.safe_dump(payload))
    with pytest.raises(ValueError, match="built-in|Built-in"):
        load_suite(alias)


@pytest.mark.parametrize("parent", [False, True])
def test_alias_links_are_rejected(tmp_path, parent):
    original = tmp_path / "real"
    original.mkdir()
    (original / "benchmark.yaml").write_text("builtin_suite: engineering-v1\n")
    linked = tmp_path / "linked"
    linked.symlink_to(original if parent else original / "benchmark.yaml")
    with pytest.raises(ValueError, match="linked"):
        load_suite(linked / "benchmark.yaml" if parent else linked)


def test_selected_split_does_not_hide_corruption_in_other_split(tmp_path, monkeypatch):
    suite = _copy_package(tmp_path, monkeypatch)
    target = next(path for path in (suite / "fixtures/benchmark").glob("*.json")
                  if json.loads(path.read_bytes())["split"] == "holdout")
    target.write_text("{}")
    with pytest.raises(ValueError, match="Frozen task changed"):
        bundled.load_builtin_suite("engineering-v1", split="development")
    with pytest.raises(ValueError, match="Frozen task changed"):
        bundled.builtin_suite_catalog()


@pytest.mark.parametrize("change", [{"version": 2}, {"id": "another-suite"}])
def test_builtin_alias_pins_manifest_identity_and_version(tmp_path, monkeypatch, change):
    suite = _copy_package(tmp_path, monkeypatch)
    path = suite / "manifest.yaml"
    manifest = yaml.safe_load(path.read_bytes())
    manifest.update(change)
    path.write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match="identity or version"):
        bundled.load_builtin_suite("engineering-v1")


@pytest.mark.parametrize("name", ["../outside", "/absolute", "fixtures/../outside", "a\\b", "."])
def test_manifest_reference_cannot_escape_package(tmp_path, monkeypatch, name):
    suite = _copy_package(tmp_path, monkeypatch)
    path = suite / "manifest.yaml"
    manifest = yaml.safe_load(path.read_bytes())
    manifest["tasks"][0]["path"] = name
    path.write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match="canonical relative"):
        bundled.load_builtin_suite("engineering-v1")


def test_missing_installed_suite_is_not_hidden_from_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(bundled.resources, "files", lambda _: tmp_path)
    with pytest.raises(ValueError, match="resources are missing"):
        bundled.builtin_suite_catalog()


@pytest.mark.parametrize("target", ["manifest.yaml", "fixtures/benchmark", "."])
def test_packaged_file_directory_and_root_links_rejected(tmp_path, monkeypatch, target):
    suite = _copy_package(tmp_path, monkeypatch)
    path = suite if target == "." else suite / target
    outside = tmp_path / "outside"
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="linked"):
        bundled.load_builtin_suite("engineering-v1")


@pytest.mark.parametrize("mutation", ["missing", "directory", "oversize"])
def test_missing_or_nonregular_or_oversize_resource_is_explicit(tmp_path, monkeypatch, mutation):
    suite = _copy_package(tmp_path, monkeypatch)
    target = next((suite / "fixtures/benchmark").glob("*.json"))
    if mutation == "oversize":
        target.write_bytes(b"x" * (bundled.MAX_RESOURCE_BYTES + 1))
    else:
        target.unlink()
        if mutation == "directory":
            target.mkdir()
    with pytest.raises(ValueError, match="missing|regular|size limit"):
        bundled.load_builtin_suite("engineering-v1")


@pytest.mark.parametrize("linked", [None, "file", "ancestor"])
def test_importlib_zip_resources_without_extraction(tmp_path, monkeypatch, linked):
    archive = tmp_path / "resources.zip"
    files = sorted(path for path in SUITE.rglob("*") if path.is_file())
    with ZipFile(archive, "w") as output:
        for path in files:
            name = "eventide/" + path.relative_to(PACKAGE).as_posix()
            if linked == "file" and path.name == "manifest.yaml":
                entry = ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                output.writestr(entry, path.read_bytes())
            else:
                output.write(path, name)
        if linked == "ancestor":
            entry = ZipInfo("eventide/bundled_evals/")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(entry, b"elsewhere")
    with ZipFile(archive) as container:
        monkeypatch.setattr(bundled.resources, "files", lambda _: ZipPath(container, "eventide/"))
        if linked:
            with pytest.raises(ValueError, match="linked"):
                bundled.load_builtin_suite("engineering-v1")
        else:
            assert bundled.load_builtin_suite("engineering-v1")["total"] == 20


def test_installed_wheel_contains_all_resources_and_loads_without_checkout(tmp_path):
    location = os.environ.get("EVENTIDE_TEST_EVAL_WHEEL")
    if not location:
        pytest.skip("Set EVENTIDE_TEST_EVAL_WHEEL for the actual offline wheel integration gate")
    wheel = Path(location).resolve(strict=True)
    prefix = "eventide/bundled_evals/engineering-v1/"
    expected = {prefix + path.relative_to(SUITE).as_posix():
                hashlib.sha256(path.read_bytes()).hexdigest()
                for path in SUITE.rglob("*") if path.is_file()}
    with ZipFile(wheel) as archive:
        actual = {name: hashlib.sha256(archive.read(name)).hexdigest()
                  for name in archive.namelist()
                  if name.startswith(prefix) and not name.endswith("/")}
    assert actual == expected and len(actual) == 21
    # -I ignores PYTHONPATH/cwd. The wheel itself is first; source paths are absent.
    script = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import eventide
assert eventide.__file__.startswith(sys.argv[1] + '/'), eventide.__file__
from eventide.bundled_evaluations import builtin_suite_catalog, load_builtin_suite
from eventide.evaluation import load_suite
full = load_builtin_suite('engineering-v1')
assert len(full['cases']) == 20
assert load_builtin_suite('engineering-v1', split='development')['total'] == 12
assert load_builtin_suite('engineering-v1', split='holdout')['total'] == 8
alias = Path('benchmark.yaml')
alias.write_text('builtin_suite: engineering-v1\\n')
assert load_suite(alias) == full['cases']
print(json.dumps(builtin_suite_catalog()))
"""
    # This is a second packaged copy, not another source coverage target. Avoid
    # pytest-cov's subprocess startup injecting wheel paths into the source report.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("COV_CORE_", "COVERAGE_"))}
    result = subprocess.run([sys.executable, "-I", "-c", script, str(wheel)], cwd=tmp_path,
                            env=environment, capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(result.stdout) == bundled.builtin_suite_catalog()
