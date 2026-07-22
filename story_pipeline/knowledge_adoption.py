"""検証済み本文 knowledge を作品文書へ決定的に反映する。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from story_pipeline.drafting import DraftCandidate, DraftKnowledgeUpdate
from story_pipeline.errors import StoryPipelineError


def build_draft_adoption_documents(
    root: Path, candidate: DraftCandidate, update: DraftKnowledgeUpdate
) -> tuple[tuple[str, str], ...]:
    """本文、canon、人物状態を同じ採用候補集合として構築する。"""
    canon = _read(root, "canon.md")
    characters = _read(root, "characters.md")
    if update.canon_facts:
        lines = []
        for item in update.canon_facts:
            people = "、".join(item.people) or "なし"
            evidence = item.evidence.replace("\n", "\\n")
            lines.append(
                f"- {item.fact}（出典: `{item.source}`、成立: {item.established_at}、"
                f"関係人物: {people}、evidence: 「{evidence}」）"
            )
        canon = _append_section_block(
            canon, "## 確定事実", f"### {candidate.path} の確定事項\n\n" + "\n".join(lines)
        )
    if update.character_states:
        lines = []
        for item in update.character_states:
            evidence = item.evidence.replace("\n", "\\n")
            lines.append(
                f"- {item.character}: {item.state}（出典: `{item.source}`、成立: "
                f"{item.established_at}、evidence: 「{evidence}」）"
            )
        characters = _append_section_block(
            characters,
            "## 人物別の目的・変化・口調・状態",
            f"### {candidate.path} の人物状態\n\n" + "\n".join(lines),
        )
    return (
        (candidate.path, candidate.content),
        ("canon.md", canon),
        ("characters.md", characters),
    )


def document_hashes(documents: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return {
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in documents
    }


def read_expected_documents(
    root: Path, output_hashes: dict[str, str]
) -> tuple[tuple[str, str], ...]:
    documents: list[tuple[str, str]] = []
    for relative, expected in output_hashes.items():
        content = _read(root, relative)
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != expected:
            raise _adoption_error("採用済み出力 hash が一致しません", relative)
        documents.append((relative, content))
    return tuple(documents)


def _append_section_block(content: str, heading: str, block: str) -> str:
    lines = content.rstrip().splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == heading) + 1
    except StopIteration:
        raise _adoption_error("knowledge 更新先の必須見出しがありません", heading)
    end = next(
        (index for index, line in enumerate(lines[start:], start) if line.strip().startswith("## ")),
        len(lines),
    )
    insertion = ["", block, ""]
    return "\n".join((*lines[:end], *insertion, *lines[end:])).rstrip() + "\n"


def _read(root: Path, relative: str) -> str:
    path = root / relative
    try:
        if not path.is_file() or path.is_symlink():
            raise OSError
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise _adoption_error("knowledge 更新対象を安全に読み取れません", relative) from error


def _adoption_error(reason: str, location: str) -> StoryPipelineError:
    return StoryPipelineError(
        reason,
        location,
        "本文 checkpoint と作品ファイルを検証してから再実行してください",
        4,
    )
