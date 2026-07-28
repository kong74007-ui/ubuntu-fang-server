import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN = (ROOT / "docs" / "superpowers" / "plans" /
        "2026-07-28-ai-edit-dual-entry-test-deploy.md").read_text(encoding="utf-8")
FANG_LOCATIONS = (ROOT / "deploy" / "nginx-fang-locations.conf").read_text(encoding="utf-8")
DEV_SETUP = (ROOT / "deploy" / "setup-dev-server.sh").read_text(encoding="utf-8")


class AiEditDeployPlanTests(unittest.TestCase):
    def test_derives_runtime_manifest_from_deployed_sha_and_hashes(self):
        for required in (
                "DEPLOYED_SOURCE_SHA", "git diff --name-only", "source_sha256",
                "target_sha256", "PRESENT", "MISSING", "SKIP_HASH_EQUAL"):
            self.assertIn(required, PLAN)
        self.assertIn("8.134.216.162", PLAN)
        self.assertIn("禁止连接生产服务器", PLAN)

    def test_manifest_contains_auth_import_and_content_worker_closure(self):
        for mapping in (
                "server/invites.py` -> `/home/ubuntu/auth-service/invites.py",
                "server/wechat_virtual_pay.py` -> `/home/ubuntu/auth-service/wechat_virtual_pay.py",
                "server/wxpay.py` -> `/home/ubuntu/auth-service/wxpay.py",
                "server/auth_server.py` -> `/home/ubuntu/auth-service/auth_server.py",
                "server/func_names.py` -> `/home/ubuntu/content-api/func_names.py",
                "server/content_domains/vendor/gsap.min.js",
                "server/ai_edit_v2_worker.py` -> `/home/ubuntu/content-api/ai_edit_v2_worker.py"):
            self.assertIn(mapping, PLAN)
        self.assertLess(PLAN.index("server/invites.py` ->"), PLAN.index("server/auth_server.py` ->"))

    def test_invite_route_and_fresh_server_auth_dependencies_are_installed(self):
        self.assertIn("location ^~ /api/invite/", FANG_LOCATIONS)
        self.assertIn("server/invites.py", DEV_SETUP)
        self.assertIn("server/wechat_virtual_pay.py", DEV_SETUP)

    def test_users_database_backup_and_rollback_are_consistency_safe(self):
        for required in (
                ".backup '$RUN/sqlite/users.db'", "PRAGMA quick_check",
                "PRAGMA integrity_check", "schema hash", "non-PII row counts",
                "post-deploy-current-users.db", "explicit operator approval",
                "-wal", "-shm"):
            self.assertIn(required, PLAN)
        self.assertIn("禁止盲目自动覆盖 users.db", PLAN)

    def test_install_order_and_disabled_v2_are_explicit(self):
        for required in (
                "AI_EDIT_V2_ENABLED=0", "service user import smoke",
                "nginx -t", "systemd-analyze verify", "active jobs = 0",
                "assets before HTML", "config before restart"):
            self.assertIn(required, PLAN)
        self.assertIn("禁止整站 rsync", PLAN)

    def test_root_only_backup_directory_is_writable_by_sqlite_backup_process(self):
        self.assertNotIn("sudo -u ubuntu sqlite3", PLAN)
        self.assertIn("sudo sqlite3 /home/ubuntu/auth-service/users.db", PLAN)
        self.assertIn("root 身份", PLAN)

    def test_unknown_deployed_sha_uses_full_closure_and_hashes_without_git_diff(self):
        for required in (
                'if [ "$DEPLOYED_SOURCE_SHA" != "UNKNOWN" ]',
                "UNKNOWN 分支禁止执行 git diff",
                "完整 runtime dependency closure",
                "只按 source_sha256 与 target_sha256"):
            self.assertIn(required, PLAN)

    def test_runtime_web_manifest_includes_the_published_openapi(self):
        self.assertIn(
            "site/api-docs/openapi.json` -> `/var/www/html/api-docs/openapi.json", PLAN)
        self.assertNotIn("/var/www/huangquechuanmei", PLAN)
        self.assertIn("docs/api/openapi.json", PLAN)
        self.assertIn("repo-only", PLAN)

    def test_existing_v2_env_is_patched_without_replacing_server_secrets(self):
        for required in (
                "若文件 PRESENT", "保留所有未列出的键", "AI_EDIT_V2_COS_SECRET_ID",
                "AI_EDIT_V2_WEBHOOK_SECRET", "AI_EDIT_V2_REMOTION_TOKEN",
                "不得打印", "root:root 0600"):
            self.assertIn(required, PLAN)
        self.assertIn("只逐键更新上述非敏感值", PLAN)


if __name__ == "__main__":
    unittest.main()
