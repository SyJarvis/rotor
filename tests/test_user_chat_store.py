import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from rotor.services.user_chat_store import ChatStore, ChatStoreError


def create(store, user=1):
    return store.create(user, title="New chat", settings={"model": "model-a"}, draft={})


def test_user_isolation_revision_conflict_and_durable_index(tmp_path):
    store = ChatStore(tmp_path)
    first, other = create(store), create(store, 2)
    assert [value["id"] for value in store.list(1)] == [first["id"]]
    with pytest.raises(ChatStoreError) as caught:
        store.get(2, first["id"])
    assert caught.value.status == 404
    saved = store.patch(1, first["id"], 1, {"title": "Saved", "draft": {"text": "draft", "attachments": []}})
    assert saved["revision"] == 2 and saved["draftRevision"] == 2
    with pytest.raises(ChatStoreError) as caught:
        store.patch(1, first["id"], 1, {"title": "Lost update"})
    assert caught.value.status == 409
    assert ChatStore(tmp_path).get(1, first["id"])["title"] == "Saved"
    assert store.get(2, other["id"])["messages"] == []
    store.delete(1, first["id"], 2)
    assert store.list(1) == []


def test_lists_read_index_and_checkpoints_do_not_read_other_conversations(tmp_path, monkeypatch):
    store = ChatStore(tmp_path)
    first, second = create(store), create(store)
    read = store._read
    reads = []

    def track(directory, name):
        reads.append(name)
        return read(directory, name)

    monkeypatch.setattr(store, "_read", track)
    assert len(store.list(1)) == 2
    assert "conversation.json" not in reads
    doc, lease = store.begin(1, first["id"], 1, {"model": "model-a"}, {"content": "A meaningful title"})
    try:
        reads.clear()
        store.checkpoint(lease, "partial")
        assert reads.count("conversation.json") == 1
        assert store.get(1, second["id"])["messages"] == []
        assert doc["title"] == "A meaningful title"
    finally:
        lease.close()


@pytest.mark.parametrize("mode", ["missing", "corrupt", "dirty"])
def test_index_can_be_rebuilt_after_missing_corrupt_or_interrupted_index_write(tmp_path, mode):
    store = ChatStore(tmp_path)
    document = create(store)
    index = tmp_path / "1/index.json"
    if mode == "missing":
        index.unlink()
    elif mode == "corrupt":
        index.write_text("incomplete JSON")
    else:
        index.write_text('{"conversations": []}')
        (tmp_path / "1/.index-dirty").write_text("1")
    assert store.list(1)[0]["id"] == document["id"]
    assert not (tmp_path / "1/.index-dirty").exists()


def test_failed_create_does_not_leave_a_broken_conversation(tmp_path):
    store = ChatStore(tmp_path)
    with pytest.raises(ChatStoreError):
        store.create(1, title="Bad", settings={}, draft={"attachments": [{"id": "a" * 32}]})
    assert store.list(1) == []
    document = create(store)
    assert store.list(1) == [store._summary(document)]


def test_generation_lease_allows_reads_and_independent_draft_revisions(tmp_path):
    store = ChatStore(tmp_path)
    document = create(store)
    running, lease = store.begin(1, document["id"], 1, {"model": "model-a"}, {"content": "hello"})
    try:
        assert store.get(1, document["id"])["messages"][-1]["status"] == "running"
        with pytest.raises(ChatStoreError) as caught:
            store.begin(1, document["id"], running["revision"], {"model": "model-a"}, {"content": "racing"})
        assert caught.value.status == 409
        store.checkpoint(lease, "partial")
        draft = store.patch(1, document["id"], running["revision"], {"draft": {"text": "next turn", "attachments": []}}, expected_draft=running["draftRevision"])
        with pytest.raises(ChatStoreError) as caught:
            store.patch(1, document["id"], draft["revision"], {"draft": {"text": "stale", "attachments": []}}, expected_draft=running["draftRevision"])
        assert caught.value.status == 409
        final = store.checkpoint(lease, "answer", status="done", usage={"total_tokens": 5})
        assert final["draft"]["text"] == "next turn"
        assert final["messages"][-1]["usage"] == {"total_tokens": 5}
    finally:
        lease.close()


def test_atomic_failure_keeps_previous_json_and_rebuilds_index(tmp_path, monkeypatch):
    store = ChatStore(tmp_path)
    document = create(store)
    original = os.replace

    def fail(source, destination, **kwargs):
        if destination == "conversation.json":
            raise OSError("simulated disk failure")
        return original(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        store.patch(1, document["id"], 1, {"title": "not committed"})
    monkeypatch.setattr(os, "replace", original)
    assert store.get(1, document["id"])["revision"] == 1
    assert store.list(1)[0]["title"] == "New chat"
    assert not list(tmp_path.rglob(".write-*"))


def test_two_processes_cannot_overwrite_the_same_revision(tmp_path):
    store = ChatStore(tmp_path)
    document = create(store)
    script = """
import sys
from rotor.services.user_chat_store import ChatStore, ChatStoreError
try:
    value=ChatStore(sys.argv[1]).patch(1, sys.argv[2], 1, {'title':sys.argv[3]})
    print('saved')
except ChatStoreError as exc:
    print(exc.status)
"""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), document["id"], title], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for title in ("one", "two")]
    results = [process.communicate(timeout=10) for process in processes]
    assert sorted(stdout.strip() for stdout, _ in results) == ["409", "saved"]
    assert all(process.returncode == 0 for process in processes)
    assert store.get(1, document["id"])["revision"] == 2


