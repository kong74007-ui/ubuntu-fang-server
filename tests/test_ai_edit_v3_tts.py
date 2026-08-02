from __future__ import annotations

import unittest


class TtsContractTests(unittest.TestCase):
    def test_protocol_module_imports(self) -> None:
        from server.content_domains.ai_edit_v3.providers.tts import TtsProvider

        self.assertIsNotNone(TtsProvider)


if __name__ == "__main__":
    unittest.main()
