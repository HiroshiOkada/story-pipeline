from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest

from story_pipeline.llm_capability import check_llm_command
from story_pipeline.llm_transport import ApiFailure
from story_pipeline.scaffold import CONFIG_TEMPLATE


class FakeClient:
    def __init__(self, structured_failure: ApiFailure | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.structured_failure = structured_failure

    def probe_model(self, reference: str) -> int:
        self.calls.append(("chat", reference))
        return 1

    def probe_structured_output(self, reference: str) -> int:
        self.calls.append(("structured", reference))
        if self.structured_failure is not None:
            raise self.structured_failure
        return 1


class LLMCapabilityCommandTest(unittest.TestCase):
    def project(self, root: Path) -> None:
        (root / "story-pipeline-config.jsonc").write_text(CONFIG_TEMPLATE, encoding="utf-8")

    def test_checks_each_referenced_model_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.project(root)
            output = io.StringIO()
            errors = io.StringIO()
            client = FakeClient()

            code = check_llm_command(
                output, errors, root=root, client=client, environment={}
            )

        self.assertEqual(code, 0)
        self.assertEqual(client.calls, [("chat", "default"), ("structured", "default")])
        self.assertIn("PASS default (openai/gpt-5.6-luna): chat completion", output.getvalue())
        self.assertIn("LLM capability check passed: 1 model(s).", output.getvalue())
        self.assertEqual(errors.getvalue(), "")

    def test_reports_structured_output_failure_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.project(root)
            output = io.StringIO()
            errors = io.StringIO()
            client = FakeClient(ApiFailure("invalid_request", "JSON Schema is unsupported", 400))

            code = check_llm_command(
                output, errors, root=root, client=client, environment={}
            )

        self.assertEqual(code, 7)
        self.assertIn("chat completion", output.getvalue())
        self.assertIn("FAIL default", errors.getvalue())
        self.assertIn("invalid_request", errors.getvalue())
        self.assertIn("API が要求を受け付けられませんでした", errors.getvalue())
        self.assertIn("対応: 設定のモデル識別子と parameters を確認してください", errors.getvalue())
        self.assertIn("LLM capability check failed: 1 model(s).", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
