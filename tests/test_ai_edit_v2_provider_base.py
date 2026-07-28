import unittest

from server.content_domains.ai_edit_v2_providers.base import (
    ProviderError,
    ProviderResult,
    RetryableProviderError,
    UnknownSubmissionError,
)


class ProviderBaseTests(unittest.TestCase):
    def test_provider_result_has_cost_and_request_identity(self):
        result = ProviderResult(
            provider="elevenlabs",
            capability="music",
            request_id="req-1",
            payload={"cos_key": "music/a.mp3"},
            cost_units=12,
            elapsed_ms=900,
        )

        self.assertEqual(result.request_id, "req-1")
        self.assertEqual(result.cost_units, 12)

    def test_retryable_and_unknown_submission_errors_are_provider_errors(self):
        self.assertTrue(issubclass(RetryableProviderError, ProviderError))
        self.assertTrue(issubclass(UnknownSubmissionError, ProviderError))


if __name__ == "__main__":
    unittest.main()
