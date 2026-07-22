from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError

from story_pipeline.environment import load_environment, require_api_key
from story_pipeline.errors import StoryPipelineError
from story_pipeline.llm_client import LLMClient
from story_pipeline.llm_connection import check_initial_connections
from story_pipeline.llm_output import FieldRule, parse_json_object, validate_evaluation, validate_markdown
from story_pipeline.llm_transport import ApiFailure, ChatResponse, ChatTransport, TokenUsage
from story_pipeline.secrets import REDACTED, SecretSanitizer


class FakeResponse:
    def __init__(self, value: dict[str, object]) -> None:
        self.body = json.dumps(value).encode()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _: int) -> bytes:
        return self.body


class LLMFoundationTest(unittest.TestCase):
    def test_environment_preserves_process_values_and_sanitizes_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dotenv = Path(directory) / ".env"
            dotenv.write_text("API_KEY=dotenv-secret\nSECOND=loaded\n", encoding="utf-8")
            config = {"dotenv": {"files": [str(dotenv)]}}
            values = load_environment(config, process_environment={"API_KEY": "process-secret"})
        self.assertEqual(values, {"API_KEY": "process-secret", "SECOND": "loaded"})
        sanitizer = SecretSanitizer(["process-secret"])
        source = (
            "Authorization: Bearer process-secret "
            "https://user:pass@example.test/a?token=abc&safe=yes"
        )
        sanitized = sanitizer.sanitize(source)
        self.assertNotIn("process-secret", sanitized)
        self.assertNotIn("user:pass", sanitized)
        self.assertNotIn("abc", sanitized)
        self.assertIn(REDACTED, sanitized)

    def test_missing_api_key_does_not_include_a_value(self) -> None:
        with self.assertRaises(StoryPipelineError) as caught:
            require_api_key("openrouter", {"api_key_env": "SECRET_NAME"}, {})
        self.assertEqual(caught.exception.exit_code, 4)
        self.assertIn("SECRET_NAME", caught.exception.location)

    def test_transport_sends_key_only_in_header(self) -> None:
        captured: dict[str, object] = {}

        def open_url(request: object, *, timeout: int) -> FakeResponse:
            captured["url"] = request.full_url
            captured["body"] = request.data
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return FakeResponse(
                {"model": "remote-model", "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
            )

        result = ChatTransport(open_url=open_url).complete(
            base_url="https://example.test/v1",
            api_key="top-secret",
            model="model-a",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=10,
            parameters={"temperature": 0},
            timeout=7,
        )

        self.assertEqual(result, ChatResponse("OK", "remote-model", "stop"))
        self.assertNotIn("top-secret", captured["url"])
        self.assertNotIn(b"top-secret", captured["body"])
        self.assertEqual(captured["authorization"], "Bearer top-secret")

    def test_http_error_is_classified_and_secret_is_removed(self) -> None:
        def open_url(*_: object, **__: object) -> FakeResponse:
            body = json.dumps({"error": {"message": "bad key top-secret"}}).encode()
            raise HTTPError(
                "https://example.test", 401, "Unauthorized", {}, BytesIO(body)
            )

        with self.assertRaises(ApiFailure) as caught:
            ChatTransport(open_url=open_url).complete(
                base_url="https://example.test/v1",
                api_key="top-secret",
                model="model-a",
                messages=[],
                max_tokens=10,
                parameters={},
                timeout=7,
            )
        self.assertEqual(caught.exception.kind, "authentication")
        self.assertNotIn("top-secret", caught.exception.message)

    def test_markdown_and_json_are_not_extracted_from_explanation(self) -> None:
        self.assertEqual(
            validate_markdown("```markdown\n# Title\n```", ("# Title",)), "# Title\n"
        )
        with self.assertRaises(StoryPipelineError):
            parse_json_object(
                'Explanation\n{"decision":"accept"}',
                {"decision": FieldRule((str,))},
            )
        with self.assertRaises(StoryPipelineError):
            validate_markdown("```python\n# Title\n```", ("# Title",))

    def test_evaluation_contract_is_strict(self) -> None:
        value = {
            "decision": "accept",
            "summary": "ok",
            "issues": [],
            "scores": {"request_fit": 5},
        }
        self.assertEqual(validate_evaluation(json.dumps(value)), value)
        value["unknown"] = True
        with self.assertRaises(StoryPipelineError):
            validate_evaluation(json.dumps(value))


class SequenceTransport:
    def __init__(self, outcomes: list[ChatResponse | ApiFailure]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, object]] = []

    def complete(self, **kwargs: object) -> ChatResponse:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, ApiFailure):
            raise outcome
        return outcome


