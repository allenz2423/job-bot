"""Shared public listing catalog and durable, single-job rescrape requests.

No candidate profile, search phrase, application state, user ID, or credential
belongs here. Application history and leases remain in each user's database.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

try:
    from . import source_adapters
except ImportError:
    import source_adapters


ROOT = Path(__file__).resolve().parents[1]
POOL_DB = Path(os.environ.get("APP_STATE_DIR", ROOT / "state")) / "global_jobs.sqlite3"
SOURCE_KINDS = {"searxng", "adzuna", "feed", "job_page", "manual"}
RESCRAPE_ERRORS = {"", "source_not_permitted", "robots_disallowed", "robots_unavailable",
                   "robots_too_large",
                   "network_error", "http_error", "not_html", "page_too_large",
                   "posting_not_found", "unsafe_page_url", "unsafe_redirect", "unknown"}
PRIVATE_QUERY_KEY = re.compile(
    r"token|auth|session|email|invite|candidate|secret|signature|account|jwt|"
    r"userid|user_id|(?:^|[_-])(?:sig|code|key|user|state)(?:$|[_-])", re.I)


def _stamp(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(ch) < 32 for ch in value):
        raise ValueError("Invalid public listing field")
    return value.strip()


def _pool_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != 32 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError("Invalid global job ID")
    return value


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else POOL_DB
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    os.chmod(path, 0o600)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS global_jobs (
            pool_id TEXT PRIMARY KEY,
            job_url TEXT NOT NULL UNIQUE,
            company TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            employer_job_id TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL,
            listing_status TEXT NOT NULL DEFAULT 'candidate',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_verified_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS rescrape_requests (
            pool_id TEXT PRIMARY KEY REFERENCES global_jobs(pool_id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK (status IN ('pending','claimed','verified','unresolved')),
            reason TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 1,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            requested_at TEXT NOT NULL,
            available_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            lease_token TEXT NOT NULL DEFAULT '',
            lease_expires_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS rescrape_claimable
            ON rescrape_requests(status,available_at,lease_expires_at);
        CREATE TABLE IF NOT EXISTS global_job_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            pool_id TEXT NOT NULL REFERENCES global_jobs(pool_id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            event_at TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT ''
        );
    """)
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(global_jobs)")}
    if "listing_status" not in columns:
        connection.execute("ALTER TABLE global_jobs ADD COLUMN listing_status TEXT NOT NULL DEFAULT 'candidate'")
    connection.commit()
    return connection


