from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MaterialLibraryDeploymentTests(unittest.TestCase):
    def test_usage_state_is_writable_without_unlocking_approved_assets(self):
        unit = (
            ROOT / "deploy/systemd/huangque-material-library.service"
        ).read_text(encoding="utf-8")

        self.assertIn("StateDirectory=huangque-material-library", unit)
        self.assertIn("StateDirectoryMode=0750", unit)
        self.assertIn(
            "Environment=MATERIAL_LIBRARY_USAGE_PATH="
            "/var/lib/huangque-material-library/usage.json",
            unit,
        )
        self.assertIn(
            "ReadWritePaths=/var/lib/huangque-material-library", unit,
        )
        self.assertIn(
            "ReadOnlyPaths=/home/ubuntu/material-libraries/huangque-media",
            unit,
        )
        self.assertNotIn(
            "ReadWritePaths=/home/ubuntu/material-libraries/huangque-media",
            unit,
        )
        installer = (
            ROOT / "deploy/material-library/install.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('d.get("usage_state_ready") is True', installer)


if __name__ == "__main__":
    unittest.main()
