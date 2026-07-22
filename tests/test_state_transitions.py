from __future__ import annotations

import unittest

from story_pipeline.state_transitions import (
    all_chapters_complete_after,
    transition_after_chapter,
    transition_after_draft,
)
from story_pipeline.story_structure import ChapterEpisodes, StoryStructure


class StateTransitionTest(unittest.TestCase):
    def structure(self) -> StoryStructure:
        return StoryStructure((
            ChapterEpisodes(1, "chapters/0001.md", (1, 2)),
            ChapterEpisodes(2, "chapters/0002.md", (3, 4)),
        ))

    def test_draft_moves_to_next_episode_then_chapter_revision(self) -> None:
        state = {"current_chapter": 1, "completed_episodes": [], "next_chapter": 1}
        first = transition_after_draft(self.structure(), state, 1)
        self.assertEqual((first["phase"], first["next_episode"]), ("episode_planning", 2))

        state.update(first)
        second = transition_after_draft(self.structure(), state, 2)
        self.assertEqual(second["phase"], "chapter_revision")
        self.assertEqual(second["current_chapter"], 1)
        self.assertEqual(second["next_episode"], 3)

    def test_chapter_moves_to_next_chapter_then_final_revision(self) -> None:
        state = {"completed_chapters": [], "completed_episodes": [1, 2], "current_chapter": 1}
        first = transition_after_chapter(self.structure(), state, 1)
        self.assertEqual(
            (first["phase"], first["current_chapter"], first["next_episode"]),
            ("episode_planning", 2, 3),
        )
        state.update(first)
        state["completed_episodes"] = [1, 2, 3, 4]
        second = transition_after_chapter(self.structure(), state, 2)
        self.assertEqual(second["phase"], "final_revision")
        self.assertIsNone(second["current_chapter"])
        self.assertEqual((second["next_chapter"], second["next_episode"]), (3, 5))
        self.assertFalse(all_chapters_complete_after(self.structure(), (), 1))
        self.assertTrue(all_chapters_complete_after(self.structure(), (1,), 2))

    def test_9999_uses_non_wrapping_terminal_sentinel(self) -> None:
        structure = StoryStructure((ChapterEpisodes(9999, "chapters/9999.md", (9999,)),))
        draft = transition_after_draft(
            structure, {"current_chapter": 9999, "completed_episodes": []}, 9999
        )
        self.assertEqual(draft["next_episode"], 9999)
        chapter = transition_after_chapter(
            structure, {"completed_chapters": [], "completed_episodes": [9999]}, 9999
        )
        self.assertEqual((chapter["next_chapter"], chapter["next_episode"]), (9999, 9999))
