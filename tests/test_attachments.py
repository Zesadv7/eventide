"""Attachment storage boundaries: validation, safe paths, and prompt inlining."""

import base64

import pytest

from eventide.attachments import (
    MAX_INLINE_BYTES,
    MAX_UPLOAD_BYTES,
    attachment_bytes,
    attachment_path,
    compose_message,
    compose_prompt,
    decode_attachment,
    delete_attachment,
    hydrate_messages,
    list_attachments,
    load_attachment,
    load_attachments,
    mark_attachments_used,
    new_attachment_id,
    save_attachment,
)

VALID_ID = "att_0123456789ab"


def encoded(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


@pytest.fixture
def state_dir(isolated_workspace):
    """The conftest temp root is the only sandbox that works on this machine."""
    return isolated_workspace


def test_attachment_ids_match_the_public_format():
    attachment_id = new_attachment_id()
    assert attachment_id.startswith("att_")
    assert len(attachment_id) == len("att_") + 12
    assert attachment_path("state", "s1", attachment_id).name == attachment_id


def test_attachment_save_and_load_roundtrip(state_dir):
    payload = save_attachment(state_dir, "sess1", VALID_ID, "笔记.py", encoded("你好\nworld"))
    assert payload["attachment_id"] == VALID_ID
    assert payload["name"] == "笔记.py"
    assert payload["size"] == len("你好\nworld".encode())
    assert payload["kind"] == "text"
    assert payload["media_type"].startswith("text/")
    assert payload["used"] is False
    loaded = load_attachment(state_dir, "sess1", VALID_ID)
    assert loaded["attachment_id"] == VALID_ID
    assert loaded["name"] == "笔记.py"
    assert loaded["content"] == "你好\nworld"
    assert load_attachments(state_dir, "sess1", [VALID_ID]) == [loaded]


def test_binary_lifecycle_and_multimodal_reference_hydration(state_dir):
    raw = b"\x89PNG\r\n\x1a\nimage"
    payload = save_attachment(
        state_dir,
        "sess1",
        VALID_ID,
        "diagram.png",
        base64.b64encode(raw).decode("ascii"),
        "image/png",
    )
    assert payload["kind"] == "image"
    assert list_attachments(state_dir, "sess1") == [payload]
    loaded = load_attachment(state_dir, "sess1", VALID_ID)
    assert attachment_bytes(loaded) == raw

    message = compose_message("看图", [loaded])
    reference = message["content"][1]
    assert reference == {
        "type": "attachment",
        "attachment_id": VALID_ID,
        "name": "diagram.png",
        "media_type": "image/png",
        "kind": "image",
        "size": len(raw),
    }
    hydrated = hydrate_messages(state_dir, "sess1", [message])
    assert hydrated[0]["content"][1] == {
        "type": "image",
        "name": "diagram.png",
        "media_type": "image/png",
        "data": base64.b64encode(raw).decode("ascii"),
    }

    mark_attachments_used(state_dir, "sess1", [VALID_ID])
    assert list_attachments(state_dir, "sess1") == []
    assert list_attachments(state_dir, "sess1", include_used=True)[0]["used"] is True
    with pytest.raises(ValueError, match="不能删除"):
        delete_attachment(state_dir, "sess1", VALID_ID)


def test_pending_attachment_can_be_deleted(state_dir):
    save_attachment(
        state_dir,
        "sess1",
        VALID_ID,
        "a.bin",
        encoded("raw"),
        "application/octet-stream",
    )
    deleted = delete_attachment(state_dir, "sess1", VALID_ID)
    assert deleted["kind"] == "file"
    assert list_attachments(state_dir, "sess1", include_used=True) == []


def test_decode_attachment_rejects_bad_base64_oversize_and_binary():
    with pytest.raises(ValueError, match="附件缺少名称"):
        decode_attachment("   ", encoded("x"))
    with pytest.raises(ValueError, match="附件内容不是有效的 base64"):
        decode_attachment("a.txt", "not!!valid")
    # A missing payload degrades to an empty (but valid) UTF-8 text attachment.
    assert decode_attachment("a.txt", None) == ""
    with pytest.raises(ValueError, match="暂仅支持 UTF-8 文本附件"):
        decode_attachment("a.bin", base64.b64encode(b"\xff\xfe\x00binary").decode("ascii"))


def test_decode_attachment_size_cap_boundary():
    with pytest.raises(ValueError, match="附件超过 10MB 上限"):
        decode_attachment("a.txt", encoded("x" * (MAX_UPLOAD_BYTES + 1)))
    # Exactly at the cap is accepted, so the pre-decode gate never over-rejects.
    assert decode_attachment("a.txt", encoded("x" * MAX_UPLOAD_BYTES)) == "x" * MAX_UPLOAD_BYTES


def test_attachment_path_rejects_illegal_ids(state_dir):
    for bad in [
        "att_0123456789a",  # 11 hex
        "att_0123456789abc",  # 13 hex
        "att_0123456789AB",  # uppercase
        "../att_0123456789ab",  # traversal
        "/att_0123456789ab",  # absolute
        "C:\\att_0123456789ab",  # absolute windows
        "att_0123456789ab\n",  # trailing newline would slip past a bare $
        "att_",
        "",
        123,
        None,
    ]:
        with pytest.raises(ValueError, match="非法附件 id"):
            attachment_path(state_dir, "sess1", bad)


def test_attachment_path_never_traverses_through_the_session_id(state_dir):
    # A ".." session id must not escape the attachments directory.
    path = attachment_path(state_dir, "..", VALID_ID)
    assert ".." not in path.parts
    assert path == state_dir / "attachments" / "_.." / VALID_ID
    # Separators and reserved characters are flattened into one component.
    assert attachment_path(state_dir, "a/b\\c:d e", VALID_ID) == (
        state_dir / "attachments" / "a_b_c_d_e" / VALID_ID
    )
    # Windows device names cannot be directory names.
    assert attachment_path(state_dir, "CON", VALID_ID).parts[-2] == "_CON"
    assert attachment_path(state_dir, "nul", VALID_ID).parts[-2] == "_nul"
    assert attachment_path(state_dir, "", VALID_ID).parts[-2] == "unknown"


def test_load_attachment_missing_foreign_and_corrupt(state_dir):
    save_attachment(state_dir, "s1", VALID_ID, "a.txt", encoded("hello"))
    # A foreign session id cannot see the file: same store, different directory.
    with pytest.raises(ValueError, match=f"附件不存在：{VALID_ID}"):
        load_attachment(state_dir, "s2", VALID_ID)
    with pytest.raises(ValueError, match="附件不存在"):
        load_attachment(state_dir, "s1", "att_ffffffffffff")
    with pytest.raises(ValueError, match="非法附件 id"):
        load_attachments(state_dir, "s1", ["../evil"])


def test_load_attachment_survives_a_corrupted_store_file(state_dir):
    corrupt = state_dir / "attachments" / "s1" / "att_ffffffffffff"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="附件数据损坏：att_ffffffffffff"):
        load_attachment(state_dir, "s1", "att_ffffffffffff")
    # A non-object document is corruption too, not a confusing AttributeError.
    corrupt.write_text('["a list"]', encoding="utf-8")
    with pytest.raises(ValueError, match="附件数据损坏：att_ffffffffffff"):
        load_attachment(state_dir, "s1", "att_ffffffffffff")


