"""実行状態と報告に共通する、安全で原子的なファイル操作。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from story_pipeline.errors import StoryPipelineError


def atomic_write_json(path: Path, value: Any) -> None:
    """厳密な JSON をインデント2、末尾改行付きで原子的に保存する。"""
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, payload)


def atomic_write_text(path: Path, content: str) -> None:
    """UTF-8 テキストを同一ディレクトリ内の一時ファイル経由で保存する。"""
    parent = path.parent
    _require_directory(parent)
    _require_replaceable_target(path)
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _require_replaceable_target(path)
        os.replace(temporary, path)
        temporary = None
        _sync_directory(parent)
    except (OSError, UnicodeError) as error:
        raise _io_error("ファイルを原子的に保存できません", path) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def sha256_file(path: Path) -> str:
    """安全な通常ファイルの生バイト列に対する SHA-256 を返す。"""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise OSError("通常ファイルではありません")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(64 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError as error:
        raise _io_error("ハッシュ対象を安全に読み取れません", path) from error


def _require_directory(path: Path) -> None:
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise OSError("通常ディレクトリではありません")
    except OSError as error:
        raise _io_error("保存先ディレクトリが安全ではありません", path) from error


def _require_replaceable_target(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    except OSError as error:
        raise _io_error("保存先を検査できません", path) from error
    if not stat.S_ISREG(mode):
        raise _io_error("保存先が安全な通常ファイルではありません", path)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _io_error(reason: str, path: Path) -> StoryPipelineError:
    return StoryPipelineError(
        reason,
        str(path),
        "ファイルの状態、アクセス権、空き容量を確認してください",
        9,
    )
