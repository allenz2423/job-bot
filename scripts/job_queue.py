"""Durable per-tenant discovery/application queue.

Callers must provide both a tenant identifier and that tenant's private SQLite
database path on every operation. Keep one database per user: the existing
application ledger is intentionally not user-partitioned.

The queue stores exact URLs and delegates application state transitions to
``application_store``. In particular, only ``mark_confirmed_applied`` and an
explicitly confirmed review resolution can mark an application submitted.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:  # Support both ``import scripts.job_queue`` and direct script imports.
    from . import application_store, global_job_pool, source_adapters
except ImportError:
    import application_store
    import global_job_pool
    import source_adapters


QUEUE_STATES = {
    "pending", "leased", "retry_wait", "paused", "needs_review",
    "done", "failed", "skipped",
}
MAX_ATTEMPTS = 5
_REVIEW_REASON_CODES = {
    "captcha", "verification_challenge", "site_block", "missing_required_answer",
    "legal_attestation", "uncertain_submission", "possible_duplicate",
    "possible_hard_conflict", "other",
    "collection_page", "manual_application_required", "source_access_unavailable",
    "auto_apply_not_authorized",
}
MANUAL_SUBMISSION_REASONS = {
    "manual_application_required", "captcha", "verification_challenge",
    "site_block", "source_access_unavailable", "auto_apply_not_authorized",
    "missing_required_answer", "legal_attestation", "uncertain_submission",
}
_FAILURE_REASON_CODES = {
    "network_error", "page_load_timeout", "browser_crash", "model_error",
    "adapter_error", "unknown",
}
_PAUSE_REASON_CODES = {"user_pause", "schedule_pause", "worker_shutdown", ""}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat().replace("+00:00", "Z")


def _parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _require_user_id(user_id: object) -> str:
    if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 200:
        raise ValueError("user_id is required and must be at most 200 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in user_id):
        raise ValueError("user_id cannot contain control characters")
    return user_id.strip()


def _require_db_path(db_path: str | Path | None) -> str | Path:
    if db_path is None or str(db_path).strip() == "":
        raise ValueError("an explicit per-user db_path is required")
    return db_path


def _bounded_text(value: object, name: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        result = ""
    elif not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    else:
        result = value.strip()
    if required and not result:
        raise ValueError(f"{name} is required")
    if len(result) > limit:
        raise ValueError(f"{name} must be at most {limit} characters")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in result):
        raise ValueError(f"{name} contains unsupported control characters")
    return result


def _validate_worker(value: object) -> str:
    return _bounded_text(value, "worker_id", 200, required=True)


def _valid_job_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("job_url is required and must be the exact full URL")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("job_url cannot contain control characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("job_url must be an absolute http or https URL")
    return value


def _reason_code(value: object, name: str, choices: set[str]) -> str:
    result = _bounded_text(value, name, 100, required=True)
    if result not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(choices))}")
    return result


def _connect(user_id: str, db_path: str | Path) -> sqlite3.Connection:
    """Initialize the ledger and queue, and bind this DB file to one tenant."""
    tenant = _require_user_id(user_id)
    explicit_path = _require_db_path(db_path)
    connection = application_store.connect(explicit_path)
    try:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS job_queue_tenant (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                user_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS job_queue (
                queue_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                application_key TEXT REFERENCES applications(application_key) ON DELETE SET NULL,
                pool_id TEXT NOT NULL DEFAULT '',
                job_url TEXT NOT NULL,
                job_source TEXT NOT NULL DEFAULT '',
                employer_job_id TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL CHECK (state IN
                    ('pending','leased','retry_wait','paused','needs_review','done','failed','skipped')),
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                available_at TEXT NOT NULL,
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_token TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                resume_approved INTEGER NOT NULL DEFAULT 0 CHECK (resume_approved IN (0,1)),
                review_reason TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS job_queue_one_application_per_user
                ON job_queue(user_id, application_key) WHERE application_key IS NOT NULL;
            CREATE INDEX IF NOT EXISTS job_queue_claimable
                ON job_queue(user_id, state, available_at, created_at);
            CREATE INDEX IF NOT EXISTS job_queue_leases
                ON job_queue(user_id, state, lease_expires_at);
            CREATE TABLE IF NOT EXISTS job_queue_urls (
                user_id TEXT NOT NULL,
                job_url TEXT NOT NULL,
                queue_id TEXT NOT NULL REFERENCES job_queue(queue_id) ON DELETE CASCADE,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                is_current INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0,1)),
                PRIMARY KEY(user_id, job_url)
            );
            CREATE INDEX IF NOT EXISTS job_queue_urls_by_item
                ON job_queue_urls(user_id, queue_id, last_seen_at);
            CREATE UNIQUE INDEX IF NOT EXISTS job_queue_one_current_url
                ON job_queue_urls(user_id, queue_id) WHERE is_current = 1;
            CREATE TABLE IF NOT EXISTS job_queue_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                queue_id TEXT NOT NULL REFERENCES job_queue(queue_id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                event_at TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS job_queue_events_by_item
                ON job_queue_events(user_id, queue_id, event_id);
        """)
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(job_queue)")}
        if "pool_id" not in columns:
            connection.execute("ALTER TABLE job_queue ADD COLUMN pool_id TEXT NOT NULL DEFAULT ''")
        connection.execute("CREATE INDEX IF NOT EXISTS job_queue_by_pool ON job_queue(user_id,pool_id)")
        connection.execute(
            "INSERT OR IGNORE INTO job_queue_tenant(singleton,user_id) VALUES(1,?)", (tenant,)
        )
        owner = connection.execute(
            "SELECT user_id FROM job_queue_tenant WHERE singleton=1"
        ).fetchone()
        if owner is None or owner["user_id"] != tenant:
            raise ValueError("db_path is already assigned to a different user")
        # Older review handoffs recorded a safe reason in last_error but left
        # review_reason blank. Restore that reason for the UI and manual
        # submission controls without changing the durable queue state.
        missing_reasons = connection.execute(
            "SELECT queue_id,last_error FROM job_queue WHERE user_id=? AND state='needs_review' "
            "AND review_reason='' AND last_error<>''", (tenant,)
        ).fetchall()
        for prior in missing_reasons:
            reason = str(prior["last_error"])
            if reason not in _REVIEW_REASON_CODES:
                continue
            changed = connection.execute(
                "UPDATE job_queue SET review_reason=?,updated_at=? WHERE user_id=? AND queue_id=? "
                "AND state='needs_review' AND review_reason='' AND last_error=?",
                (reason, _stamp(), tenant, prior["queue_id"], reason),
            )
            if changed.rowcount:
                _event(connection, tenant, str(prior["queue_id"]), "review_reason_restored",
                       {"review_reason": reason})
        connection.commit()
        return connection
    except Exception:
        connection.close()
        raise