class LLMClientTest(unittest.TestCase):
    def config(self) -> dict[str, object]:
        return {
            "providers": {"p": {"base_url": "https://example.test/v1", "api_key_env": "KEY"}},
            "models": {
                "first": {"provider": "p", "model": "a", "max_tokens": 100, "parameters": {"temperature": 0}},
                "second": {"provider": "p", "model": "b", "max_tokens": 100, "parameters": {}},
            },
            "roles": {"writer": ["first", "second"]},
            "request": {"retry_attempts": 2, "timeout_seconds": 5},
            "limits": {"retry_calls_per_request": 2},
        }

    def test_retry_then_fallback_without_sleep_after_final_attempt(self) -> None:
        transport = SequenceTransport(
            [
                ApiFailure("temporary", "one"),
                ApiFailure("temporary", "two"),
                ChatResponse("done", "b", "stop"),
            ]
        )
        sleeps: list[float] = []
        client = LLMClient(
            self.config(), {"KEY": "secret"}, transport=transport, sleep=sleeps.append, random_factor=lambda: 0
        )

        result = client.complete_role("writer", [{"role": "user", "content": "write"}])

        self.assertEqual(result.model_reference, "second")
        self.assertEqual(result.attempts, 3)
        self.assertEqual(len(result.fallbacks), 1)
        self.assertEqual(sleeps, [0.5])

    def test_transport_attempts_measure_retry_wait_and_missing_usage(self) -> None:
        class FakeClock:
            def __init__(self) -> None:
                self.seconds = 0.0

            def monotonic(self) -> float:
                value = self.seconds
                self.seconds += 0.125
                return value

            def now(self) -> datetime:
                return datetime(2026, 7, 22, tzinfo=timezone.utc) + timedelta(seconds=self.seconds)

            def sleep(self, seconds: float) -> None:
                self.seconds += seconds

        clock = FakeClock()
        transport = SequenceTransport([
            ApiFailure("temporary", "retry"),
            ChatResponse("done", "a", "stop"),
        ])
        client = LLMClient(
            self.config(),
            {"KEY": "secret"},
            transport=transport,
            sleep=clock.sleep,
            random_factor=lambda: 0,
            monotonic=clock.monotonic,
            utc_now=clock.now,
        )

        result = client.complete_role("writer", [])

        self.assertIsNone(result.response.usage)
        self.assertEqual(len(result.transport_attempts), 2)
        self.assertEqual(result.transport_attempts[0].elapsed_ms, 125)
        self.assertEqual(result.transport_attempts[0].wait_ms, 500)
        self.assertEqual(result.transport_attempts[0].failure_kind, "temporary")
        self.assertEqual(result.transport_attempts[1].elapsed_ms, 125)
        self.assertEqual(result.transport_attempts[1].result, "completed")

    def test_long_transport_emits_secret_free_heartbeat(self) -> None:
        class SlowTransport:
            def complete(self, **_: object) -> ChatResponse:
                time.sleep(0.035)
                return ChatResponse("done", "a", "stop")

        events: list[dict[str, object]] = []
        client = LLMClient(
            self.config(),
            {"KEY": "top-secret"},
            transport=SlowTransport(),
            event_sink=events.append,
            heartbeat_interval_seconds=0.01,
        )

        client.complete_role("writer", [{"role": "user", "content": "top-secret"}])

        heartbeats = [item for item in events if item["kind"] == "heartbeat"]
        self.assertGreaterEqual(len(heartbeats), 3)
        self.assertNotIn("top-secret", json.dumps(events))
        self.assertEqual(tuple(events), client.drain_events())
        self.assertEqual(client.drain_events(), ())

    def test_fake_transport_exact_retry_fallback_truncation_and_usage(self) -> None:
        class Clock:
            def __init__(self) -> None:
                self.value = 0.0

            def monotonic(self) -> float:
                current = self.value
                self.value += 0.1
                return current

            def now(self) -> datetime:
                return datetime(2026, 7, 22, tzinfo=timezone.utc) + timedelta(seconds=self.value)

            def sleep(self, seconds: float) -> None:
                self.value += seconds

        clock = Clock()
        transport = SequenceTransport([
            ApiFailure("temporary", "retry"),
            ApiFailure("output_truncated", "truncated"),
            ChatResponse(
                "done", "b", "stop",
                TokenUsage(11, 7, 18, None, 2),
            ),
        ])
        client = LLMClient(
            self.config(), {"KEY": "secret"}, transport=transport,
            sleep=clock.sleep, random_factor=lambda: 0,
            monotonic=clock.monotonic, utc_now=clock.now,
        )

        result = client.complete_role("writer", [])

        self.assertEqual(result.attempts, 3)
        self.assertEqual([item.wait_ms for item in result.transport_attempts], [500, 0, 0])
        self.assertEqual(
            [item.failure_kind for item in result.transport_attempts],
            ["temporary", "output_truncated", None],
        )
        self.assertEqual(result.fallbacks[0].reason, "output_truncated")
        self.assertEqual(result.response.usage.total_tokens, 18)
        self.assertEqual(len([event for event in client.drain_events() if event["kind"] == "fallback"]), 1)

    def test_unsupported_config_parameter_is_removed_once(self) -> None:
        transport = SequenceTransport(
            [
                ApiFailure("unsupported_parameter", "temperature", 400, unsupported_parameter="temperature"),
                ChatResponse("done", "a", "stop"),
            ]
        )
        client = LLMClient(self.config(), {"KEY": "secret"}, transport=transport)

        result = client.complete_role("writer", [])

        self.assertEqual(result.attempts, 2)
        self.assertEqual(transport.calls[0]["parameters"], {"temperature": 0})
        self.assertEqual(transport.calls[1]["parameters"], {})

    def test_authentication_never_falls_back(self) -> None:
        transport = SequenceTransport([ApiFailure("authentication", "denied", 401)])
        client = LLMClient(self.config(), {"KEY": "secret"}, transport=transport)
        with self.assertRaises(ApiFailure) as caught:
            client.complete_role("writer", [])
        self.assertEqual(caught.exception.kind, "authentication")
        self.assertEqual(len(transport.calls), 1)

    def test_connection_check_deduplicates_provider(self) -> None:
        @dataclass
        class FakeClient:
            references: list[str]

            def probe_model(self, reference: str) -> int:
                self.references.append(reference)
                return 1

        fake = FakeClient([])
        results = check_initial_connections(
            self.config(), {"KEY": "secret"}, ["first", "second"], client=fake
        )
        self.assertEqual(fake.references, ["first"])
        self.assertEqual(results[0].provider, "p")


