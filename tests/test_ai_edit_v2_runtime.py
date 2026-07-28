import unittest

from server.content_domains import ai_edit_v2_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def test_stable_sequence_stops_before_task_8_quality_implementation(self):
        self.assertEqual(
            runtime.STABLE_STAGE_SEQUENCE,
            (
                "normalizing",
                "transcribing",
                "aligning",
                "directing",
                "resolving_materials",
                "generating_media",
                "rendering",
                "postprocessing",
            ),
        )
        self.assertEqual(runtime.public_state("quality_check"), "quality_checking")


if __name__ == "__main__":
    unittest.main()
