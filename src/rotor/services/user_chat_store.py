"""User-scoped JSON conversations with process-safe writes and generation leases."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
import uuid


class ChatStoreError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ChatStoreError(404, "Conversation or attachment not found")
    return value


def public_conversation(document: dict) -> dict:
    return {key: value for key, value in document.items() if not key.startswith("_")}


@dataclass
class GenerationLease:
    user_id: int
    conversation_id: str
    message_id: str
    fd: int

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class ChatStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().absolute()

    @staticmethod
    def _directory(parent: int, name: str, *, create: bool = False) -> int:
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
        try:
            return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except OSError as exc:
            raise ChatStoreError(404, "Conversation or attachment not found") from exc

    @staticmethod
    def _open(directory: int, name: str, flags: int) -> int:
        try:
            fd = os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise ChatStoreError(404, "Conversation or attachment not found")
            return fd
        except OSError as exc:
            raise ChatStoreError(404, "Conversation or attachment not found") from exc

    @classmethod
    def _read(cls, directory: int, name: str) -> dict:
        try:
            with os.fdopen(cls._open(directory, name, os.O_RDONLY), "r", encoding="utf-8") as file:
                value = json.load(file)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError) as exc:
            raise ChatStoreError(500, "Conversation data could not be read") from exc

    @classmethod
    def _atomic(cls, directory: int, name: str, content: bytes):
        try:
            existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode):
                raise ChatStoreError(404, "Conversation or attachment not found")
        except FileNotFoundError:
            pass
        temporary = f".write-{uuid.uuid4().hex}"
        try:
            fd = cls._open(directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    @classmethod
    def _write(cls, directory: int, name: str, value: dict):
        cls._atomic(directory, name, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    @contextmanager
    def _user(self, user_id: int):
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            raise ChatStoreError(403, "Invalid user")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ChatStoreError(500, "Chat storage is unavailable") from exc
        try:
            user = self._directory(root, str(user_id), create=True)
            try:
                lock = self._open(user, ".lock", os.O_RDWR | os.O_CREAT)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    yield user
                finally:
                    os.close(lock)
            finally:
                os.close(user)
        finally:
            os.close(root)

    @classmethod
    def _generation_lock(cls, directory: int) -> int:
        fd = cls._open(directory, ".generation.lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError as exc:
            os.close(fd)
            raise ChatStoreError(409, "This conversation is generating a response") from exc

    def _load(self, user: int, directory: int, *, update_index: bool = True) -> dict:
        document = self._read(directory, "conversation.json")
        running = [message for message in document["messages"] if message.get("status") == "running"]
        if running:
            try:
                lock = self._generation_lock(directory)
            except ChatStoreError as exc:
                if exc.status != 409:
                    raise
            else:
                try:
                    for message in running:
                        message.update(status="interrupted", updatedAt=_now())
                    self._save(user, directory, document, update_index=update_index)
                finally:
                    os.close(lock)
        return document

    def _save(self, user: int, directory: int, document: dict, *, update_index: bool = True):
        summaries = self._read_index(user) if update_index else []
        self._atomic(user, ".index-dirty", b"1")
        document["revision"] += 1
        document["updatedAt"] = _now()
        self._write(directory, "conversation.json", document)
        if update_index:
            self._write_index(user, [value for value in summaries if value["id"] != document["id"]]
                              + [self._summary(document)])

    @staticmethod
    def _summary(document: dict) -> dict:
        result = {key: document[key] for key in ("id", "title", "settings", "createdAt", "updatedAt", "revision")}
        result["messageCount"] = len(document["messages"])
        result["preview"] = next((message["content"][:160] for message in reversed(document["messages"]) if message["content"]), "")
        result["generating"] = any(message.get("status") == "running" for message in document["messages"])
        return result

    def _write_index(self, user: int, summaries: list[dict]):
        summaries.sort(key=lambda value: (value["updatedAt"], value["id"]), reverse=True)
        self._write(user, "index.json", {"conversations": summaries})
        try:
            os.unlink(".index-dirty", dir_fd=user)
            os.fsync(user)
        except FileNotFoundError:
            pass

    def _read_index(self, user: int) -> list[dict]:
        try:
            dirty = os.stat(".index-dirty", dir_fd=user, follow_symlinks=False)
            if not stat.S_ISREG(dirty.st_mode):
                raise ChatStoreError(500, "Chat storage is unavailable")
        except FileNotFoundError:
            try:
                summaries = self._read(user, "index.json")["conversations"]
                if not isinstance(summaries, list) or any(
                    not isinstance(value, dict) or not {"id", "title", "settings", "updatedAt", "revision", "generating"} <= value.keys()
                    for value in summaries
                ):
                    raise ValueError
                return summaries
            except (ChatStoreError, KeyError, ValueError):
                pass
        summaries = []
        for name in os.listdir(user):
            if not re.fullmatch(r"[a-f0-9]{32}", name):
                continue
            directory = -1
            try:
                directory = self._directory(user, name)
                summaries.append(self._summary(self._load(user, directory, update_index=False)))
            except ChatStoreError as exc:
                if exc.status != 404:
                    raise
            finally:
                if directory >= 0:
                    os.close(directory)
        self._write_index(user, summaries)
        return summaries

    def list(self, user_id: int) -> list[dict]:
        with self._user(user_id) as user:
            summaries = self._read_index(user)
            for summary in summaries:
                if summary["generating"]:
                    directory = self._directory(user, _id(summary["id"]))
                    try:
                        self._load(user, directory)
                    finally:
                        os.close(directory)
            return self._read_index(user)

    def create(self, user_id: int, *, title: str, settings: dict, draft: dict) -> dict:
        if draft.get("attachments"):
            raise ChatStoreError(400, "Upload attachments after creating the conversation")
        with self._user(user_id) as user:
            summaries = self._read_index(user)
            self._atomic(user, ".index-dirty", b"1")
            conversation_id = uuid.uuid4().hex
            directory = self._directory(user, conversation_id, create=True)
            try:
                timestamp = _now()
                document = {"id": conversation_id, "title": title, "settings": settings,
                            "messages": [], "draft": {"text": draft.get("text", ""), "attachments": []},
                            "createdAt": timestamp, "updatedAt": timestamp, "revision": 1, "draftRevision": 1, "_attachments": {}}
                self._write(directory, "conversation.json", document)
                self._write_index(user, summaries + [self._summary(document)])
                return public_conversation(document)
            finally:
                os.close(directory)

    def get(self, user_id: int, conversation_id: str) -> dict:
        with self._user(user_id) as user:
            directory = self._directory(user, _id(conversation_id))
            try:
                return public_conversation(self._load(user, directory))
            finally:
                os.close(directory)

    @staticmethod
    def _revision(document: dict, expected: int):
        if document["revision"] != expected:
            raise ChatStoreError(409, "Conversation changed; reload before saving")

    @staticmethod
    def _references(document: dict, references: list[dict]) -> list[dict]:
        result = []
        seen = set()
        for reference in references:
            identifier = _id(reference["id"])
            if identifier in seen or identifier not in document["_attachments"]:
                raise ChatStoreError(400, "Attachment is missing or duplicated")
            seen.add(identifier)
            metadata = document["_attachments"][identifier]
            result.append({key: value for key, value in metadata.items() if not key.startswith("_")})
        if len(result) > 6 or sum(value["size"] for value in result) > 10_000_000:
            raise ChatStoreError(400, "Message attachments exceed the limit")
        return result

    def patch(self, user_id: int, conversation_id: str, expected: int, changes: dict, *, expected_draft: int | None = None) -> dict:
        with self._user(user_id) as user:
            directory = self._directory(user, _id(conversation_id))
            lock = -1
            try:
                document = self._load(user, directory)
                draft_only = set(changes) == {"draft"}
                if not draft_only:
                    lock = self._generation_lock(directory)
                if draft_only and expected_draft is not None:
                    if document["draftRevision"] != expected_draft:
                        raise ChatStoreError(409, "Draft changed; reload before saving")
                else:
                    self._revision(document, expected)
                for field in ("title", "settings"):
                    if field in changes:
                        document[field] = changes[field]
                if "draft" in changes:
                    document["draft"] = {"text": changes["draft"]["text"],
                                         "attachments": self._references(document, changes["draft"]["attachments"])}
                    document["draftRevision"] += 1
                self._save(user, directory, document)
                return public_conversation(document)
            finally:
                if lock >= 0:
                    os.close(lock)
                os.close(directory)

    def delete(self, user_id: int, conversation_id: str, expected: int):
        with self._user(user_id) as user:
            identifier = _id(conversation_id)
            directory = self._directory(user, identifier)
            lock = -1
            try:
                document = self._load(user, directory)
                lock = self._generation_lock(directory)
                self._revision(document, expected)
                summaries = self._read_index(user)
                self._atomic(user, ".index-dirty", b"1")
                for name in os.listdir(directory):
                    mode = os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode
                    if name == "attachments" and stat.S_ISDIR(mode):
                        attachments = self._directory(directory, name)
                        try:
                            for filename in os.listdir(attachments):
                                if not stat.S_ISREG(os.stat(filename, dir_fd=attachments, follow_symlinks=False).st_mode):
                                    raise ChatStoreError(409, "Unsafe attachment storage")
                            for filename in os.listdir(attachments):
                                os.unlink(filename, dir_fd=attachments)
                        finally:
                            os.close(attachments)
                        os.rmdir(name, dir_fd=directory)
                    elif stat.S_ISREG(mode):
                        os.unlink(name, dir_fd=directory)
                    else:
                        raise ChatStoreError(409, "Unsafe conversation storage")
                os.rmdir(identifier, dir_fd=user)
                self._write_index(user, [value for value in summaries if value["id"] != identifier])
            finally:
                if lock >= 0:
                    os.close(lock)
                os.close(directory)

    def upload(self, user_id: int, conversation_id: str, *, metadata: dict, content: bytes, extension: str, text: bool) -> dict:
        with self._user(user_id) as user:
            directory = self._directory(user, _id(conversation_id))
            try:
                document = self._load(user, directory)
                if sum(item["size"] for item in document["_attachments"].values()) + len(content) > 20_000_000:
                    raise ChatStoreError(400, "Conversation attachments exceed 20 MB")
                identifier = uuid.uuid4().hex
                filename = identifier + "." + extension
                attachments = self._directory(directory, "attachments", create=True)
                try:
                    self._atomic(attachments, filename, content)
                finally:
                    os.close(attachments)
                public = {"id": identifier, **metadata}
                document["_attachments"][identifier] = {**public, "_filename": filename, "_text": text}
                self._write(directory, "conversation.json", document)
                return public
            finally:
                os.close(directory)

    def attachment(self, user_id: int, conversation_id: str, attachment_id: str) -> dict:
        with self._user(user_id) as user:
            directory = self._directory(user, _id(conversation_id))
            try:
                document = self._load(user, directory)
                metadata = document["_attachments"].get(_id(attachment_id))
                if metadata is None:
                    raise ChatStoreError(404, "Attachment not found")
                attachments = self._directory(directory, "attachments")
                try:
                    filename = metadata["_filename"]
                    if not re.fullmatch(attachment_id + r"\.[a-z0-9]+", filename):
                        raise ChatStoreError(404, "Attachment not found")
                    with os.fdopen(self._open(attachments, filename, os.O_RDONLY), "rb") as file:
                        content = file.read(5_000_001)
                    if len(content) != metadata["size"] or len(content) > 5_000_000:
                        raise ChatStoreError(400, "Attachment data is invalid")
                finally:
                    os.close(attachments)
                result = {"id": attachment_id, "kind": metadata["kind"], "name": metadata["name"],
                          "mime_type": metadata["mimeType"], "size": metadata["size"]}
                if metadata["_text"]:
                    result["content"] = content.decode("utf-8")
                else:
                    result["data_url"] = f"data:{metadata['mimeType']};base64," + base64.b64encode(content).decode("ascii")
                return result
            finally:
                os.close(directory)

    def delete_attachment(self, user_id: int, conversation_id: str, attachment_id: str):
        with self._user(user_id) as user:
            directory = self._directory(user, _id(conversation_id))
            try:
                document = self._load(user, directory)
                identifier = _id(attachment_id)
                references = document["draft"]["attachments"] + [attachment for message in document["messages"] for attachment in message.get("attachments", [])]
                if any(value["id"] == identifier for value in references):
                    raise ChatStoreError(409, "Attachment is still referenced by this conversation")
                metadata = document["_attachments"].pop(identifier, None)
                if metadata is None:
                    raise ChatStoreError(404, "Attachment not found")
                attachments = self._directory(directory, "attachments")
                try:
                    filename = metadata["_filename"]
                    if not re.fullmatch(identifier + r"\.[a-z0-9]+", filename):
                        raise ChatStoreError(404, "Attachment not found")
                    os.unlink(filename, dir_fd=attachments)
                finally:
                    os.close(attachments)
                self._write(directory, "conversation.json", document)
            finally:
                os.close(directory)

    def begin(self, user_id: int, conversation_id: str, expected: int, settings: dict, message: dict) -> tuple[dict, GenerationLease]:
        with self._user(user_id) as user:
            directory = self._directory(user, _id(conversation_id))
            lock = -1
            try:
                document = self._load(user, directory)
                lock = self._generation_lock(directory)
                self._revision(document, expected)
                attachments = self._references(document, message.get("attachments", []))
                if not message["content"].strip() and not attachments:
                    raise ChatStoreError(400, "Message cannot be empty")
                if len(document["messages"]) >= 100:
                    raise ChatStoreError(400, "Conversation has reached the message limit")
                user_message_id = message.get("id") or uuid.uuid4().hex
                if any(value["id"] == user_message_id for value in document["messages"]):
                    raise ChatStoreError(409, "Message has already been sent")
                timestamp = _now()
                assistant_id = uuid.uuid4().hex
                if not document["messages"] and document["title"] in {"新对话", "New conversation", "New chat"}:
                    document["title"] = message["content"].strip()[:80] or attachments[0]["name"][:80]
                document["messages"].extend([
                    {"id": user_message_id, "role": "user", "content": message["content"], "status": "done",
                     "attachments": attachments, "createdAt": timestamp, "updatedAt": timestamp},
                    {"id": assistant_id, "role": "assistant", "content": "", "status": "running",
                     "attachments": [], "model": settings["model"], "createdAt": timestamp, "updatedAt": timestamp},
                ])
                document["settings"] = settings
                document["draft"] = {"text": "", "attachments": []}
                document["draftRevision"] += 1
                self._save(user, directory, document)
                lease = GenerationLease(user_id, conversation_id, assistant_id, lock)
                lock = -1
                return public_conversation(document), lease
            finally:
                if lock >= 0:
                    os.close(lock)
                os.close(directory)

    def checkpoint(self, lease: GenerationLease, content: str, *, status: str = "running", usage: Any = None, error: str | None = None) -> dict:
        if lease.fd < 0:
            raise ChatStoreError(409, "Generation is no longer active")
        with self._user(lease.user_id) as user:
            directory = self._directory(user, lease.conversation_id)
            try:
                document = self._read(directory, "conversation.json")
                message = document["messages"][-1]
                if message["id"] != lease.message_id or message["status"] != "running":
                    raise ChatStoreError(409, "Generation is no longer active")
                message.update(content=content, status=status, updatedAt=_now())
                if usage is not None:
                    message["usage"] = usage
                if error:
                    message["error"] = error
                self._save(user, directory, document)
                return public_conversation(document)
            finally:
                os.close(directory)
