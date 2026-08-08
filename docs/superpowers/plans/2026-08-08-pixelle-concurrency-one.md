# Pixelle Concurrency-One Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce one global Pixelle video task with no more than 20 waiting requests.

**Architecture:** Add a deployment-owned asynchronous capacity limiter and apply a checked patch to the pinned Pixelle API so synchronous and asynchronous video routes share it. Keep the upstream checkout pinned and fail deployment if the patch no longer applies cleanly.

**Tech Stack:** Python 3.11, asyncio, FastAPI, unittest, Bash, git apply

## Global Constraints

- Global executing video tasks: exactly 1.
- Waiting video requests: at most 20.
- Overflow response: HTTP 429 with stable code `task_queue_full`.
- No direct production edits; delivery is through a pull request.
- Do not expose Pixelle port 8103 publicly.

---

### Task 1: Capacity limiter behavior

**Files:**
- Create: `deploy/pixelle-video/overrides/api/task_capacity.py`
- Create: `tests/test_pixelle_task_capacity.py`

**Interfaces:**
- Produces: `TaskCapacityLimiter(max_running: int, max_waiting: int)`
- Produces: `TaskQueueFullError`
- Produces: `async with limiter.slot()` for admission and execution

- [ ] **Step 1: Write failing tests**

Add async tests that hold the first slot, verify a second task waits, fill 20
waiting positions, verify the next admission raises `TaskQueueFullError`, and
verify cancellation releases admission capacity.

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m unittest tests.test_pixelle_task_capacity -v`

Expected: FAIL because `task_capacity.py` does not exist.

- [ ] **Step 3: Implement the limiter**

Use an `asyncio.Semaphore(1)` for execution and an `asyncio.Lock` guarded
admitted counter for the combined capacity of 21. Release the admitted counter
in `finally` so errors and cancellations cannot leak capacity.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `python -m unittest tests.test_pixelle_task_capacity -v`

Expected: all limiter tests PASS.

### Task 2: Connect the pinned Pixelle API

**Files:**
- Create: `deploy/pixelle-video/patches/0001-enforce-video-task-capacity.patch`
- Modify: `deploy/pixelle-video/install.sh`
- Modify: `tests/test_pixelle_deployment.py`
- Modify: `deploy/pixelle-video/README.md`

**Interfaces:**
- Consumes: `TaskCapacityLimiter.slot()` from Task 1.
- Produces: shared limiter usage by sync and async video generation.
- Produces: HTTP 429 response for `TaskQueueFullError`.

- [ ] **Step 1: Write failing deployment contract tests**

Assert that the installer copies the limiter, runs `git apply --check`, applies
the pinned patch, and no longer relies on replacing the unused
`max_concurrent_tasks` default.

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m unittest tests.test_pixelle_deployment -v`

Expected: FAIL because the patch and installer integration are absent.

- [ ] **Step 3: Add the checked upstream patch**

Patch the pinned task manager and video router so both sync and async execution
enter the same limiter. Map `TaskQueueFullError` to HTTP 429 with code
`task_queue_full`.

- [ ] **Step 4: Update installer and documentation**

Copy the limiter into the upstream checkout, validate the exact patch with
`git apply --check`, apply it, and document one running plus 20 waiting tasks.

- [ ] **Step 5: Run focused tests**

Run: `python -m unittest tests.test_pixelle_task_capacity tests.test_pixelle_deployment -v`

Expected: all focused tests PASS.

### Task 3: Validate and publish

**Files:**
- Verify all files changed in Tasks 1 and 2.

**Interfaces:**
- Produces: reviewable GitHub pull request against `main`.

- [ ] **Step 1: Run deployment regressions**

Run: `python -m unittest tests.test_pixelle_task_capacity tests.test_pixelle_deployment tests.test_nginx_csp tests.test_health_check -q`

- [ ] **Step 2: Run repository validation**

Run: `python scripts/ci_validate.py`

- [ ] **Step 3: Review diff and secrets**

Run: `git diff --check` and `git diff --stat origin/main...HEAD`.

- [ ] **Step 4: Commit and push**

Commit only the planned files, push `codex/pixelle-concurrency-one-20260808`,
and open a pull request against `main`.

