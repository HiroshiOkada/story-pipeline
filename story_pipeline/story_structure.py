"""章計画から検証済みの章・話対応表を構築する。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import stat

from story_pipeline.errors import StoryPipelineError


CHAPTER_FILE = re.compile(r"^([0-9]{4})\.md$")
EPISODE_TOKEN = re.compile(
    r"(?<![0-9])([0-9]{4})(?:\s*[-〜～–—]\s*([0-9]{4}))?(?![0-9])"
)
EPISODE_SECTION = re.compile(r"(?ms)^## 収録話\s*$\n(.*?)(?=^## |\Z)")


@dataclass(frozen=True, slots=True)
class ChapterEpisodes:
    number: int
    path: str
    episodes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class StoryStructure:
    chapters: tuple[ChapterEpisodes, ...]

    @property
    def chapter_numbers(self) -> tuple[int, ...]:
        return tuple(item.number for item in self.chapters)

    @property
    def episode_numbers(self) -> tuple[int, ...]:
        return tuple(episode for item in self.chapters for episode in item.episodes)

    def chapter_for_episode(self, episode_number: int) -> ChapterEpisodes:
        matches = [item for item in self.chapters if episode_number in item.episodes]
        if len(matches) != 1:
            raise _structure_error(
                "対象話を含む章を一意に決定できません",
                f"episodes/{episode_number:04d}.md",
            )
        return matches[0]

    def chapter(self, chapter_number: int) -> ChapterEpisodes:
        matches = [item for item in self.chapters if item.number == chapter_number]
        if len(matches) != 1:
            raise _structure_error(
                "対象章を一意に決定できません",
                f"chapters/{chapter_number:04d}.md",
            )
        return matches[0]


def load_story_structure(root: Path) -> StoryStructure:
    """全章計画を安全に読み、連続した章・話対応表を返す。"""
    directory = root / "chapters"
    try:
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            raise OSError
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as error:
        raise _structure_error("章計画ディレクトリを安全に読み取れません", "chapters") from error
    chapters: list[ChapterEpisodes] = []
    for entry in entries:
        match = CHAPTER_FILE.fullmatch(entry.name)
        if match is None:
            continue
        relative = f"chapters/{entry.name}"
        try:
            if not stat.S_ISREG(os.lstat(entry).st_mode):
                raise OSError
            content = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise _structure_error("章計画を安全に読み取れません", relative) from error
        number = int(match.group(1))
        if number == 0:
            raise _structure_error("章番号は0001から開始する必要があります", relative)
        chapters.append(ChapterEpisodes(number, relative, parse_chapter_episodes(content, relative)))
    if not chapters:
        raise _structure_error("章計画がありません", "chapters")
    chapter_numbers = [item.number for item in chapters]
    _require_sequence(chapter_numbers, 1, "章番号に重複、欠落、逆順があります", chapters[-1].path)
    all_episodes: list[int] = []
    for chapter in chapters:
        if all_episodes and chapter.episodes[0] != all_episodes[-1] + 1:
            raise _structure_error(
                "章間の収録話に重複または欠落があります",
                f"{chapter.path} ## 収録話",
            )
        all_episodes.extend(chapter.episodes)
    _require_sequence(
        all_episodes, 1, "作品全体の収録話に重複、欠落、逆順があります",
        f"{chapters[-1].path} ## 収録話",
    )
    return StoryStructure(tuple(chapters))


def parse_chapter_episodes(content: str, path: str) -> tuple[int, ...]:
    """章の収録話表記を、順序を保持した話番号列へ展開する。"""
    match = EPISODE_SECTION.search(content)
    location = f"{path} ## 収録話"
    if match is None:
        raise _structure_error("章計画に ## 収録話 がありません", location)
    episodes: list[int] = []
    for token in EPISODE_TOKEN.finditer(match.group(1)):
        start = int(token.group(1))
        end = int(token.group(2)) if token.group(2) is not None else start
        if start == 0 or end == 0 or end < start:
            raise _structure_error("収録話の番号または範囲が不正です", location)
        episodes.extend(range(start, end + 1))
    if not episodes:
        raise _structure_error("収録話に4桁の話番号または範囲が必要です", location)
    _require_sequence(episodes, episodes[0], "章内の収録話に重複、欠落、逆順があります", location)
    return tuple(episodes)


def _require_sequence(
    numbers: list[int], expected_start: int, reason: str, location: str
) -> None:
    if numbers != list(range(expected_start, expected_start + len(numbers))):
        raise _structure_error(reason, location)


def _structure_error(reason: str, location: str) -> StoryPipelineError:
    return StoryPipelineError(
        reason,
        location,
        "章計画の ## 収録話 と番号の連続性を修正してから再実行してください",
        4,
    )
