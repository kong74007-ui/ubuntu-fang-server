# -*- coding: utf-8 -*-
"""jobs 表的状态机与退点幂等 —— 三个服务共用的安全网。

content_jobs.db 的 jobs 表被三个进程共写：
    content_api  (8096)  image/copy/audio/video/tryon/xiaole_video
    leadgen_api  (8100)  collect/leads
    imggen_api   (8101)  Nano Banana 作图
而 reaper（超时回收）只在 content_api 里跑。

这意味着任何一个服务写终态时不做 CAS，都会出现：
    reaper 在第 360s 判超时 → 退点 → worker 在第 3686s 跑完 → 无条件写 done
    → 用户既拿到结果又拿回点数（线上 jobs 1170/1164/1118…共 21 条，280 点）

这段逻辑此前在三个文件里各抄了一份，连注释措辞都一样，只有最后调用的退点函数不同。
同一个资金 bug 因此在 core → imggen → leadgen 上依次踩过三次。抽到这里统一维护。

本模块只依赖标准库，不 import core —— 三个服务都能安全 import（leadgen/imggen 本来
就在 `from content_domains import cos / assets_store`）。
"""
import json
import time
import uuid
from contextlib import closing


def refund_transaction_key(job_id, username=""):
    """跨 content/auth 重试时保持稳定的任务退款键。"""
    return "job-refund:%s:%d" % (str(username or "unknown")[:64], int(job_id))


class PaidJobInsertError(Exception):
    def __init__(self, compensation, submission_ref, job_id=None, job_ids=None):
        super().__init__("paid job insert failed")
        self.compensation = compensation
        self.submission_ref = submission_ref
        self.job_id = job_id
        self.job_ids = list(job_ids or ([] if job_id is None else [job_id]))


class PaidJobDeductError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = int(status or 500)
        self.detail = str(detail or "点数扣除失败")


class PaidJobConflictError(Exception):
    pass


def public_dict(row, phase=None):
    data = {key: row[key] for key in (
        "id", "kind", "username", "cost", "status", "result", "error", "created_at", "updated_at")}
    data["refunded"] = int(row["refunded"] or 0) == 1 if "refunded" in row.keys() else False
    if data.get("result"):
        try:
            data["result"] = json.loads(data["result"])
        except Exception:
            pass
    terminal_phase = {"done": "done", "error": "failed", "failed": "failed"}.get(data["status"])
    if terminal_phase is not None or phase is not None:
        data["phase"] = terminal_phase or phase
    return data


def ensure_owner_column(jdb):
    """保证 jobs.owner 存在（#511）。三个服务启动时各调一次，谁先起谁建，与部署顺序无关。

    没有这列时，content 的 pending 重排/孤儿回收会把 imggen、leadgen 的任务当成自己的：
    重排会用 content 的处理器去跑别家的 payload，重启回收会把别家正在飞的任务判失败退点。
    历史行 owner 为 NULL —— 那时只有 content 会留 pending/被回收，故 content 侧用
    COALESCE(owner,'content') 把它们仍归自己，语义与建列前完全一致。
    """
    with closing(jdb()) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
        if "owner" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN owner TEXT")
            c.commit()


