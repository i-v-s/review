from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def uid() -> str:
    return uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileVersion(Model):
    exists: bool = False
    mode: str = "100644"
    content: str = ""
    omitted_hash: str | None = None
    permissions: int | None = None

    @classmethod
    def of(cls, data: bytes | None, mode: str = "100644") -> FileVersion:
        return cls(exists=data is not None, mode=mode,
                   content=base64.b64encode(data or b"").decode())

    def bytes(self) -> bytes:
        return base64.b64decode(self.content)

    def fingerprint(self) -> str:
        return digest(f"{self.exists}:{self.mode}:{self.omitted_hash or ''}:".encode() + self.bytes())


class FileState(Model):
    path: str
    head: FileVersion
    index: FileVersion
    work: FileVersion
    unsupported: str | None = None
    dirty: bool = True


class Snapshot(Model):
    id: str = Field(default_factory=uid)
    created_at: str = Field(default_factory=now)
    version: str
    head: str | None
    index_content: str
    index_version: str
    files: list[FileState]
    omitted_untracked: list[str] = Field(default_factory=list)


class SourceEvent(Model):
    id: str
    provider: str
    session_id: str
    source_id: str
    role: str
    text: str
    timestamp: str | None = None
    raw: dict = Field(default_factory=dict)


class ReviewItem(Model):
    id: str = Field(default_factory=uid)
    section: str
    title: str
    explanation: str
    rationale_kind: Literal["recorded", "reconstructed", "unknown"] = "unknown"
    argument: str = ""
    limitations: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    fragment_ids: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    reviewed: bool = False


class Finding(Model):
    id: str = Field(default_factory=uid)
    severity: Literal["info", "warning", "error"] = "warning"
    title: str
    description: str
    suggestion: str = ""
    source_ids: list[str] = Field(default_factory=list)
    fragment_ids: list[str] = Field(default_factory=list)


class GeneratedReport(Model):
    summary: str
    items: list[ReviewItem]
    findings: list[Finding] = Field(default_factory=list)


class OperationRequest(Model):
    snapshot_id: str
    expected_version: str
    path: str
    action: Literal["stage", "unstage", "discard"]
    line_ids: list[str] = Field(default_factory=list, max_length=10000)
    whole_file: bool = False
    key: str = Field(min_length=8, max_length=128)


class ReviewError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
