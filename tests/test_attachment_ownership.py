"""Attachment identities cannot alias sessions or follow state-root links."""

import base64
import json
import os
from pathlib import Path

import pytest

from eventide import attachments

VALID_ID = "att_0123456789ab"
SECOND_ID = "att_0123456789ac"


def encoded(text: str) -> str:
    return base64.b64encode(text.encode()).decode("ascii")


def old_record(state: Path, session_id: str, attachment_id: str = VALID_ID) -> Path:
    path = state / "attachments" / session_id / attachment_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": "old.txt", "text": "old contents"}), encoding="utf-8")
    return path


def test_distinct_session_identities_have_independent_storage(isolated_workspace):
    state = isolated_workspace
    sessions = ["team:a", "team_a", "a", "A", "a/b", "a_b", "..", "_..", "CON", "_CON"]
    paths = [attachments.attachment_path(state, session, VALID_ID) for session in sessions]
    assert len(set(paths)) == len(sessions)
    for session in sessions:
        attachments.save_attachment(state, session, VALID_ID, "a.txt", encoded(session))
    for session in sessions:
        assert attachments.load_attachment(state, session, VALID_ID)["content"] == session
        assert len(attachments.list_attachments(state, session)) == 1
    attachments.mark_attachments_used(state, "team:a", [VALID_ID])
    assert not attachments.load_attachment(state, "team_a", VALID_ID)["used"]
    attachments.delete_attachment(state, "A", VALID_ID)
    assert attachments.load_attachment(state, "a", VALID_ID)["content"] == "a"


def test_exact_owner_is_internal_metadata_only(isolated_workspace):
    state = isolated_workspace
    public = attachments.save_attachment(state, "team:a", VALID_ID, "a.txt", encoded("content"))
    meta = json.loads(attachments.attachment_path(state, "team:a", VALID_ID).read_text())
    assert meta["session_id"] == "team:a"
    assert meta["version"] == 3
    assert set(public) == {
        "attachment_id", "name", "size", "media_type", "kind", "created_at", "used"
    }
    assert "session_id" not in attachments.load_attachment(state, "team:a", VALID_ID)


@pytest.mark.parametrize("operation", ["load", "load_many", "list", "mark", "delete", "hydrate"])
def test_every_reader_rejects_wrong_owner_metadata(isolated_workspace, operation):
    state = isolated_workspace
    attachments.save_attachment(state, "owner", VALID_ID, "a.png", encoded("image"), "image/png")
    source = attachments.attachment_path(state, "owner", VALID_ID)
    foreign = attachments.attachment_path(state, "foreign", VALID_ID)
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(source.read_bytes())
    before = foreign.read_bytes()
    operations = {
        "load": lambda: attachments.load_attachment(state, "foreign", VALID_ID),
        "load_many": lambda: attachments.load_attachments(state, "foreign", [VALID_ID]),
        "list": lambda: attachments.list_attachments(state, "foreign", include_used=True),
        "mark": lambda: attachments.mark_attachments_used(state, "foreign", [VALID_ID]),
        "delete": lambda: attachments.delete_attachment(state, "foreign", VALID_ID),
        "hydrate": lambda: attachments.hydrate_messages(state, "foreign", [{
            "role": "user", "content": [{"type": "attachment", "attachment_id": VALID_ID}]
        }]),
    }
    with pytest.raises(ValueError, match="不属于当前会话"):
        operations[operation]()
    assert foreign.read_bytes() == before
    assert source.read_bytes() == before


def test_hashed_directory_does_not_accept_ownerless_record(isolated_workspace):
    state = isolated_workspace
    path = attachments.attachment_path(state, "safe_session", VALID_ID)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"name": "a.txt", "text": "unowned"}))
    with pytest.raises(ValueError, match="会话归属"):
        attachments.load_attachment(state, "safe_session", VALID_ID)


def test_safe_ownerless_legacy_files_remain_accessible_without_moving(isolated_workspace):
    state = isolated_workspace
    old = old_record(state, "safe_session")
    assert attachments.load_attachment(state, "safe_session", VALID_ID)["content"] == "old contents"
    attachments.save_attachment(state, "safe_session", SECOND_ID, "new.txt", encoded("new"))
    assert len(attachments.list_attachments(state, "safe_session")) == 2
    attachments.mark_attachments_used(state, "safe_session", [VALID_ID])
    assert old.exists()
    assert not attachments.attachment_path(state, "safe_session", VALID_ID).exists()
    meta = json.loads(old.read_text())
    assert meta["session_id"] == "safe_session"
    assert meta["version"] == 3
    assert meta["used"] is True
    assert attachments.list_attachments(state, "safe_session")[0]["attachment_id"] == SECOND_ID


def test_unused_legacy_file_can_be_deleted(isolated_workspace):
    old = old_record(isolated_workspace, "safe_session")
    deleted = attachments.delete_attachment(isolated_workspace, "safe_session", VALID_ID)
    assert deleted["kind"] == "text"
    assert not old.exists()


