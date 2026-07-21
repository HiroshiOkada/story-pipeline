"""実行プロセス間で共有する、保守的な排他ロック。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
from typing import Any

from story_pipeline.errors import StoryPipelineError


@dataclass(frozen=True, slots=True)
class LockRecord:
    schema_version: int
    pid: int
    hostname: str
    started_at: str
    request_number: int | None


class RunLock:
    """排他的に作成し、取得したプロセスだけが解放できるロック。"""

    def __init__(self, path: Path, record: LockRecord) -> None:
        self.path = path
        self.record = record
        self._held = True

    @classmethod
    def acquire(cls, root: Path, request_number: int | None = None) -> RunLock:
        path = root / ".story-pipeline" / "run.lock"
        record = LockRecord(
            schema_version=1,
            pid=os.getpid(),
            hostname=socket.gethostname(),
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            request_number=request_number,
        )
        payload = _encode(record)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise _lock_conflict(path, root) from None
        except OSError as error:
            raise _lock_io_error("実行ロックを作成できません", path) from error
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            try:
                path.unlink()
            except OSError:
                pass
            raise _lock_io_error("実行ロックを書き込めません", path) from error
        return cls(path, record)

    def update_request(self, request_number: int) -> None:
        if request_number < 0 or request_number > 9999:
            raise ValueError("request_number は 0..9999 である必要があります")
        self._require_ownership()
        updated = replace(self.record, request_number=request_number)
        temporary = self.path.with_name(f".{self.path.name}.{self.record.pid}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_encode(updated))
                stream.flush()
                os.fsync(stream.fileno())
            self._require_ownership()
            os.replace(temporary, self.path)
        except OSError as error:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise _lock_io_error("実行ロックを更新できません", self.path) from error
        self.record = updated

    def release(self) -> None:
        if not self._held:
            return
        self._require_ownership()
        try:
            self.path.unlink()
        except OSError as error:
            raise _lock_io_error("実行ロックを解放できません", self.path) from error
        self._held = False

    def __enter__(self) -> RunLock:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()

    def _require_ownership(self) -> None:
        if not self._held or _read_record(self.path) != self.record:
            raise _lock_io_error("実行ロックの所有権を確認できません", self.path)


def _lock_conflict(path: Path, root: Path) -> StoryPipelineError:
    try:
        record = _read_record(path)
    except StoryPipelineError as error:
        return StoryPipelineError(
            "既存の実行ロックを安全に判定できません",
            str(path),
            error.action,
            6,
        )
    now = datetime.now(timezone.utc)
    try:
        started = datetime.fromisoformat(record.started_at.replace("Z", "+00:00"))
    except ValueError:
        return _unsafe_existing_lock(path)
    if started.tzinfo is None or started > now:
        return _unsafe_existing_lock(path)
    if record.hostname != socket.gethostname():
        return _unsafe_existing_lock(path)
    try:
        os.kill(record.pid, 0)
    except ProcessLookupError:
        if _has_running_record(root, record.request_number):
            return StoryPipelineError(
                "停止した実行のロックが残っています",
                str(path),
                "validate の表示とプロセス不存在を確認してから手動で削除してください",
                6,
            )
        return _unsafe_existing_lock(path)
    except PermissionError:
        return _unsafe_existing_lock(path)
    return StoryPipelineError(
        "別の story-pipeline run が実行中です",
        str(path),
        f"PID {record.pid} の終了を待ってから再実行してください",
        6,
    )


def _read_record(path: Path) -> LockRecord:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "pid", "hostname", "started_at", "request_number"
        }:
            raise ValueError
        record = LockRecord(**value)
        if (
            type(record.schema_version) is not int
            or record.schema_version != 1
            or type(record.pid) is not int
            or record.pid <= 0
            or not isinstance(record.hostname, str)
            or not record.hostname
            or not isinstance(record.started_at, str)
            or (record.request_number is not None and type(record.request_number) is not int)
            or (
                record.request_number is not None
                and not 0 <= record.request_number <= 9999
            )
        ):
            raise ValueError
        return record
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise _lock_io_error("実行ロックを読み取れません", path) from error


def _has_running_record(root: Path, request_number: int | None) -> bool:
    if request_number is None:
        return False
    path = root / ".story-pipeline" / "runs" / f"{request_number:04d}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("request_number") == request_number and value.get("status") == "running"


def _encode(record: LockRecord) -> bytes:
    return (json.dumps(asdict(record), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _unsafe_existing_lock(path: Path) -> StoryPipelineError:
    return StoryPipelineError(
        "既存の実行ロックを安全に stale と判断できません",
        str(path),
        "ロック内容とプロセスを確認してください。自動削除は行いません",
        6,
    )


def _lock_io_error(reason: str, path: Path) -> StoryPipelineError:
    return StoryPipelineError(reason, str(path), "ファイルの状態とアクセス権を確認してください", 9)
