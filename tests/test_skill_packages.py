"""Skill imports never execute, escape their roots or silently change running resources."""

import io
import stat
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from eventide.skill_packages import (
    SkillLimits,
    SkillPackageError,
    SkillPackageService,
    preview_directory,
    preview_zip,
)
from eventide.skills import SkillCatalog


def make_skill(root: Path, name="review", *, description="Review code", extra="") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n# Instructions\n"
        "Read references/rules.md. Never override user permissions.\n",
        encoding="utf-8",
    )
    (root / "references").mkdir(exist_ok=True)
    (root / "references" / "rules.md").write_text("Keep tests fixed.\n", encoding="utf-8")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "check.py").write_text(
        "raise SystemExit('must not run on import')\n",
        encoding="utf-8",
    )
    return root


def archive(files: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
        for name, content in files.items():
            output.writestr(name, content)
    return buffer.getvalue()


def package_zip(root: Path, **extra: bytes | str) -> bytes:
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    return archive({**files, **extra})


def test_preview_is_read_only_and_directory_zip_have_same_version(tmp_path):
    source = make_skill(tmp_path / "source")
    service = SkillPackageService(tmp_path / "state")
    preview = service.preview_directory(source)
    zipped = service.preview_zip(package_zip(source))
    assert preview.valid and zipped.valid
    assert preview.version == zipped.version
    assert preview.manifest == zipped.manifest
    assert preview.as_dict()["manifest"][0]["sha256"]
    assert not service.root.exists()
    assert "content" not in repr(preview)
    assert not (tmp_path / "must-not-run").exists()


def test_import_deduplicates_bytes_emits_facts_and_does_not_activate(tmp_path):
    source = make_skill(tmp_path / "source")
    facts = []
    service = SkillPackageService(tmp_path / "state")
    first = service.import_directory(source, record=facts.append)
    second = service.import_zip(package_zip(source))
    assert first.version == second.version
    assert first.root == second.root
    assert [version.version for version in service.list_versions("review")] == [first.version]
    assert facts[0]["type"] == "skill.package_imported"
    assert not list(service.root.rglob("*.json"))
    assert (
        service.read_resource("review", first.version, "references/rules.md")
        == "Keep tests fixed.\n"
    )
    assert not SkillCatalog.discover(tmp_path, user_packages=(first,))
    assert not SkillCatalog.discover(tmp_path, user_packages=(first,), selected=())


def test_new_version_does_not_mutate_installed_or_frozen_resources(tmp_path):
    source = make_skill(tmp_path / "source")
    service = SkillPackageService(tmp_path / "state")
    first = service.import_directory(source)
    catalog = SkillCatalog.discover(
        tmp_path,
        user_packages=(first,),
        enabled_user=("user:review",),
        selected=("user:review",),
    )
    assert catalog.selected == ("user:review",)
    original_hash = catalog.catalog_hash
    (source / "references" / "rules.md").write_text("New rules", encoding="utf-8")
    second = service.import_directory(source)
    assert first.version != second.version
    assert catalog.resource_bytes("user:review", "references/rules.md") == b"Keep tests fixed.\n"
    assert catalog.read_resource("review", "references/rules.md") == "Keep tests fixed.\n"
    assert catalog.catalog_hash == original_hash
    assert len(service.list_versions("review")) == 2
    assert service.read_resource("review", second.version, "references/rules.md") == "New rules"


def test_project_resources_are_frozen_with_manifest(tmp_path):
    source = make_skill(tmp_path / "skills" / "review")
    catalog = SkillCatalog.discover(tmp_path)
    (source / "references" / "rules.md").write_text("changed", encoding="utf-8")
    assert catalog.read_resource("project:review", "references/rules.md") == "Keep tests fixed.\n"
    assert SkillCatalog.discover(tmp_path).catalog_hash != catalog.catalog_hash
    assert catalog.snapshot()["skills"][0]["manifest"]
    assert catalog.resource_tool()["name"] == "read_skill_resource"


def test_installed_tampering_is_rejected_even_before_resource_read(tmp_path):
    service = SkillPackageService(tmp_path / "state")
    installed = service.import_directory(make_skill(tmp_path / "source"))
    resource = installed.root / "references" / "rules.md"
    resource.chmod(0o600)
    resource.write_text("tampered", encoding="utf-8")
    for operation in (
        lambda: service.inspect_version("review", installed.version),
        lambda: service.read_resource("review", installed.version, "SKILL.md"),
        lambda: service.list_versions("review"),
    ):
        with pytest.raises(SkillPackageError, match="no longer matches"):
            operation()


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/absolute",
        "references/../escape",
        "references//a",
        "references/./a",
        "references\\a",
        "C:/bad",
        "assets/secret:stream",
        "assets/a.",
        "assets/NUL.txt",
        "assets/AUX",
        "random.txt",
        "other/file",
        "assets/a\nline",
        "assets/../outside",
    ],
)
def test_zip_paths_are_rejected_before_materialization(tmp_path, path):
    source = make_skill(tmp_path / "source")
    service = SkillPackageService(tmp_path / "state")
    preview = service.preview_zip(package_zip(source, **{path: "evil"}))
    assert not preview.valid
    with pytest.raises(SkillPackageError):
        service.import_zip(package_zip(source, **{path: "evil"}))
    assert not service.root.exists()