class MockHandler(BaseHTTPRequestHandler):
    attempts = 0
    bodies: list[dict[str, object]] = []

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        self.__class__.bodies.append(json.loads(self.rfile.read(length)))
        self.__class__.attempts += 1
        if self.__class__.attempts == 1:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"temporary"}}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            b'{"model":"mock","choices":[{"message":{"content":"OK"},"finish_reason":"stop"}]}'
        )

    def log_message(self, *_: object) -> None:
        return None


class MockApiIntegrationTest(unittest.TestCase):
    def test_real_http_transport_retries_mock_chat_completions(self) -> None:
        MockHandler.attempts = 0
        MockHandler.bodies = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        config = {
            "providers": {"local": {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "api_key_env": "KEY"}},
            "models": {"probe": {"provider": "local", "model": "mock", "max_tokens": 100, "parameters": {}}},
            "roles": {"writer": ["probe"]},
            "request": {"retry_attempts": 2, "timeout_seconds": 5},
            "limits": {"retry_calls_per_request": 2},
        }
        sleeps: list[float] = []
        client = LLMClient(config, {"KEY": "secret"}, sleep=sleeps.append)

        attempts = client.probe_model("probe")

        self.assertEqual(attempts, 2)
        self.assertEqual(MockHandler.attempts, 2)
        self.assertEqual(sleeps, [0.0])
        self.assertEqual(MockHandler.bodies[0]["max_tokens"], 8)


if __name__ == "__main__":
    unittest.main()
