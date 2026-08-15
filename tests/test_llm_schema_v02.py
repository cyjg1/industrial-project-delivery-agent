import unittest

from agent import llm_provider
from agent.llm_provider import (
    ChatStreamEvent,
    LLMJsonValidationError,
    parse_llm_candidate_json,
)


class LLMJsonSchemaV02Test(unittest.TestCase):
    def test_rejects_llm_candidate_without_evidence_refs(self):
        raw = '{"items":[{"item_id":"bad","category":"链路","title":"无证据","description":"bad","status":"candidate","evidence_refs":[]}]}'

        with self.assertRaises(LLMJsonValidationError):
            parse_llm_candidate_json(raw)

    def test_rejects_non_json_llm_output(self):
        with self.assertRaises(LLMJsonValidationError):
            parse_llm_candidate_json("plain text")

    def test_stream_retries_when_connection_closes_before_first_visible_event(self):
        retry_stream = getattr(llm_provider, "_retry_stream_before_first_event", None)
        self.assertIsNotNone(retry_stream)
        attempts = 0

        def stream_factory():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError(
                    "peer closed connection without sending complete message body (incomplete chunked read)"
                )
            yield ChatStreamEvent(kind="delta", text="恢复成功")

        events = list(retry_stream(stream_factory, max_attempts=2, sleep=lambda _delay: None))

        self.assertEqual(attempts, 2)
        self.assertEqual([event.text for event in events], ["恢复成功"])

    def test_stream_does_not_replay_after_visible_output_was_emitted(self):
        retry_stream = getattr(llm_provider, "_retry_stream_before_first_event", None)
        interrupted_error = getattr(llm_provider, "LLMStreamInterruptedError", RuntimeError)
        self.assertIsNotNone(retry_stream)
        attempts = 0

        def stream_factory():
            nonlocal attempts
            attempts += 1
            yield ChatStreamEvent(kind="delta", text="已经显示")
            raise RuntimeError(
                "peer closed connection without sending complete message body (incomplete chunked read)"
            )

        with self.assertRaises(interrupted_error) as raised:
            list(retry_stream(stream_factory, max_attempts=2, sleep=lambda _delay: None))

        self.assertEqual(attempts, 1)
        self.assertTrue(raised.exception.partial)


if __name__ == "__main__":
    unittest.main()