def test_process_exit_recovers_checkpoint_as_interrupted(tmp_path):
    store = ChatStore(tmp_path)
    document = create(store)
    script = """
import os,sys
from rotor.services.user_chat_store import ChatStore
store=ChatStore(sys.argv[1])
doc,lease=store.begin(1,sys.argv[2],1,{'model':'model-a'},{'content':'hello'})
store.checkpoint(lease,'persisted partial')
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(tmp_path), document["id"]], check=True, timeout=10, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert store.list(1)[0]["generating"] is False
    recovered = store.get(1, document["id"])
    assert recovered["messages"][-1]["status"] == "interrupted"
    assert recovered["messages"][-1]["content"] == "persisted partial"


def test_generation_lease_excludes_another_process_while_allowing_reads(tmp_path):
    store = ChatStore(tmp_path)
    document = create(store)
    running, lease = store.begin(1, document["id"], 1, {"model": "model-a"}, {"content": "hello"})
    script = """
import sys
from rotor.services.user_chat_store import ChatStore, ChatStoreError
store=ChatStore(sys.argv[1])
doc=store.get(1,sys.argv[2])
assert doc['messages'][-1]['status']=='running'
try:
    store.begin(1,doc['id'],doc['revision'],{'model':'model-a'},{'content':'racing'})
except ChatStoreError as exc:
    assert exc.status==409
else:
    raise AssertionError('second generation admitted')
print('readable, generation locked')
"""
    try:
        result = subprocess.run([sys.executable, "-c", script, str(tmp_path), document["id"]], check=True, capture_output=True, text=True, timeout=10, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        assert result.stdout.strip() == "readable, generation locked"
    finally:
        lease.close()


def test_attachments_are_single_files_references_and_cannot_be_deleted_while_used(tmp_path):
    store = ChatStore(tmp_path)
    document = create(store)
    metadata = store.upload(1, document["id"], metadata={"kind": "file", "name": "../notes.txt", "mimeType": "text/plain", "size": 5}, content=b"facts", extension="txt", text=True)
    path = tmp_path / "1" / document["id"]
    assert list((path / "attachments").iterdir()) == [path / "attachments" / (metadata["id"] + ".txt")]
    assert "facts" not in (path / "conversation.json").read_text()
    assert store.attachment(1, document["id"], metadata["id"])["content"] == "facts"
    draft = store.patch(1, document["id"], 1, {"draft": {"text": "", "attachments": [{"id": metadata["id"]}]}})
    with pytest.raises(ChatStoreError) as caught:
        store.delete_attachment(1, document["id"], metadata["id"])
    assert caught.value.status == 409
    running, lease = store.begin(1, document["id"], draft["revision"], {"model": "model-a"}, {"content": "", "attachments": [{"id": metadata["id"]}]})
    try:
        with pytest.raises(ChatStoreError):
            store.delete_attachment(1, document["id"], metadata["id"])
        final = store.checkpoint(lease, "answer", status="done")
    finally:
        lease.close()
    store.delete(1, document["id"], final["revision"])
    assert not path.exists()


@pytest.mark.parametrize("location", ["user", "conversation", "document", "attachment", "root"])
def test_symlinks_never_expose_external_files(tmp_path, location):
    root = tmp_path / "chats"
    store = ChatStore(root)
    document = create(store)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_text("private")
    conversation = root / "1" / document["id"]
    if location == "user":
        (root / "2").symlink_to(root / "1", target_is_directory=True)
        action = lambda: store.get(2, document["id"])
    elif location == "conversation":
        (root / "1" / ("a" * 32)).symlink_to(conversation, target_is_directory=True)
        action = lambda: store.get(1, "a" * 32)
    elif location == "document":
        (conversation / "conversation.json").unlink()
        (conversation / "conversation.json").symlink_to(secret)
        action = lambda: store.get(1, document["id"])
    elif location == "root":
        link = tmp_path / "linked"
        link.symlink_to(root, target_is_directory=True)
        action = lambda: ChatStore(link).get(1, document["id"])
    else:
        metadata = store.upload(1, document["id"], metadata={"kind": "file", "name": "x.txt", "mimeType": "text/plain", "size": 7}, content=b"content", extension="txt", text=True)
        attachment = conversation / "attachments" / (metadata["id"] + ".txt")
        attachment.unlink()
        attachment.symlink_to(secret)
        action = lambda: store.attachment(1, document["id"], metadata["id"])
    with pytest.raises(ChatStoreError):
        action()
    assert secret.read_text() == "private"


@pytest.mark.parametrize("identifier", ["..", "../2", "/etc/passwd", "a/../b", "a" * 31])
def test_path_traversal_is_rejected(tmp_path, identifier):
    with pytest.raises(ChatStoreError) as caught:
        ChatStore(tmp_path).get(1, identifier)
    assert caught.value.status == 404