def set_terminal(jdb, job_id, status, result=None, error=None, from_states=("running",)):
    """CAS 抢终态：仅当当前状态在 from_states 内才迁移，返回是否抢到(rowcount>=1)。

    败者不写状态、不做副作用 —— 谁先抢到谁定终态，reaper 与 worker 之间无竞态。

    from_states 默认只认 running（与 reaper 竞争的正常路径）。run_job 的 except 分支要传
    ("pending","running")：若异常发生在把任务改成 running 之前(如 SQLite 锁冲突)，任务还停在
    pending，只认 running 会让 CAS 失败 → 不退点 → 预扣的点永久丢失，而 reaper 只扫 running、
    从不回收 pending，没人能救它。

    jdb: 返回 sqlite3 连接的工厂函数（各服务连的是同一个 content_jobs.db）。
    """
    now = int(time.time())
    holes = ",".join("?" * len(from_states))
    with closing(jdb()) as c:
        if status == "done":
            cur = c.execute(
                "UPDATE jobs SET status='done', result=?, updated_at=? WHERE id=? AND status IN (%s)" % holes,
                (json.dumps(result, ensure_ascii=False), now, job_id) + tuple(from_states))
        else:
            cur = c.execute(
                """UPDATE jobs SET status='error', error=?, updated_at=?,
                   refunded=CASE WHEN COALESCE(cost,0)>0 AND COALESCE(refunded,0)=0 THEN 2 ELSE refunded END
                   WHERE id=? AND status IN (%s)""" % holes,
                (str(error or "")[:300], now, job_id) + tuple(from_states))
        c.commit()
        return cur.rowcount >= 1


def claim_running(jdb, job_id):
    """CAS 认领：只有 pending 才能被本次执行接管。返回是否抢到。

    防同一个 job 被两个 worker 跑两遍（重启恢复 + 正常入队可能撞车）。
    """
    with closing(jdb()) as c:
        cur = c.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=? AND status='pending'",
                        (int(time.time()), job_id))
        c.commit()
        return cur.rowcount >= 1


def refund_once(jdb, job_id, username, cost, refund):
    """确认待退款任务：2=待确认，1=Auth 已确认，0=历史未知/未发起。

    refund(username, cost) 只有在幂等 Auth 明确确认后才返回真；未知结果保持 2。
    """
    try:
        cost = int(cost or 0)
    except (TypeError, ValueError):
        cost = 0
    if cost <= 0:
        return False
    with closing(jdb()) as c:
        row = c.execute("SELECT 1 FROM jobs WHERE id=? AND status='error' AND refunded=2",
                        (job_id,)).fetchone()
    if not row:
        return False
    try:
        refunded = bool(refund(username, cost))
    except Exception:
        refunded = False
    if refunded:
        with closing(jdb()) as c:
            cur = c.execute("UPDATE jobs SET refunded=1,updated_at=? WHERE id=? AND refunded=2",
                            (int(time.time()), job_id))
            c.commit()
            return cur.rowcount > 0 or bool(c.execute(
                "SELECT 1 FROM jobs WHERE id=? AND refunded=1", (job_id,)).fetchone())
    with closing(jdb()) as c:
        c.execute("UPDATE jobs SET updated_at=? WHERE id=? AND refunded=2",
                  (int(time.time()), job_id))
        c.commit()
    return False


def retry_failed_refunds(jdb, refund_job, limit=100):
    """轮转补扫明确处于待确认态的退款；历史 refunded=0 永远不自动处理。"""
    with closing(jdb()) as c:
        rows = c.execute(
            """SELECT id,username,cost FROM jobs
               WHERE status='error' AND refunded=2 AND COALESCE(cost,0)>0
               ORDER BY updated_at ASC,id ASC LIMIT ?""",
            (max(1, int(limit or 100)),),
        ).fetchall()
    recovered = 0
    for row in rows:
        if refund_job(row["id"], row["username"], row["cost"]):
            recovered += 1
    return recovered