def test_archive_symlink_and_case_collisions_are_rejected(tmp_path):
    source = make_skill(tmp_path / "source")
    content = io.BytesIO(package_zip(source))
    with zipfile.ZipFile(content, "a") as output:
        link = zipfile.ZipInfo("assets/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(link, "../../secret")
    assert not preview_zip(content.getvalue()).valid
    assert not preview_zip(package_zip(source, **{"references/RULES.md": "duplicate"})).valid


def test_links_special_files_and_unsupported_directories_are_diagnostics(tmp_path):
    source = make_skill(tmp_path / "source")
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    link = source / "assets"
    link.symlink_to(outside)
    assert not preview_directory(source).valid
    link.unlink()
    (source / "references" / "link").symlink_to(outside)
    assert not preview_directory(source).valid
    (source / "references" / "link").unlink()
    (source / ".git").mkdir()
    assert not preview_directory(source).valid


def test_archive_limits_apply_before_writes(tmp_path):
    source = make_skill(tmp_path / "source")
    data = package_zip(source)
    defaults = SkillLimits()
    for limits in (
        replace(defaults, max_files=1),
        replace(defaults, max_total_bytes=10),
        replace(defaults, max_file_bytes=10),
        replace(defaults, max_archive_bytes=10),
        replace(defaults, max_manifest_bytes=10),
    ):
        service = SkillPackageService(tmp_path / "state", limits)
        assert not service.preview_zip(data).valid
        with pytest.raises(SkillPackageError):
            service.import_zip(data)
        assert not service.root.exists()
    assert not preview_zip(b"not a zip").valid
    with pytest.raises(ValueError):
        SkillLimits(max_files=True)


@pytest.mark.parametrize(
    "content",
    [
        "# No metadata",
        "---\nname: review\n",
        "---\nname: [unclosed\n---\nbody",
        "---\nname: review\n---\nbody",
        "---\nname: Review\ndescription: x\n---\nbody",
        "---\nname: review\nname: review\ndescription: x\n---\nbody",
        "---\nname: review\ndescription: x\npermissions: all\n---\nbody",
        "---\nname: review\ndescription: x\nmetadata: {requires: 12}\n---\nbody",
        "---\nname: review\ndescription: x\n---\n",
        "---\nname: review\ndescription: &d x\ncompatibility: *d\n---\nbody",
        "---\nname: review\ndescription: " + "x" * 1025 + "\n---\nbody",
    ],
)
def test_invalid_metadata_is_visible_not_replaced_with_fallback(tmp_path, content):
    root = tmp_path / "skills" / "review"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(content, encoding="utf-8")
    catalog = SkillCatalog.discover(tmp_path)
    assert not catalog
    assert catalog.diagnostics[0].code == "invalid_package"
    assert catalog.load("review").startswith("Error:")


def test_missing_dependencies_are_advisory_and_do_not_run(tmp_path):
    source = make_skill(
        tmp_path / "source",
        extra="metadata:\n  requires: eventide-impossible-required-command\n",
    )
    preview = preview_directory(source)
    assert preview.valid
    assert preview.diagnostics[0].code == "dependency_missing"
    assert preview.diagnostics[0].severity == "warning"


def test_duplicate_project_names_are_both_unavailable(tmp_path):
    make_skill(tmp_path / "skills" / "a")
    make_skill(tmp_path / "skills" / "b")
    catalog = SkillCatalog.discover(tmp_path)
    assert not catalog
    assert any(item.code == "duplicate_name" for item in catalog.diagnostics)


def test_project_user_collision_requires_scoped_choice_and_selection_cannot_enable(tmp_path):
    make_skill(tmp_path / "skills" / "review")
    service = SkillPackageService(tmp_path / "state")
    installed = service.import_directory(make_skill(tmp_path / "source"))
    catalog = SkillCatalog.discover(
        tmp_path,
        user_packages=(installed,),
        enabled_user=("user:review",),
    )
    assert catalog.load("review").startswith("Error: Ambiguous")
    assert catalog.load("project:review") == catalog.load("user:review")
    assert catalog.tool()["input_schema"]["properties"]["name"]["enum"] == [
        "project:review",
        "user:review",
    ]
    with pytest.raises(SkillPackageError, match="not found"):
        SkillCatalog.discover(
            tmp_path,
            user_packages=(installed,),
            selected=("user:review",),
        )
    assert not SkillCatalog.discover(tmp_path, disabled_project=("project:review",))


def test_resource_reads_are_bounded_and_do_not_allow_paths_or_binary_as_text(tmp_path):
    source = make_skill(tmp_path / "skills" / "review")
    (source / "assets").mkdir()
    (source / "assets" / "image.bin").write_bytes(b"\xff\x00")
    catalog = SkillCatalog.discover(tmp_path)
    assert "Next character offset: 4" in catalog.read_resource(
        "review", "references/rules.md", 0, 4
    )
    assert catalog.read_resource("review", "../secret").startswith("Error:")
    assert catalog.read_resource("review", "assets/image.bin").startswith("Error: Binary")
    assert catalog.read_resource("review", "SKILL.md", -1).startswith("Error:")
    assert catalog.read_resource("review", "SKILL.md", 0, 12001).startswith("Error:")
    with pytest.raises(SkillPackageError):
        catalog.resource_bytes("review", "references/missing.md")


def test_managed_path_identity_and_links_are_checked(tmp_path):
    source = make_skill(tmp_path / "source")
    service = SkillPackageService(tmp_path / "state")
    with pytest.raises(SkillPackageError):
        service.inspect_version("../escape", "a" * 64)
    with pytest.raises(SkillPackageError):
        service.inspect_version("review", "../escape")
    service.root.mkdir(parents=True)
    (service.root / "review").symlink_to(tmp_path / "source")
    with pytest.raises(SkillPackageError, match="symlinks"):
        service.import_directory(source)


def test_archive_duplicate_entry_and_unknown_types_are_rejected(tmp_path):
    source = make_skill(tmp_path / "source")
    data = io.BytesIO(package_zip(source))
    with zipfile.ZipFile(data, "a") as output:
        special = zipfile.ZipInfo("assets/fifo")
        special.create_system = 3
        special.external_attr = (stat.S_IFIFO | 0o600) << 16
        output.writestr(special, "")
    assert not preview_zip(data.getvalue()).valid
    data = io.BytesIO(package_zip(source))
    with pytest.warns(UserWarning, match="Duplicate name"), zipfile.ZipFile(data, "a") as output:
        output.writestr("SKILL.md", "duplicate")
    assert not preview_zip(data.getvalue()).valid


def test_symlink_package_root_is_rejected(tmp_path):
    make_skill(tmp_path / "real")
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    assert not preview_directory(tmp_path / "link").valid


def test_source_resource_mutation_during_preview_is_rejected(tmp_path, monkeypatch):
    import eventide.skill_packages as packages

    source = make_skill(tmp_path / "source")
    original = packages.os.fstat
    changed = False

    def mutate_after_read(fd):
        nonlocal changed
        result = original(fd)
        if not changed:
            changed = True
            (source / "references" / "rules.md").write_text("changed", encoding="utf-8")
        return result

    monkeypatch.setattr(packages.os, "fstat", mutate_after_read)
    preview = preview_directory(source)
    # A source changed during capture is either captured before the corresponding file is read
    # or rejected; no stale digest may claim bytes different from those actually captured.
    if preview.valid:
        rules = next(item for item in preview.resources if item.path == "references/rules.md")
        assert rules.content == b"changed"
    else:
        assert preview.diagnostics


def test_non_utf8_manifest_and_directory_limits_are_reported(tmp_path):
    source = make_skill(tmp_path / "source")
    assert not preview_directory(source, SkillLimits(max_files=1)).valid
    assert not preview_directory(source, SkillLimits(max_total_bytes=10)).valid
    assert not preview_directory(source, SkillLimits(max_file_bytes=10)).valid
    (source / "SKILL.md").write_bytes(b"\xff\xfe")
    assert not preview_directory(source).valid


def test_two_enabled_versions_do_not_silently_choose_latest(tmp_path):
    source = make_skill(tmp_path / "source")
    service = SkillPackageService(tmp_path / "state")
    first = service.import_directory(source)
    (source / "references" / "rules.md").write_text("v2", encoding="utf-8")
    second = service.import_directory(source)
    with pytest.raises(SkillPackageError, match="Multiple enabled"):
        SkillCatalog.discover(
            tmp_path,
            user_packages=(first, second),
            enabled_user=("user:review",),
        )
    catalog = SkillCatalog.discover(
        tmp_path,
        user_packages=(first, second),
        enabled_user=(f"user:review@{first.version}",),
    )
    assert catalog.skills[0].version == first.version


def test_first_party_packages_are_valid_but_not_automatically_enabled(tmp_path):
    from eventide.skills import bundled_skill_directories

    previews = [preview_directory(path) for path in bundled_skill_directories()]
    assert {item.name for item in previews} == {"code-review", "fault-localization", "test-repair"}
    assert all(item.valid for item in previews)
    assert all(
        any(resource.path.startswith("references/") for resource in item.resources)
        for item in previews
    )
    assert not SkillCatalog.discover(tmp_path)
