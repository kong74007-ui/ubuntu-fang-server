import hashlib
import json
import re
import time
from contextlib import closing

_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_PREPARATION_KEY = "_paid_submission_preparation"
_ADMISSION_KEY = "_paid_submission_admission"
ADMISSION_LEASE_SECONDS = 60
MAX_PREPARATION_BYTES = 32 * 1024 * 1024
MAX_PENDING_PREPARATIONS_PER_USER = 5
MAX_PENDING_PREPARATION_BYTES_PER_USER = 64 * 1024 * 1024
MAX_PENDING_PREPARATION_BYTES_GLOBAL = 512 * 1024 * 1024


class PreparationTooLarge(ValueError):
    pass


class PreparationCapacityError(RuntimeError):
    pass

def ensure_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS submission_idempotency(
        username TEXT NOT NULL, endpoint TEXT NOT NULL, idem_key TEXT NOT NULL,
        request_hash TEXT NOT NULL, response_json TEXT, created_at INTEGER, updated_at INTEGER,
        PRIMARY KEY(username, endpoint, idem_key))""")

def clean_key(raw):
    key = str(raw or "").strip()
    if key and not _KEY_RE.fullmatch(key):
        raise ValueError("Idempotency-Key 需为 8-128 位字母、数字或 . _ : -")
    return key


def paid_submission_identity(username, endpoint, key, count=1):
    """Derive Auth and SQLite arbitration identities that survive process loss."""
    count = int(count or 0)
    if count < 1 or count > 15:
        raise ValueError("paid submission count must be between 1 and 15")
    seed = "%s\0%s\0%s" % (str(username), str(endpoint), str(key))
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    # Twelve hex digits shifted four bits plus an index remains inside the
    # JavaScript exact-integer range and does not advance SQLite AUTOINCREMENT.
    base = int(digest[:12], 16) << 4
    job_ids = [-(base + index + 1) for index in range(count)]
    return {
        "job_ids": job_ids,
        "submission_ref": "paid-submit:" + digest,
        "deduct_transaction_key": "paid-deduct:" + digest,
        "batch_id": "batch-" + digest[:32],
    }

def _request_hash(body):
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decode_response(raw):
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _preparation_from_response(raw):
    value = _decode_response(raw)
    if isinstance(value, dict) and isinstance(value.get(_PREPARATION_KEY), dict):
        return value[_PREPARATION_KEY]
    return None


def _admission_from_response(raw):
    value = _decode_response(raw)
    if isinstance(value, dict) and isinstance(value.get(_ADMISSION_KEY), dict):
        return value[_ADMISSION_KEY]
    return None


def _is_internal_processing(raw):
    return (_preparation_from_response(raw) is not None
            or _admission_from_response(raw) is not None)

def begin(db_factory, username, endpoint, key, body):
    if not key:
        return "disabled", None
    digest, now = _request_hash(body), int(time.time())
    with closing(db_factory()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        ensure_table(connection)
        inserted = connection.execute(
            "INSERT OR IGNORE INTO submission_idempotency(username,endpoint,idem_key,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (username, endpoint, key, digest, now, now)).rowcount
        row = connection.execute(
            "SELECT request_hash,response_json FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, key)).fetchone()
        connection.commit()
        if inserted:
            return "new", None
        if row["request_hash"] != digest:
            return "conflict", None
        if not row["response_json"] or _is_internal_processing(row["response_json"]):
            return "processing", None
        return "replay", json.loads(row["response_json"])


def claim_admission(db_factory, username, endpoint, key, body, owner,
                    lease_seconds=ADMISSION_LEASE_SECONDS):
    """CAS a short pre-Auth validation/admission lease for one request key."""
    if not key or not owner:
        return False
    digest = _request_hash(body)
    now = int(time.time())
    encoded = json.dumps({_ADMISSION_KEY: {
        "owner": str(owner), "expires_at": now + max(1, int(lease_seconds or 1)),
    }}, ensure_ascii=False)
    with closing(db_factory()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        ensure_table(connection)
        row = connection.execute(
            "SELECT request_hash,response_json FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, key)).fetchone()
        if not row or row["request_hash"] != digest:
            connection.rollback()
            return False
        current = _admission_from_response(row["response_json"])
        if row["response_json"] and not current:
            connection.commit()
            return False
        if current and current.get("owner") != str(owner) and int(current.get("expires_at") or 0) > now:
            connection.commit()
            return False
        connection.execute(
            """UPDATE submission_idempotency SET response_json=?,updated_at=?
               WHERE username=? AND endpoint=? AND idem_key=?""",
            (encoded, now, username, endpoint, key))
        connection.commit()
    return True


def load_preparation(db_factory, username, endpoint, key):
    if not key:
        return None
    with closing(db_factory()) as connection:
        ensure_table(connection)
        row = connection.execute(
            "SELECT response_json FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, key)).fetchone()
    return _preparation_from_response(row["response_json"] if row else None)


def prepare(db_factory, username, endpoint, key, body, preparation, owner=None):
    """Persist the immutable, admitted request snapshot before contacting Auth."""
    if not key or not isinstance(preparation, dict):
        return None
    digest = _request_hash(body)
    encoded = json.dumps({_PREPARATION_KEY: preparation}, ensure_ascii=False)
    encoded_bytes = len(encoded.encode("utf-8"))
    if encoded_bytes > MAX_PREPARATION_BYTES:
        raise PreparationTooLarge("paid submission recovery snapshot is too large")
    with closing(db_factory()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        ensure_table(connection)
        row = connection.execute(
            "SELECT request_hash,response_json FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, key)).fetchone()
        if not row or row["request_hash"] != digest:
            connection.rollback()
            return None
        existing = _preparation_from_response(row["response_json"])
        admission = _admission_from_response(row["response_json"])
        if existing is None:
            if owner is not None:
                # A validator may outlive its admission lease.  Never let that
                # stale owner prepare a row that a newer owner deleted and a
                # later begin() recreated as bare processing state.
                if not admission or admission.get("owner") != str(owner):
                    connection.commit()
                    return None
            elif row["response_json"]:
                connection.commit()
                return None
        if existing is None:
            prefix = '{"%s"%%' % _PREPARATION_KEY
            user_usage = connection.execute(
                """SELECT COUNT(*) AS n,COALESCE(SUM(LENGTH(CAST(response_json AS BLOB))),0) AS bytes
                   FROM submission_idempotency
                   WHERE username=? AND response_json LIKE ?""",
                (username, prefix)).fetchone()
            global_usage = connection.execute(
                """SELECT COALESCE(SUM(LENGTH(CAST(response_json AS BLOB))),0) AS bytes
                   FROM submission_idempotency WHERE response_json LIKE ?""",
                (prefix,)).fetchone()
            if (int(user_usage["n"] or 0) >= MAX_PENDING_PREPARATIONS_PER_USER
                    or int(user_usage["bytes"] or 0) + encoded_bytes > MAX_PENDING_PREPARATION_BYTES_PER_USER
                    or int(global_usage["bytes"] or 0) + encoded_bytes > MAX_PENDING_PREPARATION_BYTES_GLOBAL):
                connection.rollback()
                raise PreparationCapacityError("too many unresolved paid submission snapshots")
            connection.execute(
                "UPDATE submission_idempotency SET response_json=?,updated_at=? WHERE username=? AND endpoint=? AND idem_key=?",
                (encoded, int(time.time()), username, endpoint, key))
        winner = connection.execute(
            "SELECT response_json FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, key)).fetchone()
        connection.commit()
    return _preparation_from_response(winner["response_json"] if winner else None)

def complete(db_factory, username, endpoint, key, response):
    if not key:
        return response
    encoded = json.dumps(response, ensure_ascii=False)
    with closing(db_factory()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        ensure_table(connection)
        row = connection.execute(
            "SELECT response_json FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, key)).fetchone()
        if row and (not row["response_json"] or _is_internal_processing(row["response_json"])):
            connection.execute(
                """UPDATE submission_idempotency SET response_json=?,updated_at=?
                   WHERE username=? AND endpoint=? AND idem_key=?""",
                (encoded, int(time.time()), username, endpoint, key))
            row = connection.execute(
                "SELECT response_json FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
                (username, endpoint, key)).fetchone()
        connection.commit()
    return json.loads(row[0]) if row and row[0] else response

def abort(db_factory, username, endpoint, key):
    if key:
        with closing(db_factory()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            ensure_table(connection)
            row = connection.execute(
                "SELECT response_json FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
                (username, endpoint, key)).fetchone()
            if row and (not row["response_json"] or _is_internal_processing(row["response_json"])):
                connection.execute(
                    "DELETE FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
                    (username, endpoint, key))
            connection.commit()


def abort_unprepared(db_factory, username, endpoint, key, owner=None):
    """Delete only a pre-Auth row whose admission snapshot is still absent."""
    if not key:
        return False
    with closing(db_factory()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        ensure_table(connection)
        row = connection.execute(
            "SELECT response_json FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, key)).fetchone()
        admission = _admission_from_response(row["response_json"] if row else None)
        if owner is None:
            deletable = bool(row and not row["response_json"])
        else:
            # Ownership is meaningful only while the exact admission marker
            # survives.  A stale owner must not delete a newly recreated NULL
            # row belonging to a later request generation.
            deletable = bool(
                admission and admission.get("owner") == str(owner))
        deleted = 0
        if deletable:
            deleted = connection.execute(
                "DELETE FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
                (username, endpoint, key)).rowcount
        connection.commit()
    return deleted == 1