def test_legacy_unsafe_id_never_uses_the_flattened_directory(isolated_workspace):
    state = isolated_workspace
    old = old_record(state, "team_a")
    with pytest.raises(ValueError, match="附件不存在"):
        attachments.load_attachment(state, "team:a", VALID_ID)
    assert attachments.list_attachments(state, "team:a") == []
    attachments.save_attachment(state, "team:a", VALID_ID, "new.txt", encoded("new"))
    assert attachments.load_attachment(state, "team:a", VALID_ID)["content"] == "new"
    assert json.loads(old.read_text())["text"] == "old contents"


def test_legacy_uppercase_ownerless_record_is_ambiguous(isolated_workspace):
    state = isolated_workspace
    old = old_record(state, "Legacy")
    with pytest.raises(ValueError, match="会话归属"):
        attachments.load_attachment(state, "Legacy", VALID_ID)
    meta = json.loads(old.read_text())
    meta["session_id"] = "Legacy"
    old.write_text(json.dumps(meta))
    assert attachments.load_attachment(state, "Legacy", VALID_ID)["content"] == "old contents"


def test_legacy_case_alias_cannot_block_an_independent_new_session(isolated_workspace):
    state = isolated_workspace
    old = old_record(state, "Legacy")
    attachments.save_attachment(state, "legacy", VALID_ID, "new.txt", encoded("new content"))
    assert attachments.load_attachment(state, "legacy", VALID_ID)["content"] == "new content"
    assert len(attachments.list_attachments(state, "legacy")) == 1
    assert json.loads(old.read_text())["text"] == "old contents"


def test_legacy_record_with_explicit_other_owner_is_refused(isolated_workspace):
    state = isolated_workspace
    old = old_record(state, "team_a")
    meta = json.loads(old.read_text())
    meta["session_id"] = "team:a"
    old.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="不属于当前会话"):
        attachments.load_attachment(state, "team_a", VALID_ID)


@pytest.mark.parametrize("invalid", ["att_ffffffffffff", "../invalid", SECOND_ID])
def test_mark_validates_all_ids_before_changing_any_file(isolated_workspace, invalid):
    state = isolated_workspace
    attachments.save_attachment(state, "owner", VALID_ID, "a.txt", encoded("content"))
    if invalid == SECOND_ID:
        attachments.save_attachment(state, "foreign", SECOND_ID, "b.txt", encoded("other"))
        foreign = attachments.attachment_path(state, "owner", SECOND_ID)
        foreign.write_bytes(attachments.attachment_path(state, "foreign", SECOND_ID).read_bytes())
    before = attachments.attachment_path(state, "owner", VALID_ID).read_bytes()
    with pytest.raises(ValueError):
        attachments.mark_attachments_used(state, "owner", [VALID_ID, invalid])
    assert attachments.attachment_path(state, "owner", VALID_ID).read_bytes() == before
    assert attachments.load_attachment(state, "owner", VALID_ID)["used"] is False


def test_duplicate_save_cannot_modify_an_existing_reference(isolated_workspace):
    state = isolated_workspace
    attachments.save_attachment(state, "owner", VALID_ID, "a.txt", encoded("original"))
    attachments.mark_attachments_used(state, "owner", [VALID_ID])
    with pytest.raises(ValueError, match="id 已存在"):
        attachments.save_attachment(state, "owner", VALID_ID, "a.txt", encoded("changed"))
    assert attachments.load_attachment(state, "owner", VALID_ID)["content"] == "original"


def test_duplicate_legacy_save_cannot_modify_an_existing_reference(isolated_workspace):
    state = isolated_workspace
    old = old_record(state, "owner")
    before = old.read_bytes()
    with pytest.raises(ValueError, match="id 已存在"):
        attachments.save_attachment(state, "owner", VALID_ID, "a.txt", encoded("changed"))
    assert old.read_bytes() == before


@pytest.mark.parametrize("boundary", ["root", "session", "file"])
def test_symlink_escape_is_refused_for_reads_and_writes(isolated_workspace, boundary):
    state = isolated_workspace / "state"
    outside = isolated_workspace / "outside"
    outside.mkdir()
    source = old_record(outside, "source")
    path = attachments.attachment_path(state, "owner", VALID_ID)
    if boundary == "root":
        state.mkdir()
        link, target = state / "attachments", outside
    elif boundary == "session":
        path.parent.parent.mkdir(parents=True)
        link, target = path.parent, source.parent
    else:
        path.parent.mkdir(parents=True)
        link, target = path, source
    try:
        link.symlink_to(target, target_is_directory=boundary != "file")
    except (OSError, NotImplementedError):
        pytest.skip("This system does not allow test symlinks")
    before = source.read_bytes()
    with pytest.raises(ValueError, match="符号链接"):
        attachments.load_attachment(state, "owner", VALID_ID)
    with pytest.raises(ValueError, match="符号链接"):
        attachments.save_attachment(state, "owner", VALID_ID, "new.txt", encoded("changed"))
    assert source.read_bytes() == before


def test_replacing_owner_flag_is_an_atomic_json_publication(isolated_workspace, monkeypatch):
    state = isolated_workspace
    attachments.save_attachment(state, "owner", VALID_ID, "a.txt", encoded("content"))
    path = attachments.attachment_path(state, "owner", VALID_ID)
    before = path.read_bytes()

    def failed_replace(*args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", failed_replace)
    with pytest.raises(OSError, match="replace failure"):
        attachments.mark_attachments_used(state, "owner", [VALID_ID])
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]