def upsert_listing(listing: source_adapters.JobListing, source_kind: str, *,
                   db_path: str | Path | None = None) -> dict[str, Any]:
    """Save public listing facts by the exact observed URL, never by title similarity."""
    if source_kind not in SOURCE_KINDS or not isinstance(listing, source_adapters.JobListing):
        raise ValueError("Invalid public listing")
    job_url = source_adapters.validate_public_job_url(listing.job_url)
    parsed = urlsplit(job_url)
    if parsed.fragment or any(PRIVATE_QUERY_KEY.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise ValueError("Listing URL is not safe for the shared pool")
    company = _text(listing.company, 300)
    role = _text(listing.role, 500)
    location = _text(listing.location, 300)
    employer_job_id = _text(listing.employer_job_id, 300)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _stamp()
        connection.execute(
            "INSERT INTO global_jobs(pool_id,job_url,company,role,location,employer_job_id,source_kind,first_seen_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(job_url) DO UPDATE SET "
            "company=CASE WHEN excluded.company<>'' THEN excluded.company ELSE global_jobs.company END, "
            "role=CASE WHEN excluded.role<>'' THEN excluded.role ELSE global_jobs.role END, "
            "location=CASE WHEN excluded.location<>'' THEN excluded.location ELSE global_jobs.location END, "
            "employer_job_id=CASE WHEN excluded.employer_job_id<>'' THEN excluded.employer_job_id ELSE global_jobs.employer_job_id END, "
            "last_seen_at=excluded.last_seen_at",
            (secrets.token_hex(16), job_url, company, role, location, employer_job_id,
             source_kind, now, now),
        )
        row = connection.execute("SELECT * FROM global_jobs WHERE job_url=?", (job_url,)).fetchone()
        connection.commit()
        return dict(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_job(pool_id: str, *, db_path: str | Path | None = None) -> dict[str, Any] | None:
    selected = _pool_id(pool_id)
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT j.*,COALESCE(r.status,'') AS rescrape_status,COALESCE(r.last_error,'') AS rescrape_error "
            "FROM global_jobs j LEFT JOIN rescrape_requests r USING(pool_id) WHERE j.pool_id=?",
            (selected,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def reject_collection_page(pool_id: str, job_url: str, *,
                           db_path: str | Path | None = None) -> dict[str, Any]:
    """Hide a confirmed collection page from the public job pool, retaining history."""
    selected = _pool_id(pool_id)
    expected_url = source_adapters.validate_public_job_url(job_url)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM global_jobs WHERE pool_id=? AND job_url=?", (selected, expected_url)
        ).fetchone()
        if row is None:
            raise ValueError("Global job does not match the exact URL")
        if row["listing_status"] != "rejected":
            connection.execute(
                "UPDATE global_jobs SET listing_status='rejected' WHERE pool_id=?", (selected,)
            )
            connection.execute(
                "INSERT INTO global_job_events(pool_id,event_type,event_at,detail) VALUES(?,?,?,?)",
                (selected, "collection_page_rejected", _stamp(), "collection_page"),
            )
        updated = connection.execute("SELECT * FROM global_jobs WHERE pool_id=?", (selected,)).fetchone()
        connection.commit()
        return dict(updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_jobs(*, limit: int = 100, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("Global job limit must be between 1 and 500")
    connection = _connect(db_path)
    try:
        return [dict(row) for row in connection.execute(
            "SELECT j.*,COALESCE(r.status,'') AS rescrape_status FROM global_jobs j "
            "LEFT JOIN rescrape_requests r USING(pool_id) "
            "WHERE j.listing_status='candidate' ORDER BY j.last_seen_at DESC LIMIT ?",
            (limit,),
        )]
    finally:
        connection.close()


def request_rescrape(pool_id: str, job_url: str, *, reason: str = "invalid_url",
                     db_path: str | Path | None = None) -> dict[str, Any]:
    """Collapse repeated requests for one public job without recording a user."""
    selected = _pool_id(pool_id)
    if reason != "invalid_url":
        raise ValueError("Invalid rescrape reason")
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        job = connection.execute("SELECT job_url,listing_status FROM global_jobs WHERE pool_id=?", (selected,)).fetchone()
        if job is None or job["job_url"] != job_url or job["listing_status"] != "candidate":
            raise ValueError("Global job does not match the exact queue URL")
        now = _stamp()
        connection.execute(
            "INSERT INTO rescrape_requests(pool_id,status,reason,requested_at,available_at,updated_at) "
            "VALUES(?,'pending',?,?,?,?) ON CONFLICT(pool_id) DO UPDATE SET "
            "request_count=request_count+1,requested_at=excluded.requested_at,updated_at=excluded.updated_at, "
            "status=CASE WHEN status IN ('pending','claimed') THEN status ELSE 'pending' END, "
            "available_at=CASE WHEN status IN ('pending','claimed') THEN available_at ELSE excluded.available_at END, "
            "attempt_count=CASE WHEN status IN ('pending','claimed') THEN attempt_count ELSE 0 END, "
            "last_error=CASE WHEN status IN ('pending','claimed') THEN last_error ELSE '' END",
            (selected, reason, now, now, now),
        )
        connection.execute(
            "INSERT INTO global_job_events(pool_id,event_type,event_at,detail) VALUES(?,?,?,?)",
            (selected, "rescrape_requested", now, reason),
        )
        row = connection.execute("SELECT * FROM rescrape_requests WHERE pool_id=?", (selected,)).fetchone()
        connection.commit()
        return dict(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_rescrape(*, db_path: str | Path | None = None,
                   lease_seconds: int = 120) -> dict[str, Any] | None:
    if type(lease_seconds) is not int or not 30 <= lease_seconds <= 3600:
        raise ValueError("Invalid rescrape lease duration")
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = datetime.now(timezone.utc)
        stamp = _stamp(now)
        row = connection.execute(
            "SELECT j.*,r.attempt_count,r.status FROM rescrape_requests r "
            "JOIN global_jobs j USING(pool_id) "
            "WHERE (r.status='pending' AND r.available_at<=?) OR "
            "(r.status='claimed' AND r.lease_expires_at<=?) "
            "ORDER BY r.requested_at LIMIT 1", (stamp, stamp),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        token = secrets.token_urlsafe(32)
        expires = _stamp(now + timedelta(seconds=lease_seconds))
        connection.execute(
            "UPDATE rescrape_requests SET status='claimed',attempt_count=attempt_count+1,"
            "lease_token=?,lease_expires_at=?,updated_at=? WHERE pool_id=?",
            (token, expires, stamp, row["pool_id"]),
        )
        connection.commit()
        result = dict(row)
        result.update({"lease_token": token, "lease_expires_at": expires,
                       "attempt_count": int(row["attempt_count"]) + 1})
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def finish_rescrape(pool_id: str, lease_token: str, outcome: str, *, error: str = "",
                    db_path: str | Path | None = None) -> dict[str, Any]:
    selected = _pool_id(pool_id)
    if outcome not in {"verified", "unresolved", "retry"} or error not in RESCRAPE_ERRORS:
        raise ValueError("Invalid rescrape result")
    if not isinstance(lease_token, str) or not lease_token:
        raise ValueError("Invalid rescrape lease")
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM rescrape_requests WHERE pool_id=?", (selected,)).fetchone()
        if (row is None or row["status"] != "claimed" or
                row["lease_expires_at"] <= _stamp() or
                not secrets.compare_digest(row["lease_token"], lease_token)):
            raise ValueError("Rescrape lease is no longer held")
        status = "pending" if outcome == "retry" and int(row["attempt_count"]) < 3 else outcome
        if status == "retry":
            status = "unresolved"
        now = _stamp()
        available = _stamp(datetime.now(timezone.utc) + timedelta(minutes=15)) if status == "pending" else now
        connection.execute(
            "UPDATE rescrape_requests SET status=?,available_at=?,updated_at=?,lease_token='',"
            "lease_expires_at='',last_error=? WHERE pool_id=?",
            (status, available, now, error, selected),
        )
        if status == "verified":
            connection.execute("UPDATE global_jobs SET last_verified_at=? WHERE pool_id=?", (now, selected))
        connection.execute(
            "INSERT INTO global_job_events(pool_id,event_type,event_at,detail) VALUES(?,?,?,?)",
            (selected, "rescrape_" + status, now, error),
        )
        updated = connection.execute("SELECT * FROM rescrape_requests WHERE pool_id=?", (selected,)).fetchone()
        connection.commit()
        return dict(updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
