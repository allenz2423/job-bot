"""Durable application history and narrow structured tools for the job bot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "state" / "jobbot.sqlite3"
APPLICATION_STATUSES = {
    "discovered", "in_progress", "applied", "needs_review", "rejected",
    "interview", "offer", "closed",
}
REVIEW_DECISIONS = {"", "Needs review", "Approved", "Skip", "Duplicate"}
PRIORITIES = {"", "Low", "Normal", "High"}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def database_path(path: str | Path | None = None) -> Path:
    result = Path(path).expanduser() if path else DEFAULT_DB
    return result if result.is_absolute() else ROOT / result


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = database_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(db_path.parent, 0o700)
    except OSError:
        pass
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS applications (
            application_key TEXT PRIMARY KEY,
            job_source TEXT NOT NULL DEFAULT '',
            employer_job_id TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            match_score REAL,
            status TEXT NOT NULL DEFAULT 'discovered'
                CHECK (status IN ('discovered','in_progress','applied','needs_review','rejected','interview','offer','closed')),
            discovered_at TEXT NOT NULL,
            in_progress_at TEXT NOT NULL DEFAULT '',
            submitted_at TEXT NOT NULL DEFAULT '',
            last_email_update TEXT NOT NULL DEFAULT '',
            next_action TEXT NOT NULL DEFAULT '',
            review_decision TEXT NOT NULL DEFAULT '',
            priority TEXT NOT NULL DEFAULT '',
            user_notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS application_urls (
            job_url TEXT PRIMARY KEY,
            application_key TEXT NOT NULL REFERENCES applications(application_key) ON DELETE CASCADE,
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
            status TEXT NOT NULL DEFAULT 'discovered'
                CHECK (status IN ('discovered','in_progress','applied')),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_visited_at TEXT NOT NULL DEFAULT '',
            applied_at TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_primary_url_per_application
            ON application_urls(application_key) WHERE is_primary = 1;
        CREATE INDEX IF NOT EXISTS application_urls_by_application
            ON application_urls(application_key);
        CREATE INDEX IF NOT EXISTS applications_by_employer_job_id
            ON applications(job_source, employer_job_id) WHERE employer_job_id <> '';
        CREATE TABLE IF NOT EXISTS application_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_key TEXT NOT NULL REFERENCES applications(application_key) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            event_at TEXT NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}'
        );
    """)
    connection.commit()
    return connection


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _valid_job_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("job_url is required and must be the exact full URL")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("job_url cannot contain control characters")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("job_url must be an absolute http or https URL")
    return value


