from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from server.content_domains import ai_edit_v2_platform_assets as platform_assets


class PlatformAssetConnectionTests(unittest.TestCase):
    def test_jobs_database_uses_short_lived_immutable_readonly_connection(self):
        path = Path("platform-jobs.db").resolve()
        connection = Mock()

        with patch.object(platform_assets.sqlite3, "connect", return_value=connection) as connect:
            result = platform_assets._connect(path.as_posix(), immutable=True)

        self.assertIs(result, connection)
        connect.assert_called_once_with(
            f"{path.as_uri()}?mode=ro&immutable=1",
            timeout=10,
            uri=True,
        )
        self.assertIs(connection.row_factory, platform_assets.sqlite3.Row)

    def test_asset_database_keeps_normal_sqlite_connection(self):
        connection = Mock()

        with patch.object(platform_assets.sqlite3, "connect", return_value=connection) as connect:
            result = platform_assets._connect("shared-assets.db")

        self.assertIs(result, connection)
        connect.assert_called_once_with("shared-assets.db", timeout=10)
        self.assertIs(connection.row_factory, platform_assets.sqlite3.Row)


if __name__ == "__main__":
    unittest.main()
