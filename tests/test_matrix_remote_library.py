"""Operator opt-in for existing HTTPS material-library worker connections."""
import unittest
from server import matrix_template_api as matrix
from tests import test_matrix_template_api as fixtures


class RemoteLibraryTests(unittest.TestCase):
    setUp = fixtures.MatrixTemplateApiTests.setUp
    tearDown = fixtures.MatrixTemplateApiTests.tearDown

    def create(self, url, **kwargs):
        return matrix.MatrixTemplateService(
            data_root=self.root / 'secondary', skill_root=self.skill,
            library_url=url, library_token='test-only', start_worker=False,
            **kwargs,
        )

    def test_https_remains_rejected_without_operator_opt_in(self):
        with self.assertRaises(matrix.MatrixTemplateError):
            self.create('https://library.example.invalid/materials')

    def test_operator_opt_in_accepts_https_path_without_contacting_provider(self):
        service = self.create('https://library.example.invalid/materials/', allow_remote_library=True)
        try:
            self.assertEqual('https://library.example.invalid/materials', service.library_url)
        finally:
            service.shutdown()

    def test_opt_in_does_not_allow_insecure_or_credential_bearing_endpoints(self):
        for url in ('http://library.example.invalid/materials', 'file:///tmp/library',
                    'https:///materials', 'https://user:secret@example.invalid/materials',
                    'https://example.invalid/materials?token=test', 'https://example.invalid/#fragment'):
            with self.subTest(url=url), self.assertRaises(matrix.MatrixTemplateError):
                self.create(url, allow_remote_library=True)

    def test_loopback_rules_are_unchanged_when_opted_in(self):
        for url in ('http://127.0.0.1/path', 'https://localhost', 'http://localhost?query=1'):
            with self.subTest(url=url), self.assertRaises(matrix.MatrixTemplateError):
                self.create(url, allow_remote_library=True)
        service = self.create('http://127.0.0.1:8111', allow_remote_library=True)
        service.shutdown()


if __name__ == '__main__':
    unittest.main()