def _app_key(job_url: str, job_source: str, employer_job_id: str) -> str:
    identity = (
        f"id\0{job_source.casefold()}\0{employer_job_id}"
        if employer_job_id else f"url\0{job_url}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _row_with_urls(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    result = dict(row)
    urls = connection.execute(
        "SELECT job_url, is_primary, status, first_seen_at, last_seen_at, last_visited_at, applied_at "
        "FROM application_urls WHERE application_key = ? ORDER BY is_primary DESC, first_seen_at",
        (result["application_key"],),
    ).fetchall()
    result["job_urls"] = [dict(url) for url in urls]
    result["job_url"] = next((url["job_url"] for url in urls if url["is_primary"]), "")
    return result


def _find(connection: sqlite3.Connection, job_url: str = "", employer_job_id: str = "",
          job_source: str = "", company: str = "", role: str = "") -> dict[str, object]:
    exact_url_row = None
    if job_url:
        exact_url_row = connection.execute(
            "SELECT a.* FROM applications a JOIN application_urls u USING(application_key) WHERE u.job_url = ?",
            (job_url,),
        ).fetchone()
    if exact_url_row:
        return {"match_type": "exact_url", "application": _row_with_urls(connection, exact_url_row), "possible_duplicates": []}

    id_rows: list[sqlite3.Row] = []
    if employer_job_id:
        if job_source:
            id_rows = connection.execute(
                "SELECT * FROM applications WHERE employer_job_id = ? AND job_source = ?",
                (employer_job_id, job_source),
            ).fetchall()
        else:
            id_rows = connection.execute(
                "SELECT * FROM applications WHERE employer_job_id = ?", (employer_job_id,)
            ).fetchall()
    if len(id_rows) == 1:
        return {"match_type": "employer_job_id", "application": _row_with_urls(connection, id_rows[0]), "possible_duplicates": []}
    if len(id_rows) > 1:
        return {
            "match_type": "ambiguous_employer_job_id",
            "application": None,
            "possible_duplicates": [_row_with_urls(connection, row) for row in id_rows],
        }

    company_key, role_key = _normalized(company), _normalized(role)
    possible: list[sqlite3.Row] = []
    if company_key and role_key:
        rows = connection.execute(
            "SELECT * FROM applications WHERE company <> '' AND role <> '' ORDER BY updated_at DESC"
        ).fetchall()
        possible = [
            row for row in rows
            if _normalized(str(row["company"])) == company_key and _normalized(str(row["role"])) == role_key
        ]
    return {
        "match_type": "possible_company_role" if possible else "none",
        "application": None,
        "possible_duplicates": [_row_with_urls(connection, row) for row in possible],
    }


def find_existing_application(job_url: str = "", employer_job_id: str = "", job_source: str = "",
                               company: str = "", role: str = "", db_path: str | Path | None = None) -> dict[str, object]:
    if job_url:
        _valid_job_url(job_url)
    connection = connect(db_path)
    try:
        return _find(connection, job_url, employer_job_id, job_source, company, role)
    finally:
        connection.close()


def _event(connection: sqlite3.Connection, application_key: str, event_type: str,
           details: dict[str, object] | None = None) -> None:
    connection.execute(
        "INSERT INTO application_events(application_key,event_type,event_at,detail_json) VALUES(?,?,?,?)",
        (application_key, event_type, now_utc(), json.dumps(details or {}, ensure_ascii=False)),
    )


def upsert_application(payload: dict[str, object], db_path: str | Path | None = None) -> dict[str, object]:
    allowed = {"job_url", "job_source", "employer_job_id", "company", "role", "location", "match_score", "next_action"}
    extra = set(payload) - allowed
    if extra:
        raise ValueError(f"Unsupported fields: {', '.join(sorted(extra))}")
    job_url = _valid_job_url(payload.get("job_url"))
    job_source = str(payload.get("job_source", "")).strip()[:200]
    employer_job_id = str(payload.get("employer_job_id", "")).strip()[:300]
    company = str(payload.get("company", "")).strip()[:300]
    role = str(payload.get("role", "")).strip()[:500]
    location = str(payload.get("location", "")).strip()[:300]
    next_action = str(payload.get("next_action", "")).strip()[:1000]
    score_raw = payload.get("match_score")
    match_score = None if score_raw in (None, "") else float(score_raw)
    if match_score is not None and not 0 <= match_score <= 100:
        raise ValueError("match_score must be between 0 and 100")

    connection = connect(db_path)
    try:
        match = _find(connection, job_url, employer_job_id, job_source, company, role)
        possible = match["possible_duplicates"]
        existing = match["application"] if match["application"] else None
        created = existing is None
        application_key = (
            str(existing["application_key"]) if existing
            else _app_key(job_url, job_source, employer_job_id)
        )
        now = now_utc()
        if created:
            initial_status = "needs_review" if possible else "discovered"
            initial_decision = "Needs review" if possible else ""
            connection.execute(
                """INSERT INTO applications(
                    application_key,job_source,employer_job_id,company,role,location,match_score,status,
                    discovered_at,next_action,review_decision,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (application_key, job_source, employer_job_id, company, role, location, match_score,
                 initial_status, now, next_action, initial_decision, now, now),
            )
        else:
            current = connection.execute(
                "SELECT * FROM applications WHERE application_key = ?", (application_key,)
            ).fetchone()
            connection.execute(
                """UPDATE applications SET
                    job_source = CASE WHEN ? <> '' THEN ? ELSE job_source END,
                    employer_job_id = CASE WHEN ? <> '' THEN ? ELSE employer_job_id END,
                    company = CASE WHEN ? <> '' THEN ? ELSE company END,
                    role = CASE WHEN ? <> '' THEN ? ELSE role END,
                    location = CASE WHEN ? <> '' THEN ? ELSE location END,
                    match_score = COALESCE(?, match_score),
                    next_action = CASE WHEN ? <> '' THEN ? ELSE next_action END,
                    updated_at = ?
                   WHERE application_key = ?""",
                (job_source, job_source, employer_job_id, employer_job_id, company, company,
                 role, role, location, location, match_score, next_action, next_action, now, application_key),
            )
            del current

        prior_url = connection.execute(
            "SELECT application_key FROM application_urls WHERE job_url = ?", (job_url,)
        ).fetchone()
        if prior_url and prior_url["application_key"] != application_key:
            raise RuntimeError("This exact URL is already assigned to another application record.")
        current_status = connection.execute(
            "SELECT status FROM applications WHERE application_key = ?", (application_key,)
        ).fetchone()["status"]
        url_status = current_status if current_status in {"discovered", "in_progress", "applied"} else "discovered"
        is_primary = connection.execute(
            "SELECT 1 FROM application_urls WHERE application_key = ? AND is_primary = 1", (application_key,)
        ).fetchone() is None
        connection.execute(
            """INSERT INTO application_urls(job_url,application_key,is_primary,status,first_seen_at,last_seen_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(job_url) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
            (job_url, application_key, int(is_primary), url_status, now, now),
        )
        if created:
            _event(connection, application_key, "discovered", {"job_url": job_url, "needs_review": bool(possible)})
        else:
            _event(connection, application_key, "listing_refreshed", {"job_url": job_url})
        connection.commit()
        row = connection.execute(
            "SELECT * FROM applications WHERE application_key = ?", (application_key,)
        ).fetchone()
        return {
            "created": created,
            "match_type": match["match_type"],
            "application": _row_with_urls(connection, row),
            "possible_duplicates": possible,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_in_progress(application_key: str, job_url: str = "", db_path: str | Path | None = None) -> dict[str, object]:
    connection = connect(db_path)
    try:
        row = connection.execute("SELECT * FROM applications WHERE application_key = ?", (application_key,)).fetchone()
        if not row:
            raise ValueError("Unknown application_key")
        url_row = connection.execute(
            "SELECT * FROM application_urls WHERE application_key = ? AND (? = '' OR job_url = ?) ORDER BY is_primary DESC LIMIT 1",
            (application_key, job_url, job_url),
        ).fetchone()
        if not url_row:
            raise ValueError("No tracked URL found for this application")
        if row["status"] == "applied":
            raise ValueError("This application is already marked applied")
        if row["status"] == "needs_review" or row["review_decision"] in {"Needs review", "Skip", "Duplicate"}:
            raise ValueError("This application is waiting for a user review decision")
        now = now_utc()
        connection.execute(
            "UPDATE applications SET status='in_progress', in_progress_at=COALESCE(NULLIF(in_progress_at,''),?), updated_at=? WHERE application_key=?",
            (now, now, application_key),
        )
        connection.execute(
            "UPDATE application_urls SET status='in_progress', last_visited_at=?, last_seen_at=? WHERE job_url=?",
            (now, now, url_row["job_url"]),
        )
        _event(connection, application_key, "application_started", {"job_url": url_row["job_url"]})
        connection.commit()
        updated = connection.execute("SELECT * FROM applications WHERE application_key=?", (application_key,)).fetchone()
        return _row_with_urls(connection, updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_applied(application_key: str, job_url: str = "", confirmed: bool = False,
                 db_path: str | Path | None = None) -> dict[str, object]:
    if not confirmed:
        raise ValueError("confirmed=true is required after the site confirms submission")
    connection = connect(db_path)
    try:
        row = connection.execute("SELECT * FROM applications WHERE application_key = ?", (application_key,)).fetchone()
        if not row:
            raise ValueError("Unknown application_key")
        if row["status"] == "applied":
            return _row_with_urls(connection, row)
        if row["status"] != "in_progress":
            raise ValueError("Application must be in progress before it can be marked applied")
        url_row = connection.execute(
            "SELECT * FROM application_urls WHERE application_key = ? AND (? = '' OR job_url = ?) ORDER BY is_primary DESC LIMIT 1",
            (application_key, job_url, job_url),
        ).fetchone()
        if not url_row:
            raise ValueError("No tracked URL found for this application")
        if url_row["status"] != "in_progress":
            raise ValueError("The selected job URL must be in progress before it can be marked applied")
        now = now_utc()
        connection.execute(
            "UPDATE applications SET status='applied', submitted_at=?, updated_at=? WHERE application_key=?",
            (now, now, application_key),
        )
        connection.execute(
            "UPDATE application_urls SET status='applied', applied_at=?, last_seen_at=? WHERE application_key=?",
            (now, now, application_key),
        )
        connection.execute(
            "UPDATE application_urls SET last_visited_at=? WHERE job_url=?",
            (now, url_row["job_url"]),
        )
        _event(connection, application_key, "application_confirmed", {"job_url": url_row["job_url"]})
        connection.commit()
        updated = connection.execute("SELECT * FROM applications WHERE application_key=?", (application_key,)).fetchone()
        return _row_with_urls(connection, updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def recover_in_progress(db_path: str | Path | None = None) -> list[dict[str, object]]:
    connection = connect(db_path)
    try:
        rows = connection.execute(
            """SELECT a.*, u.job_url AS resume_url, u.last_visited_at
               FROM applications a JOIN application_urls u USING(application_key)
               WHERE a.status='in_progress' AND u.status='in_progress'
               ORDER BY u.last_visited_at"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def list_applications(db_path: str | Path | None = None) -> list[dict[str, object]]:
    connection = connect(db_path)
    try:
        rows = connection.execute("SELECT * FROM applications ORDER BY discovered_at DESC").fetchall()
        return [_row_with_urls(connection, row) for row in rows]
    finally:
        connection.close()


def update_user_fields(application_key: str, priority: str = "", user_notes: str = "",
                       review_decision: str = "", db_path: str | Path | None = None) -> None:
    if priority not in PRIORITIES:
        raise ValueError("priority must be Low, Normal, High, or blank")
    if review_decision not in REVIEW_DECISIONS:
        raise ValueError("review_decision must be Needs review, Approved, Skip, Duplicate, or blank")
    connection = connect(db_path)
    try:
        current = connection.execute(
            "SELECT status, review_decision FROM applications WHERE application_key=?", (application_key,)
        ).fetchone()
        if current is None:
            raise ValueError("Unknown application_key")
        cursor = connection.execute(
            """UPDATE applications SET priority=?, user_notes=?, review_decision=?,
                   status=CASE WHEN status='needs_review' AND ?='Approved' THEN 'discovered' ELSE status END,
                   updated_at=? WHERE application_key=?""",
            (priority, user_notes[:4000], review_decision, review_decision, now_utc(), application_key),
        )
        if cursor.rowcount == 0:
            raise ValueError("Unknown application_key")
        if current["review_decision"] != review_decision:
            _event(connection, application_key, "review_decision_updated", {"decision": review_decision})
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
