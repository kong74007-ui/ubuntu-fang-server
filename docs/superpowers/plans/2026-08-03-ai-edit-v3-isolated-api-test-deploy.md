# AI Edit V3 Isolated API Test Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the V3 HTTP API and V3 Worker as the same isolated Unix user so both can safely share the strict V3 SQLite store on the test server.

**Architecture:** Add a V3-only HTTP entrypoint on `127.0.0.1:8113`; it reuses the existing authenticated V3 handler without starting the generic content job pool. The API and Worker run as `huangque-ai-edit-v3`, share a private V3 database, bind the live V2 database read-only into private runtime directories, and publish completed assets through the existing group-shared asset database. Nginx routes only `/api/v3/edit/` to the isolated API; all existing V1/V2/content routes stay on port 8096.

**Tech Stack:** Python 3.10 `ThreadingHTTPServer`, systemd sandboxing and bind mounts, Nginx, SQLite WAL, existing V3 contracts and tests.

## Global Constraints

- Test environment only; do not deploy to production.
- Keep V2 routes, databases, Worker, content service, and user-visible behavior unchanged.
- Keep `AI_EDIT_V3_WORKER_CONCURRENCY=1` on the 3.4 GB test host.
- V3 API and Worker must run as `huangque-ai-edit-v3`; do not weaken native SQLite identity checks.
- V2 database access from V3 must be the same inode exposed read-only below a service-owned `0700` runtime directory.
- The V3 job database parent must be owned by `huangque-ai-edit-v3` and mode `0700`.
- V3 API must not start generic content workers or reclaim generic content jobs.

---

### Task 1: V3-only HTTP entrypoint

**Files:**
- Create: `server/ai_edit_v3_api.py`
- Create: `tests/test_ai_edit_v3_api_entrypoint.py`

**Interfaces:**
- Consumes: `server.content_domains.core.H`, whose `/api/v3/edit/` dispatch already authenticates and calls the V3 service.
- Produces: `main() -> int`, binding `127.0.0.1` to `AI_EDIT_V3_API_PORT` (default `8113`) without calling `core.init_db`, `core.start_job_workers`, or `core.reclaim_orphaned_running`.

- [ ] **Step 1: Write the failing entrypoint test**

```python
def test_v3_entrypoint_binds_private_port_without_starting_generic_workers():
    module = load_entrypoint_with_fake_server()
    assert module.PORT == 8113
    assert fake_server.address == ("127.0.0.1", 8113)
    assert generic_worker_calls == []
```

- [ ] **Step 2: Run the test and verify it fails because `server.ai_edit_v3_api` does not exist**

Run: `python -m unittest tests.test_ai_edit_v3_api_entrypoint -v`

- [ ] **Step 3: Implement the minimal dedicated server**

```python
PORT = int(os.environ.get("AI_EDIT_V3_API_PORT", "8113"))

def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), core.H)
    server.serve_forever()
    return 0
```

- [ ] **Step 4: Run the entrypoint test and verify it passes**

Run: `python -m unittest tests.test_ai_edit_v3_api_entrypoint -v`

### Task 2: Isolated services and routing

**Files:**
- Create: `deploy/systemd/huangque-ai-edit-v3-api.service`
- Create: `deploy/ai-edit-v3-api.env.example`
- Modify: `deploy/systemd/huangque-ai-edit-v3.service`
- Modify: `deploy/systemd/huangque-content.service.d/ai-edit-v3.conf`
- Modify: `deploy/tmpfiles.d/huangque-ai-edit-v3.conf`
- Modify: `deploy/huangque-secrets.env.example`
- Modify: `deploy/nginx-fang-locations.conf`
- Modify: `tests/test_ai_edit_v3_render_sandbox.py`
- Modify: `tests/test_ai_edit_v3_feature.py`

**Interfaces:**
- Produces: API unit on port `8113`, private V3 store `/var/lib/huangque-ai-edit-v3-private/ai_edit_v3.db`, V3 Worker V2 bind path `/run/huangque-ai-edit-v3/ai_edit_v2.db`, and API V2 bind path `/run/huangque-ai-edit-v3-api/ai_edit_v2.db`.
- Preserves: shared asset database `/var/lib/huangque-ai-edit-v3/shared-assets.db` for content, V2, and V3 publishing.

- [ ] **Step 1: Write failing static deployment assertions**

```python
self.assertIn("User=huangque-ai-edit-v3", api_unit)
self.assertIn("AI_EDIT_V3_API_PORT=8113", api_role_env)
self.assertIn("d /var/lib/huangque-ai-edit-v3-private 0700", tmpfiles)
self.assertIn("proxy_pass http://127.0.0.1:8113", nginx_v3_block)
self.assertNotIn("EnvironmentFile=/etc/huangque/ai-edit-v3.env", content_dropin)
```

- [ ] **Step 2: Run the two static modules and verify the new assertions fail**

Run: `python -m unittest tests.test_ai_edit_v3_feature tests.test_ai_edit_v3_render_sandbox -v`

- [ ] **Step 3: Add the API unit, role environment, private state directory, and port-8113 route**

The API unit must load `content.env`, `auth.env`, `ai-edit-v3.env`, and then `ai-edit-v3-api.env`; use `RuntimeDirectory=huangque-ai-edit-v3-api`, `RuntimeDirectoryMode=0700`, and a read-only V2 bind mount. Remove V3 environment loading from the generic content-service drop-in while retaining its shared asset DB and group access.

- [ ] **Step 4: Run static tests and all V3 API/feature/render tests**

Run: `python -m unittest tests.test_ai_edit_v3_api_entrypoint tests.test_ai_edit_v3_feature tests.test_ai_edit_v3_render_sandbox -v`

### Task 3: Test-server migration and end-to-end proof

**Files:**
- Deploy exact merged files from the final commit.
- Install: `/etc/systemd/system/huangque-ai-edit-v3-api.service`
- Install: `/etc/huangque/ai-edit-v3-api.env`
- Migrate: `/var/lib/huangque-ai-edit-v3/ai_edit_v3.db` to `/var/lib/huangque-ai-edit-v3-private/ai_edit_v3.db` while all V3 writers are stopped.

**Interfaces:**
- Produces: public authenticated V3 API, active API/Worker units, playable and downloadable V3 result asset.

- [ ] **Step 1: Stop V3 services, take SQLite-consistent backups, migrate the V3 database, and run `PRAGMA quick_check`**
- [ ] **Step 2: Install final systemd/Nginx/static files, run `systemd-analyze verify`, `visudo -cf`, and `nginx -t`**
- [ ] **Step 3: Start API and Worker, verify both remain `active`, and confirm their V2 bind-mount inode matches the host V2 database**
- [ ] **Step 4: Verify authenticated capabilities report `accepts_new_jobs=true`**
- [ ] **Step 5: Create one real task from a platform digital-IP asset and monitor every stage to `completed`**
- [ ] **Step 6: Verify result MP4, COS delivery, asset-library publication, browser playback, and download URL**