def test_compose_prompt_without_attachments_is_unchanged():
    assert compose_prompt("只改这一处", []) == "只改这一处"
    assert compose_prompt("原样", None) == "原样"


def test_compose_prompt_inlines_attachments_with_delimiters():
    result = compose_prompt(
        "请看这两个文件",
        [
            {"attachment_id": "att_a", "name": "a.py", "content": "print(1)"},
            {"attachment_id": "att_b", "name": "b.md", "content": "# hi"},
        ],
    )
    assert result.startswith("请看这两个文件")
    assert "print(1)" in result and "# hi" in result
    assert result.index("--- 附件：a.py ---") < result.index("--- 附件：b.md ---")
    # An attachment without a usable name still gets a header.
    assert "--- 附件：未命名 ---" in compose_prompt("p", [{"content": "x"}])
    assert "--- 附件：empty.txt ---" in compose_prompt(
        "p", [{"name": "empty.txt", "content": None}]
    )


def test_compose_prompt_truncates_on_character_boundaries():
    text = "a" + "你" * 200000  # 1 + 600000 bytes, well over the inline budget
    result = compose_prompt("看", [{"name": "big.txt", "content": text}])
    assert "[附件内容过长，已截断]" in result
    body = result.split("--- 附件：big.txt ---\n", 1)[1].rsplit("\n[附件内容过长，已截断]", 1)[0]
    # 524288 bytes cut after "a" keeps 524287 bytes = 174762 whole characters,
    # and the final two stray bytes of a "你" are dropped instead of rendered.
    assert body == "a" + "你" * ((MAX_INLINE_BYTES - 1) // 3)
    assert len(body.encode("utf-8")) <= MAX_INLINE_BYTES
    assert "\ufffd" not in result


def test_compose_prompt_keeps_content_exactly_at_the_budget():
    exact = "x" * MAX_INLINE_BYTES
    result = compose_prompt("p", [{"name": "ok.txt", "content": exact}])
    assert "[附件内容过长，已截断]" not in result
    assert exact in result