def _compensate_failed_insert(jdb, refund, username, cost, kind, submission_ref, error, owner):
    if int(cost or 0) <= 0:
        return "refunded"
    fallback_key = "job-insert-refund:%s" % submission_ref
    reason = "job:%s:insert_failed submit:%s" % (kind, submission_ref)
    now = int(time.time())
    payload = json.dumps({"_submission_ref": submission_ref}, ensure_ascii=False)
    try:
        with closing(jdb()) as c:
            cur = c.execute(
                """INSERT INTO jobs(kind,username,cost,status,payload,error,created_at,updated_at,owner,refunded)
                   VALUES(?,?,?,'error',?,?,?,?,?,2)""",
                (kind, username, int(cost), payload,
                 "任务创建失败，退款待确认: %s" % str(error or "")[:180], now, now, owner),
            )
            c.commit()
            retry_job_id = cur.lastrowid
    except Exception as record_error:
        try:
            if refund(username, cost, reason, transaction_key=fallback_key) is False:
                raise RuntimeError("refund not confirmed")
            return "refunded"
        except Exception as refund_error:
            print("[points-critical] job insert/refund record both failed submit=%s user=%s cost=%s "
                  "insert=%s refund=%s record=%s" % (
                      submission_ref, username, cost, str(error)[:120],
                      str(refund_error)[:120], str(record_error)[:120]), flush=True)
            return "untracked"
    transaction_key = refund_transaction_key(retry_job_id, username)
    confirmed = refund_once(
        jdb, retry_job_id, username, cost,
        lambda u, c: refund(u, c, reason, transaction_key=transaction_key))
    return "refunded" if confirmed else "queued"


def create_paid_jobs(jdb, deduct, refund, kind, username, items, owner, reason_kind="",
                     submission_ref=None, deduct_transaction_key=None):
    """一次预扣并原子写入一个或多个任务；失败补偿只维护这一处。"""
    items = [(int(cost or 0), payload) for cost, payload in items]
    total = sum(cost for cost, _ in items)
    submission_ref = str(submission_ref or uuid.uuid4().hex).strip()[:128] or uuid.uuid4().hex
    reason = "job:%s submit:%s" % (reason_kind or kind, submission_ref)
    if deduct_transaction_key:
        points_left = deduct(
            username, total, reason, transaction_key=str(deduct_transaction_key))
    else:
        points_left = deduct(username, total, reason)
    now = int(time.time())
    try:
        with closing(jdb()) as c:
            job_ids = []
            for cost, payload in items:
                cur = c.execute(
                    "INSERT INTO jobs(kind,username,cost,payload,created_at,updated_at,owner) VALUES(?,?,?,?,?,?,?)",
                    (kind, username, cost, json.dumps(payload, ensure_ascii=False), now, now, owner),
                )
                job_ids.append(cur.lastrowid)
            c.commit()
            return job_ids, points_left
    except Exception as error:
        state = _compensate_failed_insert(
            jdb, refund, username, total, kind, submission_ref, error, owner)
        raise PaidJobInsertError(state, submission_ref) from error


def _explicit_job_matches(row, kind, username, cost, payload, submission_ref):
    if not row or row["kind"] != kind or row["username"] != username or int(row["cost"] or 0) != int(cost or 0):
        return False
    try:
        stored_payload = json.loads(row["payload"] or "{}")
    except Exception:
        return False
    stored_ref = stored_payload.pop("_submission_ref", None)
    stored_payload.pop("_submission_state", None)
    return stored_ref == submission_ref and stored_payload == payload


def explicit_job_matches(row, kind, username, cost, payload, submission_ref):
    """Return whether an explicit-id row is the winner for this submission."""
    return _explicit_job_matches(row, kind, username, cost, payload, submission_ref)


def _explicit_job_row(jdb, job_id):
    with closing(jdb()) as c:
        return c.execute(
            "SELECT id,kind,username,cost,payload FROM jobs WHERE id=?", (job_id,)).fetchone()


def _explicit_job_rows(jdb, job_ids, columns="id,kind,username,cost,payload,status,updated_at"):
    job_ids = [int(job_id) for job_id in job_ids]
    if not job_ids:
        return {}
    holes = ",".join("?" * len(job_ids))
    with closing(jdb()) as c:
        rows = c.execute(
            "SELECT %s FROM jobs WHERE id IN (%s)" % (columns, holes), tuple(job_ids)).fetchall()
    return {int(row["id"]): row for row in rows}