def _event(connection: sqlite3.Connection, user_id: str, queue_id: str,
           event_type: str, details: dict[str, object] | None = None) -> None:
    connection.execute(
        "INSERT INTO job_queue_events(user_id,queue_id,event_type,event_at,detail_json) VALUES(?,?,?,?,?)",
        (user_id, queue_id, event_type, _stamp(), json.dumps(details or {}, ensure_ascii=False)),
    )


def _ledger_status(connection: sqlite3.Connection, application_key: str | None) -> str:
    if not application_key:
        return ""
    row = connection.execute(
        "SELECT status FROM applications WHERE application_key=?", (application_key,)
    ).fetchone()
    return str(row["status"]) if row else ""


def _row(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    result: dict[str, Any] = dict(row)
    urls = connection.execute(
        "SELECT job_url,first_seen_at,last_seen_at,is_current FROM job_queue_urls "
        "WHERE user_id=? AND queue_id=? ORDER BY is_current DESC, first_seen_at",
        (row["user_id"], row["queue_id"]),
    ).fetchall()
    result["job_urls"] = [dict(url) for url in urls]
    result["resume_url"] = str(row["job_url"])
    result["application_status"] = _ledger_status(connection, row["application_key"])
    application = None
    if row["application_key"]:
        application = connection.execute(
            "SELECT match_score,next_action FROM applications WHERE application_key=?",
            (row["application_key"],),
        ).fetchone()
    result["match_score"] = application["match_score"] if application else None
    result["next_action"] = str(application["next_action"] or "") if application else ""
    return result


def _find_by_url(connection: sqlite3.Connection, user_id: str, job_url: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT q.* FROM job_queue q JOIN job_queue_urls u USING(user_id,queue_id) "
        "WHERE q.user_id=? AND u.job_url=?", (user_id, job_url)
    ).fetchone()


def _add_url(connection: sqlite3.Connection, user_id: str, queue_id: str,
             job_url: str, *, make_current: bool) -> None:
    now = _stamp()
    if make_current:
        connection.execute(
            "UPDATE job_queue_urls SET is_current=0 WHERE user_id=? AND queue_id=?",
            (user_id, queue_id),
        )
    connection.execute(
        "INSERT INTO job_queue_urls(user_id,job_url,queue_id,first_seen_at,last_seen_at,is_current) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,job_url) DO UPDATE SET "
        "last_seen_at=excluded.last_seen_at, is_current=MAX(job_queue_urls.is_current,excluded.is_current)",
        (user_id, job_url, queue_id, now, now, int(make_current)),
    )


def _ledger_match(job_url: str, job_source: str, employer_job_id: str,
                  company: str, role: str, db_path: str | Path) -> tuple[dict[str, Any], str]:
    by_url = application_store.find_existing_application(job_url=job_url, db_path=db_path)
    by_id: dict[str, Any] = {"match_type": "none", "application": None, "possible_duplicates": []}
    if employer_job_id:
        by_id = application_store.find_existing_application(
            employer_job_id=employer_job_id, job_source=job_source, db_path=db_path
        )
    url_app = by_url.get("application")
    id_app = by_id.get("application")
    reason = ""
    if url_app and id_app and url_app["application_key"] != id_app["application_key"]:
        reason = "url_and_employer_id_conflict"
    elif by_id.get("match_type") == "ambiguous_employer_job_id":
        reason = "ambiguous_employer_job_id"
    elif url_app and employer_job_id:
        known_id = str(url_app.get("employer_job_id") or "")
        known_source = str(url_app.get("job_source") or "")
        if known_id and known_id != employer_job_id:
            reason = "url_and_employer_id_conflict"
        elif known_id and job_source and known_source and known_source != job_source:
            reason = "url_and_employer_id_conflict"
    # Use the exact URL record as the anchor when identities disagree. It is the
    # strongest identity key and preserves the URL's already audited history.
    if url_app:
        return by_url, reason
    if id_app:
        return by_id, reason
    if reason:
        return by_id, reason
    return application_store.find_existing_application(
        job_url=job_url, employer_job_id=employer_job_id, job_source=job_source,
        company=company, role=role, db_path=db_path,
    ), ""


def enqueue_job(user_id: str, payload: dict[str, object], *, db_path: str | Path,
                pool_id: str = "") -> dict[str, Any]:
    """Record a discovery and enqueue it once by exact URL or confirmed job ID."""
    tenant = _require_user_id(user_id)
    _require_db_path(db_path)
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    allowed = {"job_url", "job_source", "employer_job_id", "company", "role", "location",
               "match_score", "next_action", "review_reason"}
    extra = set(payload) - allowed
    if extra:
        raise ValueError(f"Unsupported fields: {', '.join(sorted(extra))}")
    job_url = _valid_job_url(payload.get("job_url"))
    if pool_id:
        pooled = global_job_pool.get_job(pool_id)
        if pooled is None or pooled["job_url"] != job_url or pooled["listing_status"] != "candidate":
            raise ValueError("Global job does not match the exact queue URL")
    job_source = _bounded_text(payload.get("job_source", ""), "job_source", 200)
    employer_job_id = _bounded_text(payload.get("employer_job_id", ""), "employer_job_id", 300)
    company = _bounded_text(payload.get("company", ""), "company", 300)
    role = _bounded_text(payload.get("role", ""), "role", 500)
    location = _bounded_text(payload.get("location", ""), "location", 300)
    explicit_review = ""
    if payload.get("review_reason") not in (None, ""):
        explicit_review = _reason_code(payload.get("review_reason"), "review_reason", _REVIEW_REASON_CODES)
    normalized: dict[str, object] = {
        "job_url": job_url, "job_source": job_source, "employer_job_id": employer_job_id,
        "company": company, "role": role, "location": location,
    }
    if "match_score" in payload:
        normalized["match_score"] = payload["match_score"]
    if "next_action" in payload:
        normalized["next_action"] = _bounded_text(payload["next_action"], "next_action", 1000)

    match, review_reason = _ledger_match(
        job_url, job_source, employer_job_id, company, role, db_path
    )
    review_reason = review_reason or explicit_review
    application = match.get("application")
    if application is None and review_reason in {"", explicit_review}:
        result = application_store.upsert_application(normalized, db_path=db_path)
        application = result["application"]
        match = result
        if result.get("possible_duplicates") and not review_reason:
            review_reason = "listing_requires_review"
    elif application is not None:
        # Let the ledger refresh an unambiguous match so it retains every exact
        # URL and source event. Conflicting identity claims remain review-only.
        if review_reason in {"", explicit_review}:
            result = application_store.upsert_application(normalized, db_path=db_path)
            application = result["application"]
            match = result
            if result.get("possible_duplicates") and not review_reason:
                review_reason = "listing_requires_review"
    if (explicit_review and review_reason == explicit_review and application and
            application.get("review_decision") == "Approved"):
        # An advisory model signal cannot undo a human decision on the same
        # linked application during the next feed refresh.
        review_reason = ""
    application_key = str(application["application_key"]) if application else ""
    application_status = str(application.get("status") or "") if application else ""
    if application_status == "applied":
        initial_state = "done"
        if not review_reason:
            review_reason = "application_already_applied"
    elif not review_reason and application_status == "in_progress":
        initial_state = "needs_review"
        review_reason = "application_already_in_progress"
    elif not review_reason and application_status == "needs_review":
        initial_state = "needs_review"
        review_reason = "application_requires_review"
    elif not review_reason and application_status != "discovered":
        initial_state = "needs_review"
        review_reason = "application_requires_review"
    elif review_reason:
        initial_state = "needs_review"
    else:
        initial_state = "pending"

    connection = _connect(tenant, db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = _find_by_url(connection, tenant, job_url)
        if existing is None and application_key:
            existing = connection.execute(
                "SELECT * FROM job_queue WHERE user_id=? AND application_key=?",
                (tenant, application_key),
            ).fetchone()
        if existing:
            queue_id = str(existing["queue_id"])
            state = str(existing["state"])
            collision = _find_by_url(connection, tenant, job_url)
            if collision and collision["queue_id"] != queue_id:
                state = "needs_review"
                review_reason = "queue_url_conflict"
                _event(connection, tenant, str(collision["queue_id"]), "duplicate_url_conflict",
                       {"review_reason": review_reason})
                connection.execute(
                    "UPDATE job_queue SET state='needs_review',review_reason=?,updated_at=? "
                    "WHERE user_id=? AND queue_id=? AND state NOT IN ('done','skipped')",
                    (review_reason, _stamp(), tenant, collision["queue_id"]),
                )
                existing = collision
                queue_id = str(collision["queue_id"])
                state = str(existing["state"])
            else:
                if not collision:
                    should_current = state not in {"leased", "done", "skipped", "failed"}
                    _add_url(connection, tenant, queue_id, job_url, make_current=should_current)
                new_state = state
                if state in {"pending", "retry_wait"} and initial_state == "needs_review":
                    new_state = "needs_review"
                elif state in {"pending", "retry_wait"} and initial_state == "done":
                    new_state = "done"
                new_reason = review_reason if new_state == "needs_review" else str(existing["review_reason"])
                connection.execute(
                    "UPDATE job_queue SET job_url=CASE WHEN ? THEN ? ELSE job_url END, "
                    "job_source=CASE WHEN ?<>'' THEN ? ELSE job_source END, "
                    "employer_job_id=CASE WHEN ?<>'' THEN ? ELSE employer_job_id END, "
                    "company=CASE WHEN ?<>'' THEN ? ELSE company END, "
                    "role=CASE WHEN ?<>'' THEN ? ELSE role END, "
                    "location=CASE WHEN ?<>'' THEN ? ELSE location END, "
                    "pool_id=CASE WHEN ?<>'' THEN ? ELSE pool_id END, "
                    "state=?,review_reason=?,updated_at=? WHERE user_id=? AND queue_id=?",
                    (int(state not in {"leased", "done", "skipped", "failed"}), job_url,
                     job_source, job_source, employer_job_id, employer_job_id,
                     company, company, role, role, location, location,
                     pool_id, pool_id,
                     new_state, new_reason, _stamp(), tenant, queue_id),
                )
                if initial_state == "needs_review" and state != "needs_review":
                    _event(connection, tenant, queue_id, "duplicate_or_review_warning",
                           {"review_reason": review_reason})
                else:
                    _event(connection, tenant, queue_id, "listing_refreshed", {"job_url": job_url})
        else:
            queue_id = secrets.token_hex(16)
            now = _stamp()
            try:
                connection.execute(
                    "INSERT INTO job_queue(queue_id,user_id,application_key,pool_id,job_url,job_source,employer_job_id,"
                    "company,role,location,state,available_at,review_reason,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (queue_id, tenant, application_key or None, pool_id, job_url, job_source, employer_job_id,
                     company, role, location, initial_state, now, review_reason, now, now),
                )
                _add_url(connection, tenant, queue_id, job_url, make_current=True)
                _event(connection, tenant, queue_id, "enqueued", {
                    "job_url": job_url, "match_type": match.get("match_type", "none"),
                    "review_reason": review_reason,
                })
            except sqlite3.IntegrityError:
                # A concurrent discovery may have inserted this URL/application.
                existing = _find_by_url(connection, tenant, job_url)
                if existing is None and application_key:
                    existing = connection.execute(
                        "SELECT * FROM job_queue WHERE user_id=? AND application_key=?",
                        (tenant, application_key),
                    ).fetchone()
                if existing is None:
                    raise
                queue_id = str(existing["queue_id"])
                if pool_id:
                    connection.execute(
                        "UPDATE job_queue SET pool_id=? WHERE user_id=? AND queue_id=?",
                        (pool_id, tenant, queue_id),
                    )
                _event(connection, tenant, queue_id, "listing_refreshed", {"job_url": job_url})
        connection.commit()
        row = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone()
        result = _row(connection, row)
        result["created"] = existing is None
        result["match_type"] = match.get("match_type", "none")
        result["possible_duplicates"] = match.get("possible_duplicates", [])
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def enqueue_from_global_pool(user_id: str, pool_id: str, *, db_path: str | Path) -> dict[str, Any]:
    """Give one user a private application record for a public pooled listing."""
    pooled = global_job_pool.get_job(pool_id)
    if pooled is None or pooled["listing_status"] != "candidate":
        raise ValueError("Global job not found")
    if pooled["rescrape_status"] in {"pending", "claimed", "unresolved"}:
        raise ValueError("This global job is waiting for URL review")
    return enqueue_job(user_id, {
        "job_url": pooled["job_url"],
        "job_source": "",
        "company": pooled["company"],
        "role": pooled["role"],
        "location": pooled["location"],
    }, db_path=db_path, pool_id=pool_id)


def share_owned_job(user_id: str, queue_id: str, *, db_path: str | Path) -> dict[str, Any]:
    """Explicitly publish an existing user's listing facts, never its state."""
    tenant = _require_user_id(user_id)
    connection = _connect(tenant, db_path)
    try:
        row = connection.execute(
            "SELECT pool_id,job_url,job_source,company,role,location FROM job_queue WHERE user_id=? AND queue_id=?",
            (tenant, queue_id),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown queue item")
        if row["job_source"] == "Adzuna":
            raise ValueError("Adzuna results cannot be shared without redistribution rights")
        if row["pool_id"]:
            pooled = global_job_pool.get_job(str(row["pool_id"]))
            if pooled is not None:
                if pooled["listing_status"] != "candidate":
                    raise ValueError("A collection page cannot be shared as a job")
                return pooled
        job_url = str(row["job_url"])
        listing = source_adapters.JobListing(job_url, str(row["role"]),
                                             str(row["company"]), str(row["location"]), "")
    finally:
        connection.close()
    pooled = global_job_pool.upsert_listing(listing, "manual")
    if pooled["listing_status"] != "candidate":
        raise ValueError("A collection page cannot be shared as a job")
    connection = _connect(tenant, db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT pool_id,job_url FROM job_queue WHERE user_id=? AND queue_id=?",
            (tenant, queue_id),
        ).fetchone()
        if row is None or row["job_url"] != job_url:
            raise ValueError("The queue item changed before it was shared")
        if row["pool_id"] != pooled["pool_id"]:
            connection.execute(
                "UPDATE job_queue SET pool_id=?,updated_at=? WHERE user_id=? AND queue_id=?",
                (pooled["pool_id"], _stamp(), tenant, queue_id),
            )
            _event(connection, tenant, queue_id, "public_listing_shared", {"pool_id": pooled["pool_id"]})
        connection.commit()
        return pooled
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _recover_expired(connection: sqlite3.Connection, user_id: str) -> list[str]:
    now = _stamp()
    expired = connection.execute(
        "SELECT * FROM job_queue WHERE user_id=? AND state='leased' AND lease_expires_at<>'' "
        "AND lease_expires_at<=? ORDER BY lease_expires_at", (user_id, now)
    ).fetchall()
    recovered: list[str] = []
    for row in expired:
        queue_id = str(row["queue_id"])
        app_status = _ledger_status(connection, row["application_key"])
        if app_status == "applied":
            state, reason = "done", "application_already_applied"
        elif app_status == "in_progress":
            state, reason = "needs_review", "worker_lease_expired_during_application"
        elif app_status == "needs_review":
            state, reason = "needs_review", "application_requires_review"
        elif not app_status:
            state, reason = "needs_review", "application_requires_review"
        elif int(row["attempt_count"]) >= MAX_ATTEMPTS:
            state, reason = "failed", "worker_lease_expired_before_start"
        else:
            state, reason = "pending", "worker_lease_expired_before_start"
        connection.execute(
            "UPDATE job_queue SET state=?,available_at=?,lease_owner='',lease_token='',lease_expires_at='',"
            "review_reason=?,updated_at=? WHERE user_id=? AND queue_id=? AND state='leased'",
            (state, now, reason, now, user_id, queue_id),
        )
        _event(connection, user_id, queue_id, "lease_expired_recovered", {
            "state": state, "review_reason": reason, "application_status": app_status,
        })
        recovered.append(queue_id)
    # Reconcile a crash after application_store committed but before queue update.
    applied = connection.execute(
        "SELECT queue_id FROM job_queue q JOIN applications a USING(application_key) "
        "WHERE q.user_id=? AND q.state IN ('pending','retry_wait','paused','needs_review') "
        "AND a.status='applied'", (user_id,)
    ).fetchall()
    for row in applied:
        queue_id = str(row["queue_id"])
        connection.execute(
            "UPDATE job_queue SET state='done',lease_owner='',lease_token='',lease_expires_at='',"
            "review_reason='',updated_at=? WHERE user_id=? AND queue_id=?",
            (now, user_id, queue_id),
        )
        _event(connection, user_id, queue_id, "application_ledger_reconciled", {"state": "done"})
        recovered.append(queue_id)
    return recovered


def recover_interrupted(user_id: str, *, db_path: str | Path) -> list[dict[str, Any]]:
    """Recover expired leases and return exact URLs for human/site inspection."""
    tenant = _require_user_id(user_id)
    connection = _connect(tenant, db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        recovered_ids = _recover_expired(connection, tenant)
        # Include persistent ambiguous outcomes, so a restart can redisplay them.
        persistent = connection.execute(
            "SELECT queue_id FROM job_queue WHERE user_id=? AND state='needs_review' "
            "AND review_reason IN ('worker_lease_expired_during_application','application_already_in_progress') "
            "ORDER BY updated_at", (tenant,)
        ).fetchall()
        queue_ids = list(dict.fromkeys(
            recovered_ids + [str(row["queue_id"]) for row in persistent]
        ))
        rows = [connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone() for queue_id in queue_ids]
        rows = [row for row in rows if row is not None]
        connection.commit()
        return [_row(connection, row) for row in rows]
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_next(user_id: str, worker_id: str, *, db_path: str | Path,
               lease_seconds: int = 120) -> dict[str, Any] | None:
    """Atomically lease one eligible item; ambiguous submissions stay in review."""
    tenant = _require_user_id(user_id)
    worker = _validate_worker(worker_id)
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 10 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be an integer from 10 to 3600")
    connection = _connect(tenant, db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _recover_expired(connection, tenant)
        now = _now()
        stamp = _stamp(now)
        # A ledger in progress means submission may have happened. Require a
        # site check before another worker can continue it.
        in_progress = connection.execute(
            "SELECT q.queue_id FROM job_queue q JOIN applications a USING(application_key) "
            "WHERE q.user_id=? AND q.state IN ('pending','retry_wait') AND a.status='in_progress' "
            "AND q.resume_approved=0",
            (tenant,),
        ).fetchall()
        for pending in in_progress:
            queue_id = str(pending["queue_id"])
            connection.execute(
                "UPDATE job_queue SET state='needs_review',review_reason='application_already_in_progress',updated_at=? "
                "WHERE user_id=? AND queue_id=?", (stamp, tenant, queue_id)
            )
            _event(connection, tenant, queue_id, "application_outcome_check_required", {})
        # An intentionally inspected, confirmed-not-submitted job may resume
        # from the ledger's in_progress state.
        candidate = connection.execute(
            "SELECT q.* FROM job_queue q JOIN applications a USING(application_key) "
            "WHERE q.user_id=? AND q.state IN ('pending','retry_wait') AND q.available_at<=? "
            "AND a.status IN ('discovered','in_progress') "
            "AND (a.status='discovered' OR q.resume_approved=1) "
            "ORDER BY q.available_at,q.created_at LIMIT 1", (tenant, stamp)
        ).fetchone()
        if candidate is None:
            connection.commit()
            return None
        queue_id = str(candidate["queue_id"])
        token = secrets.token_urlsafe(32)
        expires = _stamp(now + timedelta(seconds=lease_seconds))
        connection.execute(
            "UPDATE job_queue SET state='leased',lease_owner=?,lease_token=?,lease_expires_at=?,"
            "attempt_count=attempt_count+1,resume_approved=0,review_reason='',updated_at=? "
            "WHERE user_id=? AND queue_id=?",
            (worker, token, expires, stamp, tenant, queue_id),
        )
        _event(connection, tenant, queue_id, "claimed", {
            "worker_id": worker, "attempt": int(candidate["attempt_count"]) + 1,
            "lease_expires_at": expires,
        })
        row = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone()
        connection.commit()
        return _row(connection, row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _assert_lease(connection: sqlite3.Connection, user_id: str, queue_id: str,
                  worker_id: str, lease_token: str, *, allow_expired: bool = False) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (user_id, queue_id)
    ).fetchone()
    if row is None:
        raise ValueError("Unknown queue item")
    if row["state"] != "leased" or row["lease_owner"] != worker_id or not secrets.compare_digest(
        str(row["lease_token"]), lease_token
    ):
        raise ValueError("The queue item is not leased by this worker")
    if not allow_expired and _parse_stamp(str(row["lease_expires_at"])) <= _now():
        raise ValueError("The worker lease has expired")
    return row


def heartbeat(user_id: str, queue_id: str, worker_id: str, lease_token: str, *,
              db_path: str | Path, lease_seconds: int = 120) -> dict[str, Any]:
    tenant = _require_user_id(user_id)
    worker = _validate_worker(worker_id)
    token = _bounded_text(lease_token, "lease_token", 300, required=True)
    if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 10 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be an integer from 10 to 3600")
    connection = _connect(tenant, db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _assert_lease(connection, tenant, queue_id, worker, token)
        expires = _stamp(_now() + timedelta(seconds=lease_seconds))
        connection.execute(
            "UPDATE job_queue SET lease_expires_at=?,updated_at=? WHERE user_id=? AND queue_id=?",
            (expires, _stamp(), tenant, queue_id),
        )
        _event(connection, tenant, queue_id, "lease_heartbeat", {"lease_expires_at": expires})
        updated = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, row["queue_id"])
        ).fetchone()
        connection.commit()
        return _row(connection, updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def release_unstarted(user_id: str, queue_id: str, worker_id: str, lease_token: str, *,
                      db_path: str | Path, reason: str = "settings_changed") -> dict[str, Any]:
    """Return an unstarted lease after a settings or daily-limit gate."""
    tenant = _require_user_id(user_id)
    worker = _validate_worker(worker_id)
    token = _bounded_text(lease_token, "lease_token", 300, required=True)
    if reason not in {"settings_changed", "worker_shutdown", "daily_limit_reached"}:
        raise ValueError("Invalid release reason")
    connection = _connect(tenant, db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _assert_lease(connection, tenant, queue_id, worker, token)
        if _ledger_status(connection, row["application_key"]) != "discovered":
            raise ValueError("Application may have started; inspect outcome before retrying")
        now = _stamp()
        connection.execute(
            "UPDATE job_queue SET state='pending',available_at=?,lease_owner='',lease_token='',"
            "lease_expires_at='',attempt_count=MAX(attempt_count-1,0),updated_at=? "
            "WHERE user_id=? AND queue_id=?", (now, now, tenant, queue_id),
        )
        _event(connection, tenant, queue_id, "unstarted_released", {"reason": reason})
        updated = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone()
        connection.commit()
        return _row(connection, updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def start_application(user_id: str, queue_id: str, worker_id: str, lease_token: str, *,
                      db_path: str | Path) -> dict[str, Any]:
    """Mark the exact ledger URL in progress before the worker opens the form."""
    tenant = _require_user_id(user_id)
    worker = _validate_worker(worker_id)
    token = _bounded_text(lease_token, "lease_token", 300, required=True)
    connection = _connect(tenant, db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _assert_lease(connection, tenant, queue_id, worker, token)
        if not row["application_key"]:
            raise ValueError("This review item has no resolved application record")
        app_key, job_url = str(row["application_key"]), str(row["job_url"])
        connection.commit()
        application = application_store.mark_in_progress(app_key, job_url, db_path=db_path)
        connection.execute("BEGIN IMMEDIATE")
        _assert_lease(connection, tenant, queue_id, worker, token)
        connection.execute(
            "UPDATE job_queue SET updated_at=? WHERE user_id=? AND queue_id=?",
            (_stamp(), tenant, queue_id),
        )
        _event(connection, tenant, queue_id, "application_started", {"job_url": job_url})
        updated = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone()
        connection.commit()
        result = _row(connection, updated)
        result["application"] = application
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_confirmed_applied(user_id: str, queue_id: str, worker_id: str, lease_token: str, *,
                           confirmed: bool, db_path: str | Path) -> dict[str, Any]:
    """Complete the queue only after calling the ledger's confirmed transition."""
    if confirmed is not True:
        raise ValueError("confirmed=true is required after the site confirms submission")
    tenant = _require_user_id(user_id)
    worker = _validate_worker(worker_id)
    token = _bounded_text(lease_token, "lease_token", 300, required=True)
    connection = _connect(tenant, db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _assert_lease(connection, tenant, queue_id, worker, token)
        if not row["application_key"]:
            raise ValueError("This queue item has no application ledger record")
        application_key, job_url = str(row["application_key"]), str(row["job_url"])
        connection.commit()
        application = application_store.mark_applied(
            application_key, job_url, confirmed=True, db_path=db_path
        )
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone()
        if current is None:
            raise ValueError("Unknown queue item")
        # If a concurrent recovery already reconciled the queue, the ledger is
        # still authoritative and this operation is safe to repeat.
        if current["state"] == "leased" and current["lease_token"] == token:
            connection.execute(
                "UPDATE job_queue SET state='done',lease_owner='',lease_token='',lease_expires_at='',"
                "review_reason='',last_error='',updated_at=? WHERE user_id=? AND queue_id=?",
                (_stamp(), tenant, queue_id),
            )
            _event(connection, tenant, queue_id, "application_confirmed", {"job_url": job_url})
        elif current["state"] != "done":
            raise RuntimeError("The ledger confirmed submission but the queue lease changed; recovery will reconcile it")
        updated = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone()
        connection.commit()
        result = _row(connection, updated)
        result["application"] = application
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _finish_lease(user_id: str, queue_id: str, worker_id: str, lease_token: str,
                  db_path: str | Path, *, state: str, reason: str = "",
                  retry_after_seconds: int = 0) -> dict[str, Any]:
    tenant = _require_user_id(user_id)
    worker = _validate_worker(worker_id)
    token = _bounded_text(lease_token, "lease_token", 300, required=True)
    connection = _connect(tenant, db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _assert_lease(connection, tenant, queue_id, worker, token)
        app_status = _ledger_status(connection, row["application_key"])
        safe_reason = _bounded_text(reason, "reason", 100)
        target, review_reason = state, safe_reason if state == "needs_review" else ""
        if target == "retry_wait" and int(row["attempt_count"]) >= MAX_ATTEMPTS:
            target = "failed"
        if app_status in {"in_progress", "applied"}:
            if app_status == "applied":
                target = "done"
            else:
                target = "needs_review"
                review_reason = (
                    safe_reason if state == "needs_review"
                    else "worker_lease_expired_during_application"
                ) or "worker_lease_expired_during_application"
        if target == "retry_wait":
            available = _stamp(_now() + timedelta(seconds=retry_after_seconds))
        else:
            available = _stamp()
        connection.execute(
            "UPDATE job_queue SET state=?,available_at=?,lease_owner='',lease_token='',lease_expires_at='',"
            "review_reason=?,last_error=?,updated_at=? WHERE user_id=? AND queue_id=?",
            (target, available, review_reason,
             safe_reason if target in {"paused", "retry_wait", "failed", "needs_review"} else "",
             _stamp(), tenant, queue_id),
        )
        _event(connection, tenant, queue_id, {
            "paused": "paused", "needs_review": "review_requested", "retry_wait": "failure_retry_scheduled",
            "failed": "failure_terminal", "done": "application_already_confirmed",
        }[target], {"review_reason": review_reason, "reason": safe_reason, "available_at": available})
        updated = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone()
        connection.commit()
        return _row(connection, updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def pause_item(user_id: str, queue_id: str, worker_id: str, lease_token: str, *,
               db_path: str | Path, reason: str = "") -> dict[str, Any]:
    reason_code = _bounded_text(reason, "reason", 100)
    if reason_code not in _PAUSE_REASON_CODES:
        raise ValueError(f"reason must be one of: {', '.join(sorted(_PAUSE_REASON_CODES))}")
    return _finish_lease(user_id, queue_id, worker_id, lease_token, db_path,
                         state="paused", reason=reason_code)


def report_invalid_url(user_id: str, queue_id: str, worker_id: str, lease_token: str, *,
                       db_path: str | Path) -> dict[str, Any]:
    """Request one public-job rescrape, pause this user's item, and release its lease.

    The application worker must discard its transient task context after this
    return and claim a different item with a fresh context. Submission outcomes
    already in progress require human review instead of this shortcut.
    """
    tenant = _require_user_id(user_id)
    worker = _validate_worker(worker_id)
    token = _bounded_text(lease_token, "lease_token", 300, required=True)
    connection = _connect(tenant, db_path)
    try:
        row = _assert_lease(connection, tenant, queue_id, worker, token)
        if _ledger_status(connection, row["application_key"]) != "discovered":
            raise ValueError("An application was started; review the submission outcome")
        pool_id, job_url = str(row["pool_id"]), str(row["job_url"])
        if not pool_id:
            raise ValueError("This private job has no public pool record; request review")
    finally:
        connection.close()

    # Global first, then private: a crash can leave a harmless extra refresh
    # request, while the private lease still recovers by its normal audit path.
    global_job_pool.request_rescrape(pool_id, job_url)
    connection = _connect(tenant, db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _assert_lease(connection, tenant, queue_id, worker, token)
        if (str(row["pool_id"]) != pool_id or str(row["job_url"]) != job_url or
                _ledger_status(connection, row["application_key"]) != "discovered"):
            raise ValueError("The queue item changed before the rescrape handoff")
        now = _stamp()
        connection.execute(
            "UPDATE job_queue SET state='paused',lease_owner='',lease_token='',lease_expires_at='',"
            "last_error='invalid_url_rescrape_requested',updated_at=? "
            "WHERE user_id=? AND queue_id=?",
            (now, tenant, queue_id),
        )
        _event(connection, tenant, queue_id, "invalid_url_rescrape_requested",
               {"pool_id": pool_id, "job_url": job_url})
        updated = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone()
        connection.commit()
        result = _row(connection, updated)
        result["worker_instruction"] = "discard_task_context_then_claim_next"
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def retry_owned_rescrape(user_id: str, queue_id: str, *, db_path: str | Path) -> dict[str, Any]:
    """Let the owning user retry a paused URL check after source access changes."""
    tenant = _require_user_id(user_id)
    connection = _connect(tenant, db_path)
    try:
        row = connection.execute(
            "SELECT pool_id,job_url,state,last_error FROM job_queue WHERE user_id=? AND queue_id=?",
            (tenant, queue_id),
        ).fetchone()
        if (row is None or row["state"] != "paused" or
                row["last_error"] != "invalid_url_rescrape_requested" or not row["pool_id"]):
            raise ValueError("No paused public URL check exists for this job")
        pool_id, job_url = str(row["pool_id"]), str(row["job_url"])
    finally:
        connection.close()
    request = global_job_pool.request_rescrape(pool_id, job_url)
    connection = _connect(tenant, db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT pool_id,job_url,state,last_error FROM job_queue WHERE user_id=? AND queue_id=?",
            (tenant, queue_id),
        ).fetchone()
        if (row is None or row["pool_id"] != pool_id or row["job_url"] != job_url or
                row["state"] != "paused" or row["last_error"] != "invalid_url_rescrape_requested"):
            raise ValueError("The queue item changed before the URL check was retried")
        _event(connection, tenant, queue_id, "invalid_url_rescrape_retried", {"pool_id": pool_id})
        connection.commit()
        return request
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def require_review(user_id: str, queue_id: str, worker_id: str, lease_token: str, *,
                   db_path: str | Path, reason: str) -> dict[str, Any]:
    # Stable reason codes avoid storing arbitrary page text or secret-bearing data.
    reason_code = _reason_code(reason, "reason", _REVIEW_REASON_CODES)
    return _finish_lease(user_id, queue_id, worker_id, lease_token, db_path,
                         state="needs_review", reason=reason_code)


def record_failure(user_id: str, queue_id: str, worker_id: str, lease_token: str, *,
                   db_path: str | Path, reason: str, retryable: bool = True,
                   retry_after_seconds: int = 300) -> dict[str, Any]:
    if not isinstance(retryable, bool):
        raise ValueError("retryable must be a boolean")
    if isinstance(retry_after_seconds, bool) or not isinstance(retry_after_seconds, int) or not 0 <= retry_after_seconds <= 86400:
        raise ValueError("retry_after_seconds must be an integer from 0 to 86400")
    reason_code = _reason_code(reason, "reason", _FAILURE_REASON_CODES)
    return _finish_lease(
        user_id, queue_id, worker_id, lease_token, db_path,
        state="retry_wait" if retryable else "failed", reason=reason_code,
        retry_after_seconds=retry_after_seconds,
    )


def resume_paused(user_id: str, queue_id: str, *, db_path: str | Path) -> dict[str, Any]:
    tenant = _require_user_id(user_id)
    preliminary = _connect(tenant, db_path)
    try:
        prior = preliminary.execute(
            "SELECT pool_id,job_url,last_error FROM job_queue WHERE user_id=? AND queue_id=?",
            (tenant, queue_id),
        ).fetchone()
    finally:
        preliminary.close()
    verified_pool_id = ""
    if prior is not None and prior["last_error"] == "invalid_url_rescrape_requested":
        verified_pool_id = str(prior["pool_id"])
        pooled = global_job_pool.get_job(verified_pool_id) if verified_pool_id else None
        if pooled is None or pooled["rescrape_status"] != "verified" or pooled["job_url"] != prior["job_url"]:
            raise ValueError("The exact job URL has not been verified by rescraping")
    connection = _connect(tenant, db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown queue item")
        if row["state"] != "paused":
            raise ValueError("Only a paused queue item can be resumed")
        if row["last_error"] == "invalid_url_rescrape_requested" and (
                verified_pool_id != row["pool_id"] or prior["job_url"] != row["job_url"]):
            raise ValueError("The queue item changed before resuming")
        app_status = _ledger_status(connection, row["application_key"])
        if app_status == "applied":
            state, reason = "done", "application_already_applied"
        elif app_status == "in_progress":
            state, reason = "needs_review", "application_already_in_progress"
        elif app_status == "needs_review" or not app_status:
            state, reason = "needs_review", "application_requires_review"
        else:
            state, reason = "pending", ""
        connection.execute(
            "UPDATE job_queue SET state=?,review_reason=?,last_error='',available_at=?,updated_at=? "
            "WHERE user_id=? AND queue_id=?",
            (state, reason, _stamp(), _stamp(), tenant, queue_id),
        )
        _event(connection, tenant, queue_id, "resumed", {"state": state, "review_reason": reason})
        updated = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone()
        connection.commit()
        return _row(connection, updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def resume_verified_pool_job(user_id: str, pool_id: str, *, db_path: str | Path) -> int:
    """Resume only paused private copies of the same exact verified URL."""
    tenant = _require_user_id(user_id)
    pooled = global_job_pool.get_job(pool_id)
    if pooled is None or pooled["rescrape_status"] != "verified":
        return 0
    connection = _connect(tenant, db_path)
    try:
        queue_ids = [str(row["queue_id"]) for row in connection.execute(
            "SELECT queue_id FROM job_queue WHERE user_id=? AND pool_id=? AND job_url=? "
            "AND state='paused' AND last_error='invalid_url_rescrape_requested'",
            (tenant, pool_id, pooled["job_url"]),
        )]
    finally:
        connection.close()
    resumed = 0
    for selected in queue_ids:
        try:
            resume_paused(tenant, selected, db_path=db_path)
            resumed += 1
        except ValueError:
            continue
    return resumed


def resume_verified_rescrapes(user_id: str, *, db_path: str | Path) -> int:
    """Reopen paused items only after their own exact URL was verified."""
    tenant = _require_user_id(user_id)
    connection = _connect(tenant, db_path)
    try:
        pool_ids = [str(row["pool_id"]) for row in connection.execute(
            "SELECT DISTINCT pool_id FROM job_queue WHERE user_id=? AND pool_id<>'' "
            "AND state='paused' AND last_error='invalid_url_rescrape_requested'",
            (tenant,),
        )]
    finally:
        connection.close()
    return sum(resume_verified_pool_job(tenant, pool_id, db_path=db_path) for pool_id in pool_ids)


def flag_collection_page(user_id: str, job_url: str, *, db_path: str | Path) -> dict[str, Any]:
    """Remove one exact collection URL from claimable work pending a skip decision."""
    tenant = _require_user_id(user_id)
    exact_url = _valid_job_url(job_url)
    connection = _connect(tenant, db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _find_by_url(connection, tenant, exact_url)
        if row is None:
            raise ValueError("Unknown queue URL")
        if row["state"] == "needs_review" and row["review_reason"] == "collection_page":
            connection.commit()
            return _row(connection, row)
        if row["state"] not in {"pending", "retry_wait"}:
            raise ValueError("Only an unstarted queue item can be flagged as a collection page")
        if _ledger_status(connection, row["application_key"]) not in {"", "discovered"}:
            raise ValueError("Application work has already begun")
        connection.execute(
            "UPDATE job_queue SET state='needs_review',review_reason='collection_page',updated_at=? "
            "WHERE user_id=? AND queue_id=?",
            (_stamp(), tenant, row["queue_id"]),
        )
        _event(connection, tenant, str(row["queue_id"]), "collection_page_flagged",
               {"job_url": exact_url})
        updated = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?",
            (tenant, row["queue_id"]),
        ).fetchone()
        connection.commit()
        return _row(connection, updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def resolve_review(user_id: str, queue_id: str, decision: str, *,
                   db_path: str | Path, confirmed: bool = False) -> dict[str, Any]:
    """Resolve a human review; submission/retry outcomes require explicit confirmation."""
    tenant = _require_user_id(user_id)
    if decision not in {"approve", "skip", "duplicate", "submitted", "not_submitted"}:
        raise ValueError("decision must be approve, skip, duplicate, submitted, or not_submitted")
    if decision in {"submitted", "not_submitted"} and confirmed is not True:
        raise ValueError("confirmed=true is required after inspecting the site")
    connection = _connect(tenant, db_path)
    try:
        row = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown queue item")
        if row["state"] != "needs_review":
            raise ValueError("Only a queue item in needs_review can be resolved")
        application_key = str(row["application_key"] or "")
        job_url = str(row["job_url"])
        if not application_key:
            if decision not in {"skip", "duplicate"}:
                raise ValueError("Resolve the conflicting job identity before approving this item")
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT state FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
            ).fetchone()
            if current is None or current["state"] != "needs_review":
                raise RuntimeError("The review state changed while it was being resolved")
            connection.execute(
                "UPDATE job_queue SET state='skipped',review_reason='',updated_at=? "
                "WHERE user_id=? AND queue_id=?", (_stamp(), tenant, queue_id)
            )
            _event(connection, tenant, queue_id, "review_resolved", {
                "decision": decision, "state": "skipped", "application_status": "",
            })
            updated = connection.execute(
                "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
            ).fetchone()
            connection.commit()
            return _row(connection, updated)
        app_status = _ledger_status(connection, application_key)

        def set_review(decision_value: str) -> None:
            tracked = application_store.find_existing_application(job_url=job_url, db_path=db_path)
            tracked_application = tracked.get("application")
            if not tracked_application or tracked_application.get("application_key") != application_key:
                raise ValueError("The queue URL no longer resolves to its linked application record")
            application_store.update_user_fields(
                application_key,
                priority=str(tracked_application.get("priority") or ""),
                user_notes=str(tracked_application.get("user_notes") or ""),
                review_decision=decision_value,
                db_path=db_path,
            )

        if decision == "submitted":
            if app_status == "discovered" and row["review_reason"] in MANUAL_SUBMISSION_REASONS:
                # A person may finish an inspected job outside the worker. The
                # explicit site confirmation is the sole authority for this path.
                application_store.mark_in_progress(application_key, job_url, db_path=db_path)
            elif app_status != "in_progress":
                raise ValueError("Only an in-progress or manual-handoff application can be confirmed submitted")
            application_store.mark_applied(application_key, job_url, confirmed=True, db_path=db_path)
            state, review_decision, resume_approved = "done", "Approved", 0
        elif decision == "not_submitted":
            if app_status != "in_progress":
                raise ValueError("not_submitted is only valid for an in-progress application")
            set_review("Approved")
            state, review_decision, resume_approved = "pending", "Approved", 1
        elif decision == "approve":
            if app_status == "in_progress":
                raise ValueError("Inspect the site and choose submitted or not_submitted for an in-progress application")
            if app_status not in {"discovered", "needs_review"}:
                raise ValueError("Only a discovered or review-needed application can be approved for processing")
            set_review("Approved")
            state, review_decision, resume_approved = "pending", "Approved", 0
        else:
            review_decision = "Skip" if decision == "skip" else "Duplicate"
            set_review(review_decision)
            state, resume_approved = "skipped", 0
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT state FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone()
        if current is None or current["state"] != "needs_review":
            raise RuntimeError("The review state changed while it was being resolved")
        reason = "" if state != "needs_review" else str(row["review_reason"])
        connection.execute(
            "UPDATE job_queue SET state=?,review_reason=?,resume_approved=?,available_at=?,updated_at=? "
            "WHERE user_id=? AND queue_id=?",
            (state, reason, resume_approved, _stamp(), _stamp(), tenant, queue_id),
        )
        _event(connection, tenant, queue_id, "review_resolved", {
            "decision": decision, "state": state, "application_status": _ledger_status(connection, application_key),
        })
        updated = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone()
        connection.commit()
        return _row(connection, updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_queue(user_id: str, *, db_path: str | Path,
               state: str | None = None) -> list[dict[str, Any]]:
    tenant = _require_user_id(user_id)
    if state is not None and state not in QUEUE_STATES:
        raise ValueError("unknown queue state")
    connection = _connect(tenant, db_path)
    try:
        if state is None:
            rows = connection.execute(
                "SELECT * FROM job_queue WHERE user_id=? ORDER BY created_at DESC", (tenant,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM job_queue WHERE user_id=? AND state=? ORDER BY created_at DESC",
                (tenant, state),
            ).fetchall()
        return [_row(connection, row) for row in rows]
    finally:
        connection.close()


def get_queue_item(user_id: str, queue_id: str, *, db_path: str | Path) -> dict[str, Any]:
    tenant = _require_user_id(user_id)
    if not isinstance(queue_id, str) or len(queue_id) != 32 or any(ch not in "0123456789abcdef" for ch in queue_id):
        raise ValueError("Unknown queue item")
    connection = _connect(tenant, db_path)
    try:
        row = connection.execute(
            "SELECT * FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown queue item")
        return _row(connection, row)
    finally:
        connection.close()


def queue_events(user_id: str, queue_id: str, *, db_path: str | Path,
                 limit: int | None = None) -> list[dict[str, Any]]:
    tenant = _require_user_id(user_id)
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 500):
        raise ValueError("event limit must be between 1 and 500")
    connection = _connect(tenant, db_path)
    try:
        if connection.execute(
            "SELECT 1 FROM job_queue WHERE user_id=? AND queue_id=?", (tenant, queue_id)
        ).fetchone() is None:
            raise ValueError("Unknown queue item")
        if limit is None:
            rows = connection.execute(
                "SELECT event_type,event_at,detail_json FROM job_queue_events "
                "WHERE user_id=? AND queue_id=? ORDER BY event_id", (tenant, queue_id)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT event_type,event_at,detail_json FROM job_queue_events "
                "WHERE user_id=? AND queue_id=? ORDER BY event_id DESC LIMIT ?",
                (tenant, queue_id, limit)
            ).fetchall()[::-1]
        return [dict(row) for row in rows]
    finally:
        connection.close()
