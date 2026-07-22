"""正式採用前の本文候補を入力 hash へ束縛して保存する。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from story_pipeline.drafting import (
    CanonFact,
    CharacterStateUpdate,
    DraftCandidate,
    DraftEvaluation,
    DraftEvaluationIssue,
    DraftKnowledgeUpdate,
    DraftingContext,
    EvaluatedDraftCandidate,
    check_draft_candidate,
)
from story_pipeline.errors import StoryPipelineError
from story_pipeline.persistence import atomic_write_json
from story_pipeline.validation import IssueCollector


HASH = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def checkpoint_relative_path(request_number: int) -> str:
    if not 0 <= request_number <= 9999:
        raise ValueError("request_number は0から9999の範囲である必要があります")
    return f".story-pipeline/checkpoints/{request_number:04d}/draft.json"


def create_pending_checkpoint(
    request_number: int,
    request_revision: int,
    context: DraftingContext,
    best: EvaluatedDraftCandidate,
    *,
    evaluation_model_reference: str,
    now: str | None = None,
) -> dict[str, Any]:
    """採用可能な本文と評価を正式成果物から隔離した checkpoint にする。"""
    if request_revision < 0 or not best.evaluation.adoptable:
        raise ValueError("checkpoint には非負の要求 revision と採用可能な評価が必要です")
    candidate = best.candidate
    content_hash = _digest(candidate.content)
    mechanical = check_draft_candidate(
        candidate, context.target_length, tolerance=context.length_tolerance
    )
    timestamp = now or _utc_timestamp()
    value: dict[str, Any] = {
        "schema_version": 1,
        "request_number": request_number,
        "request_revision": request_revision,
        "target_path": candidate.path,
        "input_hashes": dict(context.input_hashes),
        "candidate": {
            "content": candidate.content,
            "sha256": content_hash,
            "episode_number": candidate.episode_number,
            "generation": candidate.generation,
            "revision_count": candidate.revision_count,
            "model_reference": candidate.model_reference,
            "mechanical": {
                "character_count": mechanical.character_count,
                "target_length": mechanical.target_length,
                "issues": [
                    {
                        "severity": item.severity,
                        "code": item.code,
                        "location": item.location,
                        "message": item.message,
                    }
                    for item in mechanical.issues
                ],
            },
        },
        "evaluation": {
            "target_sha256": content_hash,
            "decision": best.evaluation.decision,
            "summary": best.evaluation.summary,
            "issues": [
                {
                    "severity": item.severity,
                    "category": item.category,
                    "location": item.location,
                    "evidence": item.evidence,
                    "instruction": item.instruction,
                }
                for item in best.evaluation.issues
            ],
            "scores": dict(best.evaluation.scores),
            "model_reference": evaluation_model_reference,
        },
        "knowledge": {"status": "pending", "sha256": None, "update": None},
        "adoption": {"status": "pending", "output_hashes": {}},
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    return validate_checkpoint_data(value, request_number)


def write_draft_checkpoint(root: Path, checkpoint: dict[str, Any]) -> str:
    number = checkpoint.get("request_number")
    if type(number) is not int:
        raise ValueError("checkpoint.request_number が不正です")
    relative = checkpoint_relative_path(number)
    validate_checkpoint_data(checkpoint, number)
    _ensure_checkpoint_directory(root, number)
    atomic_write_json(root / relative, checkpoint)
    return relative


def load_draft_checkpoint(root: Path, request_number: int) -> dict[str, Any] | None:
    relative = checkpoint_relative_path(request_number)
    path = root / relative
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise OSError("通常ファイルではありません")
        value = json.loads(path.read_text(encoding="utf-8"))
        return validate_checkpoint_data(value, request_number, relative)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise StoryPipelineError(
            "本文 checkpoint を安全に検証できません",
            relative,
            "checkpoint と Git 履歴を確認してから再実行してください",
            4,
        ) from error


def validate_draft_checkpoints(
    root: Path, runs: dict[int, dict[str, Any]], collector: IssueCollector
) -> None:
    """保存済み checkpoint の構造、run 対応、採用状態を副作用なしで検査する。"""
    directory = root / ".story-pipeline" / "checkpoints"
    if not directory.exists() and not directory.is_symlink():
        return
    try:
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            raise OSError
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError:
        collector.error(
            "CHECKPOINT_DIRECTORY_INVALID", "checkpoint ディレクトリが安全ではありません",
            ".story-pipeline/checkpoints",
        )
        return
    for entry in entries:
        if not re.fullmatch(r"[0-9]{4}", entry.name):
            collector.warning(
                "UNKNOWN_CHECKPOINT_ENTRY", "checkpoint の要求番号形式に一致しません",
                entry.relative_to(root).as_posix(),
            )
            continue
        number = int(entry.name)
        relative = checkpoint_relative_path(number)
        try:
            checkpoint = load_draft_checkpoint(root, number)
        except StoryPipelineError as error:
            collector.error("CHECKPOINT_INVALID", error.reason, relative)
            continue
        if checkpoint is None:
            collector.error("CHECKPOINT_FILE_MISSING", "draft checkpoint がありません", relative)
            continue
        if number not in runs:
            collector.error("CHECKPOINT_RUN_MISSING", "checkpoint に対応する run がありません", relative)
        adoption = checkpoint["adoption"]["status"]
        actual = inspect_checkpoint_adoption(root, checkpoint)
        if actual == "partial":
            collector.error(
                "CHECKPOINT_PARTIAL_ADOPTION", "本文、canon、人物状態が部分適用されています", relative
            )
        elif adoption == "adopted" and actual != "all":
            collector.error(
                "CHECKPOINT_ADOPTED_OUTPUT_MISMATCH", "採用済み checkpoint の出力 hash が一致しません", relative
            )
        for path, expected in checkpoint["input_hashes"].items():
            if path == "request_interpretation":
                continue
            try:
                target = root / path
                actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            except OSError:
                actual_hash = ""
            if actual_hash != expected:
                collector.warning(
                    "CHECKPOINT_STALE_INPUT", "checkpoint の入力 hash が現在のファイルと異なります", path
                )


def reusable_checkpoint(
    checkpoint: dict[str, Any],
    *,
    request_revision: int,
    target_path: str,
    input_hashes: dict[str, str],
) -> EvaluatedDraftCandidate | None:
    """入力境界が完全一致する pending/completed checkpoint だけを復元する。"""
    if (
        checkpoint["request_revision"] != request_revision
        or checkpoint["target_path"] != target_path
        or checkpoint["input_hashes"] != input_hashes
    ):
        return None
    return checkpoint_candidate(checkpoint)


def checkpoint_candidate(checkpoint: dict[str, Any]) -> EvaluatedDraftCandidate:
    candidate_value = checkpoint["candidate"]
    evaluation_value = checkpoint["evaluation"]
    candidate = DraftCandidate(
        checkpoint["target_path"],
        candidate_value["content"],
        candidate_value["episode_number"],
        candidate_value["generation"],
        candidate_value["model_reference"],
        tuple(sorted(checkpoint["input_hashes"].items())),
        candidate_value["revision_count"],
    )
    issues = tuple(DraftEvaluationIssue(**item) for item in evaluation_value["issues"])
    evaluation = DraftEvaluation(
        evaluation_value["decision"],
        evaluation_value["summary"],
        issues,
        tuple(sorted(evaluation_value["scores"].items())),
    )
    return EvaluatedDraftCandidate(candidate, evaluation)


def complete_checkpoint_knowledge(
    checkpoint: dict[str, Any], update: DraftKnowledgeUpdate, *, now: str | None = None
) -> dict[str, Any]:
    payload = {
        "canon_facts": [
            {
                "fact": item.fact,
                "evidence": item.evidence,
                "source": item.source,
                "established_at": item.established_at,
                "people": list(item.people),
            }
            for item in update.canon_facts
        ],
        "character_states": [
            {
                "character": item.character,
                "state": item.state,
                "evidence": item.evidence,
                "source": item.source,
                "established_at": item.established_at,
            }
            for item in update.character_states
        ],
    }
    updated = deepcopy(checkpoint)
    updated["knowledge"] = {
        "status": "completed",
        "sha256": _digest(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        "update": payload,
    }
    updated["updated_at"] = now or _utc_timestamp()
    return validate_checkpoint_data(updated, updated["request_number"])


def mark_checkpoint_adopted(
    checkpoint: dict[str, Any], output_hashes: dict[str, str], *, now: str | None = None
) -> dict[str, Any]:
    if checkpoint["knowledge"]["status"] != "completed" or not output_hashes:
        raise ValueError("knowledge 完了後の出力 hash が採用記録に必要です")
    updated = deepcopy(checkpoint)
    updated["adoption"] = {"status": "adopted", "output_hashes": dict(output_hashes)}
    updated["updated_at"] = now or _utc_timestamp()
    return validate_checkpoint_data(updated, updated["request_number"])


def prepare_checkpoint_adoption(
    checkpoint: dict[str, Any], output_hashes: dict[str, str], *, now: str | None = None
) -> dict[str, Any]:
    if checkpoint["knowledge"]["status"] != "completed" or not output_hashes:
        raise ValueError("knowledge 完了後の期待出力 hash が必要です")
    updated = deepcopy(checkpoint)
    updated["adoption"] = {"status": "ready", "output_hashes": dict(output_hashes)}
    updated["updated_at"] = now or _utc_timestamp()
    return validate_checkpoint_data(updated, updated["request_number"])


def inspect_checkpoint_adoption(root: Path, checkpoint: dict[str, Any]) -> str:
    """期待出力に対する現在値を none、all、partial へ分類する。"""
    expected = checkpoint["adoption"]["output_hashes"]
    if not expected:
        return "none"
    matching = 0
    for relative, digest in expected.items():
        path = root / relative
        try:
            if stat.S_ISREG(os.lstat(path).st_mode):
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                matching += actual == digest
        except OSError:
            pass
    if matching == 0:
        return "none"
    if matching == len(expected):
        return "all"
    return "partial"


def checkpoint_knowledge(checkpoint: dict[str, Any]) -> DraftKnowledgeUpdate | None:
    value = checkpoint["knowledge"]
    if value["status"] != "completed":
        return None
    payload = value["update"]
    facts = tuple(CanonFact(
        item["fact"], item["evidence"], item["source"], item["established_at"], tuple(item["people"])
    ) for item in payload["canon_facts"])
    states = tuple(CharacterStateUpdate(
        item["character"], item["state"], item["evidence"], item["source"], item["established_at"]
    ) for item in payload["character_states"])
    return DraftKnowledgeUpdate(facts, states)


def validate_checkpoint_data(
    value: Any, request_number: int, location: str = "draft checkpoint"
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} は object である必要があります")
    expected = {
        "schema_version", "request_number", "request_revision", "target_path", "input_hashes",
        "candidate", "evaluation", "knowledge", "adoption", "created_at", "updated_at",
    }
    _keys(value, expected, location)
    if value["schema_version"] != 1 or value["request_number"] != request_number:
        raise ValueError(f"{location} の schema または要求番号が不正です")
    if type(value["request_revision"]) is not int or value["request_revision"] < 0:
        raise ValueError(f"{location} の要求 revision が不正です")
    _relative(value["target_path"], f"{location}/target_path")
    _hashes(value["input_hashes"], f"{location}/input_hashes")
    _timestamp(value["created_at"], f"{location}/created_at")
    _timestamp(value["updated_at"], f"{location}/updated_at")
    candidate = _object(value["candidate"], f"{location}/candidate")
    _keys(candidate, {"content", "sha256", "episode_number", "generation", "revision_count", "model_reference", "mechanical"}, f"{location}/candidate")
    for name in ("content", "model_reference"):
        _string(candidate[name], f"{location}/candidate/{name}")
    for name in ("episode_number", "generation", "revision_count"):
        if type(candidate[name]) is not int or candidate[name] < 0:
            raise ValueError(f"{location}/candidate/{name} が不正です")
    _hash(candidate["sha256"], f"{location}/candidate/sha256")
    if candidate["sha256"] != _digest(candidate["content"]):
        raise ValueError(f"{location}/candidate/sha256 が本文と一致しません")
    mechanical = _object(candidate["mechanical"], f"{location}/candidate/mechanical")
    _keys(mechanical, {"character_count", "target_length", "issues"}, f"{location}/candidate/mechanical")
    if not isinstance(mechanical["issues"], list):
        raise ValueError(f"{location}/candidate/mechanical/issues は配列である必要があります")
    for issue in mechanical["issues"]:
        if not isinstance(issue, dict) or set(issue) != {"severity", "code", "location", "message"}:
            raise ValueError(f"{location}/candidate/mechanical/issues の要素が不正です")
    evaluation = _object(value["evaluation"], f"{location}/evaluation")
    _keys(evaluation, {"target_sha256", "decision", "summary", "issues", "scores", "model_reference"}, f"{location}/evaluation")
    if evaluation["target_sha256"] != candidate["sha256"] or evaluation["decision"] != "accept":
        raise ValueError(f"{location}/evaluation が採用本文へ束縛されていません")
    for name in ("summary", "model_reference"):
        _string(evaluation[name], f"{location}/evaluation/{name}")
    if not isinstance(evaluation["issues"], list) or not isinstance(evaluation["scores"], dict):
        raise ValueError(f"{location}/evaluation の評価構造が不正です")
    knowledge = _object(value["knowledge"], f"{location}/knowledge")
    _keys(knowledge, {"status", "sha256", "update"}, f"{location}/knowledge")
    if knowledge["status"] not in {"pending", "completed"}:
        raise ValueError(f"{location}/knowledge/status が不正です")
    if knowledge["status"] == "pending" and (knowledge["sha256"] is not None or knowledge["update"] is not None):
        raise ValueError(f"{location}/knowledge の pending 値が不正です")
    if knowledge["status"] == "completed":
        _hash(knowledge["sha256"], f"{location}/knowledge/sha256")
        if not isinstance(knowledge["update"], dict):
            raise ValueError(f"{location}/knowledge/update が不正です")
        serialized = json.dumps(knowledge["update"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if _digest(serialized) != knowledge["sha256"]:
            raise ValueError(f"{location}/knowledge/sha256 が更新候補と一致しません")
    adoption = _object(value["adoption"], f"{location}/adoption")
    _keys(adoption, {"status", "output_hashes"}, f"{location}/adoption")
    if adoption["status"] not in {"pending", "ready", "adopted"}:
        raise ValueError(f"{location}/adoption/status が不正です")
    _hashes(adoption["output_hashes"], f"{location}/adoption/output_hashes")
    if adoption["status"] in {"ready", "adopted"} and knowledge["status"] != "completed":
        raise ValueError(f"{location} は knowledge 未完了のまま採用できません")
    return value


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{location} のキーが不正です")


def _object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} は object である必要があります")
    return value


def _string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{location} は空でない文字列である必要があります")
    return value


def _hash(value: Any, location: str) -> None:
    if not isinstance(value, str) or HASH.fullmatch(value) is None:
        raise ValueError(f"{location} は SHA-256 である必要があります")


def _hashes(value: Any, location: str) -> None:
    mapping = _object(value, location)
    for path, digest in mapping.items():
        _relative(path, f"{location}/{path}") if path != "request_interpretation" else None
        _hash(digest, f"{location}/{path}")


def _relative(value: Any, location: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{location} は相対パスである必要があります")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"{location} は安全な相対パスである必要があります")


def _timestamp(value: Any, location: str) -> None:
    if not isinstance(value, str) or TIMESTAMP.fullmatch(value) is None:
        raise ValueError(f"{location} は UTC timestamp である必要があります")


def _ensure_checkpoint_directory(root: Path, request_number: int) -> None:
    base = root / ".story-pipeline"
    for directory in (base, base / "checkpoints", base / "checkpoints" / f"{request_number:04d}"):
        try:
            if not directory.exists() and not directory.is_symlink():
                directory.mkdir()
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                raise OSError("通常ディレクトリではありません")
            directory.resolve(strict=True).relative_to(root.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise StoryPipelineError(
                "本文 checkpoint ディレクトリを安全に作成できません",
                str(directory),
                "checkpoint ディレクトリと権限を確認してください",
                4,
            ) from error