def _explicit_batch_matches(rows, job_ids, kind, username, items, submission_ref):
    if len(rows) != len(job_ids):
        return False
    return all(_explicit_job_matches(
        rows.get(int(job_id)), kind, username, cost, payload, submission_ref)
        for job_id, (cost, payload) in zip(job_ids, items))


def _explicit_batch_result(job_ids, points_left, created, return_created):
    result = (list(job_ids), points_left)
    return result + (bool(created),) if return_created else result


def _paid_job_result(job_id, points_left, created, return_created):
    if return_created:
        return job_id, points_left, bool(created)
    return job_id, points_left


def explicit_job_state(jdb, job_id):
    with closing(jdb()) as c:
        row = c.execute("SELECT status,payload,updated_at FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except Exception:
        payload = {}
    return row["status"], payload.get("_submission_state"), int(row["updated_at"] or 0)


def explicit_jobs_state(jdb, job_ids):
    rows = _explicit_job_rows(jdb, job_ids, columns="id,status,payload,updated_at")
    states = {}
    for job_id, row in rows.items():
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        states[job_id] = (
            row["status"], payload.get("_submission_state"), int(row["updated_at"] or 0))
    return states


def set_explicit_job_state(jdb, job_id, state, expected_states=()):
    with closing(jdb()) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT payload FROM jobs WHERE id=? AND status='pending'", (job_id,)).fetchone()
        if not row:
            c.commit()
            return False
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        current = payload.get("_submission_state")
        if expected_states and current not in set(expected_states):
            c.commit()
            return False
        payload["_submission_state"] = state
        c.execute("UPDATE jobs SET payload=?,updated_at=? WHERE id=? AND status='pending'",
                  (json.dumps(payload, ensure_ascii=False), int(time.time()), job_id))
        c.commit()
        return True


def set_explicit_jobs_state(jdb, job_ids, state, expected_states=()):
    """Atomically move every pending explicit job to one submission state."""
    job_ids = [int(job_id) for job_id in job_ids]
    if not job_ids or len(set(job_ids)) != len(job_ids):
        return False
    expected_states = set(expected_states)
    holes = ",".join("?" * len(job_ids))
    with closing(jdb()) as c:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute(
            "SELECT id,status,payload FROM jobs WHERE id IN (%s)" % holes,
            tuple(job_ids)).fetchall()
        if len(rows) != len(job_ids) or any(row["status"] != "pending" for row in rows):
            c.rollback()
            return False
        encoded = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except Exception:
                payload = {}
            if expected_states and payload.get("_submission_state") not in expected_states:
                c.rollback()
                return False
            payload["_submission_state"] = str(state)
            encoded.append((json.dumps(payload, ensure_ascii=False), int(row["id"])))
        now = int(time.time())
        for payload_json, job_id in encoded:
            c.execute(
                "UPDATE jobs SET payload=?,updated_at=? WHERE id=? AND status='pending'",
                (payload_json, now, job_id))
        c.commit()
        return True


def publish_explicit_jobs_ready(jdb, job_ids, expected_state):
    """Atomically publish accepted explicit jobs before workers can observe them.

    Unlike the initializer lease transition, publication is allowed after a
    worker has claimed a row.  Every row must still belong to the same
    initializer (or already be published) so a stale owner cannot revive a
    compensated submission.
    """
    job_ids = [int(job_id) for job_id in job_ids]
    if not job_ids or len(set(job_ids)) != len(job_ids):
        return False
    expected_state = str(expected_state or "")
    if not expected_state:
        return False
    holes = ",".join("?" * len(job_ids))
    with closing(jdb()) as c:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute(
            "SELECT id,payload FROM jobs WHERE id IN (%s)" % holes,
            tuple(job_ids)).fetchall()
        if len(rows) != len(job_ids):
            c.rollback()
            return False
        encoded = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except Exception:
                payload = {}
            if payload.get("_submission_state") not in {expected_state, "ready"}:
                c.rollback()
                return False
            payload["_submission_state"] = "ready"
            encoded.append((json.dumps(payload, ensure_ascii=False), int(row["id"])))
        now = int(time.time())
        for payload_json, job_id in encoded:
            c.execute(
                "UPDATE jobs SET payload=?,updated_at=? WHERE id=?",
                (payload_json, now, job_id))
        c.commit()
        return True


def reject_explicit_job_owner(jdb, job_id, expected_state, error):
    """Atomically prove initialization ownership and publish compensation."""
    with closing(jdb()) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT payload FROM jobs WHERE id=? AND status='pending'", (job_id,)).fetchone()
        if not row:
            c.commit()
            return False
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        if payload.get("_submission_state") != expected_state:
            c.commit()
            return False
        cur = c.execute(
            """UPDATE jobs SET status='error',error=?,updated_at=?,
                       refunded=CASE WHEN COALESCE(cost,0)>0 AND COALESCE(refunded,0)=0 THEN 2 ELSE refunded END
               WHERE id=? AND status='pending'""",
            (str(error or "")[:300], int(time.time()), job_id))
        c.commit()
        return cur.rowcount == 1


def reject_explicit_jobs_owner(jdb, job_ids, expected_state, error):
    """Atomically reject a whole batch only while the caller owns every row."""
    job_ids = [int(job_id) for job_id in job_ids]
    if not job_ids or len(set(job_ids)) != len(job_ids):
        return False
    holes = ",".join("?" * len(job_ids))
    with closing(jdb()) as c:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute(
            "SELECT id,status,payload FROM jobs WHERE id IN (%s)" % holes,
            tuple(job_ids)).fetchall()
        if len(rows) != len(job_ids) or any(row["status"] != "pending" for row in rows):
            c.rollback()
            return False
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except Exception:
                payload = {}
            if payload.get("_submission_state") != expected_state:
                c.rollback()
                return False
        now = int(time.time())
        cur = c.execute(
            """UPDATE jobs SET status='error',error=?,updated_at=?,
                       refunded=CASE WHEN COALESCE(cost,0)>0 AND COALESCE(refunded,0)=0 THEN 2 ELSE refunded END
               WHERE id IN (%s) AND status='pending'""" % holes,
            (str(error or "")[:300], now) + tuple(job_ids))
        if cur.rowcount != len(job_ids):
            c.rollback()
            return False
        c.commit()
        return True


def _compensate_explicit_insert(jdb, refund, job_id, kind, username, cost, payload,
                                submission_ref, error, owner):
    """Persist compensation at the same PK before refunding an explicit job.

    If persistence itself is unavailable, keep the original hold: replay can
    then finish the paid job without turning a refunded transaction into free
    work. A concurrent matching winner always takes precedence.
    """
    now = int(time.time())
    try:
        with closing(jdb()) as c:
            c.execute(
                "INSERT INTO jobs(id,kind,username,cost,status,payload,error,created_at,updated_at,owner,refunded) "
                "VALUES(?,?,?,?, 'error',?,?,?,?,?,2)",
                (job_id, kind, username, int(cost or 0),
                 json.dumps(payload, ensure_ascii=False),
                 "Task creation failed; refund pending: %s" % str(error or "")[:180],
                 now, now, owner))
            c.commit()
    except Exception:
        try:
            winner = _explicit_job_row(jdb, job_id)
        except Exception:
            return "untracked"
        if _explicit_job_matches(
                winner, kind, username, cost,
                {key: value for key, value in payload.items() if key != "_submission_ref"},
                submission_ref):
            return "winner"
        return "untracked"
    confirmed = refund_once(
        jdb, job_id, username, cost,
        lambda u, c: refund(
            u, c, "job:%s:insert_failed submit:%s" % (kind, submission_ref),
            transaction_key=refund_transaction_key(job_id, username)))
    return "refunded" if confirmed else "queued"


def _compensate_explicit_batch_insert(jdb, refund, job_ids, kind, username, items,
                                      stored_payloads, submission_ref, error, owner):
    """Persist all compensation rows before releasing any part of one batch hold."""
    now = int(time.time())
    try:
        with closing(jdb()) as c:
            c.execute("BEGIN IMMEDIATE")
            for job_id, (cost, _payload), stored_payload in zip(job_ids, items, stored_payloads):
                c.execute(
                    "INSERT INTO jobs(id,kind,username,cost,status,payload,error,created_at,updated_at,owner,refunded) "
                    "VALUES(?,?,?,?, 'error',?,?,?,?,?,2)",
                    (int(job_id), kind, username, int(cost or 0),
                     json.dumps(stored_payload, ensure_ascii=False),
                     "Task creation failed; refund pending: %s" % str(error or "")[:180],
                     now, now, owner))
            c.commit()
    except Exception:
        try:
            winner = _explicit_job_rows(jdb, job_ids)
        except Exception:
            return "untracked"
        if _explicit_batch_matches(
                winner, job_ids, kind, username, items, submission_ref):
            return "winner"
        return "untracked"

    confirmed = True
    for job_id, (cost, _payload) in zip(job_ids, items):
        if int(cost or 0) <= 0:
            continue
        reason = "job:%s:insert_failed submit:%s" % (kind, submission_ref)
        ok = refund_once(
            jdb, int(job_id), username, int(cost),
            lambda u, c, jid=int(job_id): refund(
                u, c, reason, transaction_key=refund_transaction_key(jid, username)))
        confirmed = bool(ok) and confirmed
    return "refunded" if confirmed else "queued"


def create_explicit_paid_jobs(jdb, deduct, refund, kind, username, items, owner, *,
                              job_ids, submission_ref, deduct_transaction_key,
                              submission_state=None, return_created=False,
                              reason_kind=""):
    """Create a deterministically identified paid batch with one durable DB winner."""
    items = [(int(cost or 0), dict(payload or {})) for cost, payload in items]
    job_ids = [int(job_id) for job_id in job_ids]
    submission_ref = str(submission_ref or "").strip()[:128]
    deduct_transaction_key = str(deduct_transaction_key or "").strip()
    if (not items or len(items) != len(job_ids) or len(set(job_ids)) != len(job_ids)
            or not submission_ref or not deduct_transaction_key):
        raise ValueError("explicit paid jobs require aligned ids and stable submission keys")

    reason_label = str(reason_kind or kind)
    existing = _explicit_job_rows(jdb, job_ids)
    if existing:
        if not _explicit_batch_matches(
                existing, job_ids, kind, username, items, submission_ref):
            raise PaidJobConflictError("explicit job_ids conflict")
        points_left = deduct(
            username, sum(cost for cost, _ in items),
            "job:%s submit:%s" % (reason_label, submission_ref),
            transaction_key=deduct_transaction_key)
        return _explicit_batch_result(job_ids, points_left, False, return_created)

    stored_payloads = []
    for _cost, canonical_payload in items:
        stored_payload = dict(canonical_payload)
        stored_payload["_submission_ref"] = submission_ref
        if submission_state:
            stored_payload["_submission_state"] = str(submission_state)
        stored_payloads.append(stored_payload)
    total = sum(cost for cost, _ in items)
    points_left = deduct(
        username, total, "job:%s submit:%s" % (reason_label, submission_ref),
        transaction_key=deduct_transaction_key)
    now = int(time.time())
    try:
        with closing(jdb()) as c:
            c.execute("BEGIN IMMEDIATE")
            holes = ",".join("?" * len(job_ids))
            rows = c.execute(
                "SELECT id,kind,username,cost,payload,status,updated_at FROM jobs WHERE id IN (%s)" % holes,
                tuple(job_ids)).fetchall()
            winner = {int(row["id"]): row for row in rows}
            if winner:
                if not _explicit_batch_matches(
                        winner, job_ids, kind, username, items, submission_ref):
                    c.rollback()
                    raise PaidJobConflictError("explicit job_ids conflict")
                c.commit()
                return _explicit_batch_result(job_ids, points_left, False, return_created)
            for job_id, (cost, _payload), stored_payload in zip(job_ids, items, stored_payloads):
                c.execute(
                    "INSERT INTO jobs(id,kind,username,cost,payload,created_at,updated_at,owner) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (job_id, kind, username, cost,
                     json.dumps(stored_payload, ensure_ascii=False), now, now, owner))
            c.commit()
        return _explicit_batch_result(job_ids, points_left, True, return_created)
    except PaidJobConflictError:
        raise
    except Exception as error:
        try:
            winner = _explicit_job_rows(jdb, job_ids)
        except Exception:
            winner = {}
        if _explicit_batch_matches(winner, job_ids, kind, username, items, submission_ref):
            return _explicit_batch_result(job_ids, points_left, False, return_created)
        state = _compensate_explicit_batch_insert(
            jdb, refund, job_ids, kind, username, items, stored_payloads,
            submission_ref, error, owner)
        if state == "winner":
            return _explicit_batch_result(job_ids, points_left, False, return_created)
        raise PaidJobInsertError(
            state, submission_ref, job_ids=job_ids) from error


