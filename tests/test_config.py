from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from story_pipeline.config import load_config
from story_pipeline.errors import StoryPipelineError
from story_pipeline.scaffold import CONFIG_TEMPLATE


def base_config() -> dict[str, object]:
    """検証を通る scaffold 設定。各テストで不正な箇所だけを書き換える。"""
    return json.loads(CONFIG_TEMPLATE)


class ConfigValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write_config(self, config: object) -> None:
        (self.root / "story-pipeline-config.jsonc").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )

    def assert_invalid(self, config: object, location: str) -> StoryPipelineError:
        self.write_config(config)
        with self.assertRaises(StoryPipelineError) as raised:
            load_config(self.root)
        self.assertEqual(raised.exception.location, location)
        self.assertEqual(raised.exception.exit_code, 4)
        return raised.exception

    def test_rejects_non_object_top_level(self) -> None:
        self.assert_invalid([], "/")

    def test_rejects_config_version_other_than_one(self) -> None:
        config = base_config()
        config["config_version"] = 2
        self.assert_invalid(config, "/config_version")

    def test_rejects_missing_top_level_key(self) -> None:
        config = base_config()
        del config["roles"]
        self.assert_invalid(config, "/roles")

    def test_provider_base_url_requires_http_absolute_url(self) -> None:
        config = base_config()
        config["providers"]["openai"]["base_url"] = "ftp://example.test/v1"
        self.assert_invalid(config, "/providers/openai/base_url")
        config = base_config()
        config["providers"]["openai"]["base_url"] = "example.test/v1"
        self.assert_invalid(config, "/providers/openai/base_url")

    def test_provider_base_url_trailing_slash_is_removed(self) -> None:
        config = base_config()
        config["providers"]["openai"]["base_url"] = "http://localhost:1234/v1/"
        self.write_config(config)
        loaded = load_config(self.root)
        self.assertEqual(loaded["providers"]["openai"]["base_url"], "http://localhost:1234/v1")

    def test_provider_api_key_env_must_be_environment_name(self) -> None:
        config = base_config()
        config["providers"]["openai"]["api_key_env"] = "1INVALID-NAME"
        self.assert_invalid(config, "/providers/openai/api_key_env")

    def test_providers_and_models_must_not_be_empty(self) -> None:
        config = base_config()
        config["providers"] = {}
        self.assert_invalid(config, "/providers")
        config = base_config()
        config["models"] = {}
        self.assert_invalid(config, "/models")

    def test_model_must_reference_existing_provider(self) -> None:
        config = base_config()
        config["models"]["default"]["provider"] = "missing"
        self.assert_invalid(config, "/models/default/provider")

    def test_model_identifier_must_not_be_empty(self) -> None:
        config = base_config()
        config["models"]["default"]["model"] = "  "
        self.assert_invalid(config, "/models/default/model")

    def test_model_max_tokens_must_be_positive(self) -> None:
        config = base_config()
        config["models"]["default"]["max_tokens"] = 0
        self.assert_invalid(config, "/models/default/max_tokens")

    def test_forbidden_parameters_are_rejected(self) -> None:
        config = base_config()
        config["models"]["default"]["parameters"] = {"api_key": "secret", "temperature": 0.7}
        error = self.assert_invalid(config, "/models/default/parameters/api_key")
        self.assertIn("設定できません", error.reason)

    def test_roles_must_cover_every_required_role(self) -> None:
        config = base_config()
        del config["roles"]["summarizer"]
        self.assert_invalid(config, "/roles/summarizer")

    def test_role_references_must_not_be_empty_or_duplicated(self) -> None:
        config = base_config()
        config["roles"]["writer"] = []
        self.assert_invalid(config, "/roles/writer")
        config = base_config()
        config["roles"]["writer"] = ["default", "default"]
        error = self.assert_invalid(config, "/roles/writer")
        self.assertIn("重複", error.reason)

    def test_limits_must_include_every_key_with_positive_values(self) -> None:
        config = base_config()
        del config["limits"]["summary_calls"]
        self.assert_invalid(config, "/limits/summary_calls")
        config = base_config()
        config["limits"]["max_changed_lines"] = 0
        self.assert_invalid(config, "/limits/max_changed_lines")

    def test_request_section_requires_positive_integers(self) -> None:
        config = base_config()
        del config["request"]["timeout_seconds"]
        self.assert_invalid(config, "/request/timeout_seconds")
        config = base_config()
        config["request"]["retry_attempts"] = 0
        self.assert_invalid(config, "/request/retry_attempts")

    def test_dotenv_relative_paths_are_resolved_from_root(self) -> None:
        config = base_config()
        config["dotenv"]["files"] = [".env", "secrets/.env"]
        self.write_config(config)
        loaded = load_config(self.root)
        self.assertEqual(
            loaded["dotenv"]["files"],
            [str(self.root / ".env"), str(self.root / "secrets/.env")],
        )


if __name__ == "__main__":
    unittest.main()
