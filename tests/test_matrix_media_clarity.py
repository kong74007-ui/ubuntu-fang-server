import re
import unittest

from server.matrix_template_api import REFERENCE_MEDIA_CLARITY_STYLE


class ReferenceMediaClarityTests(unittest.TestCase):
    def test_removes_dark_overlays_but_preserves_v11_white_design(self):
        self.assertIn('#root:not(.v11) .text-layer::before{background:none}', REFERENCE_MEDIA_CLARITY_STYLE)
        self.assertNotIn('#root .text-layer::before{background:none}', REFERENCE_MEDIA_CLARITY_STYLE)
        for unwanted in ('rgba(', 'brightness(', 'filter:', 'opacity:', 'font-', 'color:'):
            self.assertNotIn(unwanted, REFERENCE_MEDIA_CLARITY_STYLE)

    def test_preserves_only_existing_split_layout_bands(self):
        self.assertEqual(['01', '03', '14', '15'], re.findall(r'#root\.v(\d+)', REFERENCE_MEDIA_CLARITY_STYLE))
        self.assertIn('#070707 0 590px,transparent 590px 1330px,#070707 1330px 1920px', REFERENCE_MEDIA_CLARITY_STYLE)
        self.assertNotIn('.media-video', REFERENCE_MEDIA_CLARITY_STYLE)
        self.assertNotIn('.bottom2', REFERENCE_MEDIA_CLARITY_STYLE)


if __name__ == '__main__':
    unittest.main()