def create_paid_job(jdb, deduct, refund, kind, username, cost, payload, owner,
                    submission_ref=None, deduct_transaction_key=None, job_id=None,
                    return_created=False, submission_state=None):
    if job_id is not None:
        job_id = int(job_id)
        submission_ref = str(submission_ref or "").strip()[:128]
        deduct_transaction_key = str(deduct_transaction_key or "").strip()
        if not submission_ref or not deduct_transaction_key:
            raise ValueError("explicit job_id requires stable submission and deduct keys")
        canonical_payload = dict(payload or {})
        existing = _explicit_job_row(jdb, job_id)
        if existing:
            if not _explicit_job_matches(
                    existing, kind, username, cost, canonical_payload, submission_ref):
                raise PaidJobConflictError("explicit job_id conflict")
            points_left = deduct(
                username, int(cost or 0), "job:%s submit:%s" % (kind, submission_ref),
                transaction_key=deduct_transaction_key)
            return _paid_job_result(job_id, points_left, False, return_created)

        stored_payload = dict(canonical_payload)
        stored_payload["_submission_ref"] = submission_ref
        if submission_state:
            stored_payload["_submission_state"] = str(submission_state)
        points_left = deduct(
            username, int(cost or 0), "job:%s submit:%s" % (kind, submission_ref),
            transaction_key=deduct_transaction_key)
        now = int(time.time())
        try:
            with closing(jdb()) as c:
                c.execute(
                    "INSERT INTO jobs(id,kind,username,cost,payload,created_at,updated_at,owner) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (job_id, kind, username, int(cost or 0),
                     json.dumps(stored_payload, ensure_ascii=False), now, now, owner))
                c.commit()
            return _paid_job_result(job_id, points_left, True, return_created)
        except Exception as error:
            try:
                winner = _explicit_job_row(jdb, job_id)
            except Exception:
                winner = None
            if _explicit_job_matches(
                    winner, kind, username, cost, canonical_payload, submission_ref):
                return _paid_job_result(job_id, points_left, False, return_created)
            state = _compensate_explicit_insert(
                jdb, refund, job_id, kind, username, int(cost or 0), stored_payload,
                submission_ref, error, owner)
            if state == "winner":
                return _paid_job_result(job_id, points_left, False, return_created)
            raise PaidJobInsertError(state, submission_ref, job_id=job_id) from error

    job_ids, points_left = create_paid_jobs(
        jdb, deduct, refund, kind, username, [(cost, payload)], owner,
        submission_ref=submission_ref,
        deduct_transaction_key=deduct_transaction_key)
    return _paid_job_result(job_ids[0], points_left, True, return_created)
