"""Accounts, sessions, private tenant paths, and encrypted candidate profiles."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

try:
    from . import application_store, profile_schema
except ImportError:
    import application_store
    import profile_schema


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.environ.get("APP_STATE_DIR", ROOT / "state"))
PRIVATE_DIR = Path(os.environ.get("APP_PRIVATE_SECRETS_DIR", ROOT / "secrets" / "app"))
CONTROL_DB = STATE_DIR / "control.sqlite3"
BOOTSTRAP_TOKEN = STATE_DIR / "bootstrap_token"
MASTER_KEY = PRIVATE_DIR / "profile.key"
SESSION_HOURS = 12
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DISCOVERY_INTERVAL_MINUTES = (1, 5, 10, 15, 30, 60, 120, 240, 360, 720, 1440)


def _stamp(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _write_new_secret(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _connect() -> sqlite3.Connection:
    _private_dir(STATE_DIR)
    connection = sqlite3.connect(CONTROL_DB, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    os.chmod(CONTROL_DB, 0o600)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('owner','member')),
            storage_key TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
            must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0,1)),
            created_at TEXT NOT NULL,
            last_login_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            csrf_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions(user_id);
        CREATE TABLE IF NOT EXISTS login_attempts (
            email TEXT PRIMARY KEY,
            failed_count INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT NOT NULL DEFAULT ''
        );
    """)
    connection.commit()
    return connection


def initialize() -> None:
    _private_dir(PRIVATE_DIR)
    if not MASTER_KEY.exists():
        try:
            _write_new_secret(MASTER_KEY, Fernet.generate_key().decode("ascii"))
        except FileExistsError:
            pass
    os.chmod(MASTER_KEY, 0o600)
    connection = _connect()
    try:
        owner_exists = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    finally:
        connection.close()
    if not owner_exists and not BOOTSTRAP_TOKEN.exists():
        try:
            _write_new_secret(BOOTSTRAP_TOKEN, secrets.token_urlsafe(32))
        except FileExistsError:
            pass


def needs_setup() -> bool:
    connection = _connect()
    try:
        return connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
    finally:
        connection.close()


def _email(value: str) -> str:
    cleaned = value.strip().lower()
    if len(cleaned) > 254 or not EMAIL_RE.fullmatch(cleaned):
        raise ValueError("Enter a valid email address")
    return cleaned


def _password(value: str) -> str:
    if not isinstance(value, str) or not 12 <= len(value) <= 1024:
        raise ValueError("Password must be between 12 and 1024 characters")
    return value


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1,
                            dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt$16384$8$1$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def _check_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.urlsafe_b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


def create_owner(token: str, email: str, password: str) -> dict[str, object]:
    clean_email = _email(email)
    password_hash = _hash_password(_password(password))
    try:
        expected = BOOTSTRAP_TOKEN.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError("Setup token is unavailable") from exc
    if not hmac.compare_digest(token, expected):
        raise ValueError("Invalid setup token")
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise ValueError("Owner account already exists")
        now = _stamp()
        cursor = connection.execute(
            "INSERT INTO users(email,password_hash,role,storage_key,created_at) VALUES(?,?,?,?,?)",
            (clean_email, password_hash, "owner", "legacy", now),
        )
        connection.commit()
        BOOTSTRAP_TOKEN.unlink(missing_ok=True)
        return {"user_id": cursor.lastrowid, "email": clean_email, "role": "owner", "storage_key": "legacy"}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_member(email: str) -> tuple[dict[str, object], str]:
    clean_email = _email(email)
    temporary_password = secrets.token_urlsafe(24)
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _stamp()
        storage_key = secrets.token_hex(16)
        cursor = connection.execute(
            "INSERT INTO users(email,password_hash,role,storage_key,must_change_password,created_at) "
            "VALUES(?,?,?,?,1,?)",
            (clean_email, _hash_password(temporary_password), "member", storage_key, now),
        )
        user = {"user_id": cursor.lastrowid, "email": clean_email, "role": "member", "storage_key": storage_key}
        initialize_tenant(user)
        connection.commit()
        return user, temporary_password
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_users() -> list[dict[str, object]]:
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT user_id,email,role,active,must_change_password,created_at,last_login_at "
            "FROM users ORDER BY user_id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def list_worker_users() -> list[dict[str, object]]:
    """Return only the account fields required by isolated worker loops."""
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT user_id,email,role,storage_key,active,must_change_password "
            "FROM users ORDER BY user_id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def login(email: str, password: str) -> tuple[str, str, dict[str, object]] | None:
    try:
        clean_email = _email(email)
    except ValueError:
        return None
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        attempts = connection.execute("SELECT * FROM login_attempts WHERE email=?", (clean_email,)).fetchone()
        if attempts and attempts["locked_until"] and attempts["locked_until"] > _stamp():
            connection.commit()
            return None
        row = connection.execute("SELECT * FROM users WHERE email=? AND active=1", (clean_email,)).fetchone()
        # Always perform a comparable password hash for nonexistent users.
        fallback = "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$mM7xuo3jr7Eds2YEHHCrYoOFsLo4vXqOQ32-tAKcR3I="
        valid = _check_password(password, row["password_hash"] if row else fallback)
        if not row or not valid:
            count = int(attempts["failed_count"]) + 1 if attempts else 1
            locked = _stamp(_utc_now() + timedelta(minutes=15)) if count >= 5 else ""
            connection.execute(
                "INSERT INTO login_attempts(email,failed_count,locked_until) VALUES(?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET failed_count=excluded.failed_count,locked_until=excluded.locked_until",
                (clean_email, count, locked),
            )
            connection.commit()
            return None
        connection.execute("DELETE FROM login_attempts WHERE email=?", (clean_email,))
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        connection.execute(
            "INSERT INTO sessions(token_hash,user_id,csrf_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
            (_hash_token(token), row["user_id"], _hash_token(csrf),
             _stamp(_utc_now() + timedelta(hours=SESSION_HOURS)), _stamp()),
        )
        connection.execute("UPDATE users SET last_login_at=? WHERE user_id=?", (_stamp(), row["user_id"]))
        connection.commit()
        return token, csrf, {
            "user_id": row["user_id"],
            "email": row["email"],
            "role": row["role"],
            "storage_key": row["storage_key"],
            "must_change_password": row["must_change_password"],
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def session(token: str) -> dict[str, object] | None:
    if not token or len(token) > 200:
        return None
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT u.user_id,u.email,u.role,u.storage_key,u.active,u.must_change_password,"
            "s.csrf_hash,s.expires_at FROM sessions s JOIN users u USING(user_id) "
            "WHERE s.token_hash=?", (_hash_token(token),)
        ).fetchone()
        if not row or not row["active"] or row["expires_at"] <= _stamp():
            return None
        return dict(row)
    finally:
        connection.close()


def valid_csrf(user_session: dict[str, object], token: str) -> bool:
    return bool(token) and hmac.compare_digest(str(user_session["csrf_hash"]), _hash_token(token))


def logout(token: str) -> None:
    connection = _connect()
    try:
        connection.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
        connection.commit()
    finally:
        connection.close()


def change_password(user_id: int, old_password: str, new_password: str) -> None:
    new_hash = _hash_password(_password(new_password))
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT password_hash FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row or not _check_password(old_password, row["password_hash"]):
            raise ValueError("Current password is incorrect")
        connection.execute(
            "UPDATE users SET password_hash=?,must_change_password=0 WHERE user_id=?",
            (new_hash, user_id),
        )
        connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def tenant_db_path(user: dict[str, object]) -> Path:
    key = str(user["storage_key"])
    if key == "legacy":
        return STATE_DIR / "jobbot.sqlite3"
    if not re.fullmatch(r"[0-9a-f]{32}", key):
        raise ValueError("Invalid tenant storage key")
    return STATE_DIR / "users" / key / "jobbot.sqlite3"


def initialize_tenant(user: dict[str, object]) -> Path:
    path = tenant_db_path(user)
    _private_dir(path.parent)
    connection = application_store.connect(path)
    try:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS candidate_profile (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                ciphertext BLOB NOT NULL,
                version INTEGER NOT NULL CHECK (version >= 1),
                approved_version INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resume_documents (
                document_id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                file_format TEXT NOT NULL CHECK (file_format IN ('pdf','docx','txt')),
                sha256 TEXT NOT NULL UNIQUE,
                content_ciphertext BLOB NOT NULL,
                text_ciphertext BLOB NOT NULL,
                text_chars INTEGER NOT NULL,
                purpose TEXT NOT NULL DEFAULT 'unassigned'
                    CHECK (purpose IN ('primary','targeted','unassigned')),
                display_name TEXT NOT NULL DEFAULT '',
                target_role_region TEXT NOT NULL DEFAULT '',
                metadata_version INTEGER NOT NULL DEFAULT 1 CHECK (metadata_version >= 1),
                approved_at TEXT NOT NULL DEFAULT '',
                uploaded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS resume_documents_recent
                ON resume_documents(uploaded_at DESC, document_id DESC);
            CREATE TABLE IF NOT EXISTS user_settings (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                discovery_paused INTEGER NOT NULL DEFAULT 1 CHECK (discovery_paused IN (0,1)),
                application_paused INTEGER NOT NULL DEFAULT 1 CHECK (application_paused IN (0,1)),
                autonomy_mode TEXT NOT NULL DEFAULT 'discovery_only'
                    CHECK (autonomy_mode IN ('discovery_only','prepare_for_review','submit_eligible')),
                daily_application_limit INTEGER NOT NULL DEFAULT 5 CHECK (daily_application_limit BETWEEN 1 AND 100),
                daily_application_unlimited INTEGER NOT NULL DEFAULT 0 CHECK (daily_application_unlimited IN (0,1)),
                browser_concurrency INTEGER NOT NULL DEFAULT 1 CHECK (browser_concurrency BETWEEN 1 AND 8),
                auto_apply_hosts TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_sources (
                source_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'job_page',
                url_hash TEXT NOT NULL UNIQUE,
                url_ciphertext BLOB NOT NULL,
                job_title TEXT NOT NULL DEFAULT '',
                max_jobs INTEGER NOT NULL DEFAULT 25,
                interval_minutes INTEGER NOT NULL DEFAULT 360
                    CHECK (interval_minutes IN (1,5,10,15,30,60,120,240,360,720,1440)),
                timespan TEXT NOT NULL DEFAULT 'any',
                permitted INTEGER NOT NULL CHECK (permitted IN (0,1)),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
                run_requested_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_searches (
                search_id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_hash TEXT NOT NULL UNIQUE,
                phrase_ciphertext BLOB NOT NULL,
                location_ciphertext BLOB NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discovery_integrations (
                provider TEXT PRIMARY KEY,
                credentials_ciphertext BLOB NOT NULL,
                country TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
                updated_at TEXT NOT NULL
            );
        """)
        source_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(discovery_sources)")}
        if "source_type" not in source_columns:
            connection.execute("ALTER TABLE discovery_sources ADD COLUMN source_type TEXT NOT NULL DEFAULT 'job_page'")
        if "job_title" not in source_columns:
            connection.execute("ALTER TABLE discovery_sources ADD COLUMN job_title TEXT NOT NULL DEFAULT ''")
        if "max_jobs" not in source_columns:
            connection.execute("ALTER TABLE discovery_sources ADD COLUMN max_jobs INTEGER NOT NULL DEFAULT 25")
        if "timespan" not in source_columns:
            connection.execute("ALTER TABLE discovery_sources ADD COLUMN timespan TEXT NOT NULL DEFAULT 'any'")
        if "interval_minutes" not in source_columns:
            legacy_interval = 360
            legacy_target: int | None = None
            settings_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(user_settings)")}
            if "scrape_interval_minutes" in settings_columns:
                legacy_row = connection.execute(
                    "SELECT scrape_interval_minutes FROM user_settings WHERE singleton=1"
                ).fetchone()
                if legacy_row and int(legacy_row[0]) in DISCOVERY_INTERVAL_MINUTES:
                    legacy_interval = int(legacy_row[0])
            if "scrape_target_count" in settings_columns:
                legacy_row = connection.execute(
                    "SELECT scrape_target_count FROM user_settings WHERE singleton=1"
                ).fetchone()
                if legacy_row and 1 <= int(legacy_row[0]) <= 200:
                    legacy_target = int(legacy_row[0])
            allowed_intervals = ",".join(str(value) for value in DISCOVERY_INTERVAL_MINUTES)
            connection.execute(
                "ALTER TABLE discovery_sources ADD COLUMN interval_minutes INTEGER NOT NULL DEFAULT 360 "
                f"CHECK (interval_minutes IN ({allowed_intervals}))"
            )
            connection.execute(
                "UPDATE discovery_sources SET interval_minutes=?", (legacy_interval,)
            )
            if legacy_target is not None:
                connection.execute(
                    "UPDATE discovery_sources SET max_jobs=MIN(max_jobs,?)", (legacy_target,)
                )
        if "run_requested_at" not in source_columns:
            connection.execute(
                "ALTER TABLE discovery_sources ADD COLUMN run_requested_at TEXT NOT NULL DEFAULT ''"
            )
        connection.execute("BEGIN IMMEDIATE")
        existing = {str(row["name"]) for row in connection.execute("PRAGMA table_info(resume_documents)")}
        for name, definition in (
            ("purpose", "TEXT NOT NULL DEFAULT 'unassigned' CHECK (purpose IN ('primary','targeted','unassigned'))"),
            ("display_name", "TEXT NOT NULL DEFAULT ''"),
            ("target_role_region", "TEXT NOT NULL DEFAULT ''"),
            ("metadata_version", "INTEGER NOT NULL DEFAULT 1 CHECK (metadata_version >= 1)"),
            ("approved_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in existing:
                connection.execute(f"ALTER TABLE resume_documents ADD COLUMN {name} {definition}")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS resume_documents_one_primary "
            "ON resume_documents(purpose) WHERE purpose='primary'"
        )
        settings_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(user_settings)")}
        if "auto_apply_hosts" not in settings_columns:
            connection.execute("ALTER TABLE user_settings ADD COLUMN auto_apply_hosts TEXT NOT NULL DEFAULT ''")
        if "daily_application_unlimited" not in settings_columns:
            connection.execute(
                "ALTER TABLE user_settings ADD COLUMN daily_application_unlimited "
                "INTEGER NOT NULL DEFAULT 0 CHECK (daily_application_unlimited IN (0,1))"
            )
        connection.execute("INSERT OR IGNORE INTO user_settings(singleton,updated_at) VALUES(1,?)", (_stamp(),))
        connection.execute("DELETE FROM discovery_integrations WHERE provider='brave'")
        connection.commit()
    finally:
        connection.close()
    return path


def _fernet(user: dict[str, object], purpose: str = "profile") -> Fernet:
    master = base64.urlsafe_b64decode(MASTER_KEY.read_text(encoding="ascii"))
    if len(master) != 32:
        raise RuntimeError("Invalid profile encryption key")
    derived = hmac.new(master, (purpose + ":" + str(user["storage_key"])).encode(), hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


_RESUME_METADATA_COLUMNS = (
    "document_id,filename,file_format,sha256,text_chars,purpose,display_name,"
    "target_role_region,metadata_version,approved_at,uploaded_at"
)


def _resume_metadata(purpose: str, display_name: str, target_role_region: str) -> tuple[str, str, str]:
    if purpose not in {"primary", "targeted", "unassigned"}:
        raise ValueError("Choose a resume type")
    if not isinstance(display_name, str) or not isinstance(target_role_region, str):
        raise ValueError("Invalid resume label")
    name = display_name.strip()
    target = target_role_region.strip()
    if len(name) > 200 or len(target) > 300 or any(ord(char) < 32 or ord(char) == 127 for char in name + target):
        raise ValueError("Resume labels must be short single lines")
    if purpose == "targeted" and not target:
        raise ValueError("Enter the role or region for a targeted resume")
    if purpose != "targeted":
        target = ""
    return purpose, name, target


def _demote_primary(connection: sqlite3.Connection, except_id: int) -> None:
    connection.execute(
        "UPDATE resume_documents SET purpose='unassigned',target_role_region='',"
        "metadata_version=metadata_version+1,approved_at='' "
        "WHERE purpose='primary' AND document_id<>?",
        (except_id,),
    )


def save_resume_document(user: dict[str, object], filename: str, file_format: str,
                         content: bytes, extracted_text: str, *, purpose: str = "unassigned",
                         display_name: str = "", target_role_region: str = "") -> dict[str, object]:
    if (not isinstance(filename, str) or not filename or len(filename) > 200 or
            any(char in filename for char in "/\\\r\n\0")):
        raise ValueError("Invalid resume filename")
    if file_format not in {"pdf", "docx", "txt"}:
        raise ValueError("Unsupported resume format")
    if not isinstance(content, bytes) or not 0 < len(content) <= 5 * 1024 * 1024:
        raise ValueError("Resume file must be 5 MB or smaller")
    if not isinstance(extracted_text, str) or len(extracted_text) > 100000:
        raise ValueError("Extracted resume text is too long")
    purpose, display_name, target_role_region = _resume_metadata(purpose, display_name, target_role_region)
    sha256 = hashlib.sha256(content).hexdigest()
    file_ciphertext = _fernet(user, "resume-file").encrypt(content)
    text_ciphertext = _fernet(user, "resume-text").encrypt(extracted_text.encode("utf-8"))
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT document_id,purpose,display_name,target_role_region FROM resume_documents WHERE sha256=?",
            (sha256,),
        ).fetchone()
        document_id = int(existing["document_id"]) if existing else 0
        if purpose == "primary":
            _demote_primary(connection, document_id)
        if existing is None:
            cursor = connection.execute(
                "INSERT INTO resume_documents(filename,file_format,sha256,content_ciphertext,"
                "text_ciphertext,text_chars,purpose,display_name,target_role_region,uploaded_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (filename, file_format, sha256, file_ciphertext, text_ciphertext,
                 len(extracted_text), purpose, display_name, target_role_region, _stamp()),
            )
            document_id = int(cursor.lastrowid)
        elif (existing["purpose"], existing["display_name"], existing["target_role_region"]) != (
                purpose, display_name, target_role_region):
            connection.execute(
                "UPDATE resume_documents SET purpose=?,display_name=?,target_role_region=?,"
                "metadata_version=metadata_version+1,approved_at='' WHERE document_id=?",
                (purpose, display_name, target_role_region, document_id),
            )
        row = connection.execute(
            f"SELECT {_RESUME_METADATA_COLUMNS} FROM resume_documents WHERE document_id=?",
            (document_id,),
        ).fetchone()
        connection.commit()
        return dict(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_resume_documents(user: dict[str, object]) -> list[dict[str, object]]:
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        rows = connection.execute(
            f"SELECT {_RESUME_METADATA_COLUMNS} FROM resume_documents "
            "ORDER BY CASE purpose WHEN 'primary' THEN 0 WHEN 'targeted' THEN 1 ELSE 2 END,"
            "uploaded_at DESC,document_id DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def update_resume_document_metadata(user: dict[str, object], document_id: int, *, purpose: str,
                                    display_name: str, target_role_region: str,
                                    expected_metadata_version: int) -> dict[str, object]:
    if type(document_id) is not int or document_id < 1 or type(expected_metadata_version) is not int or expected_metadata_version < 1:
        raise ValueError("Invalid resume document version")
    purpose, display_name, target_role_region = _resume_metadata(purpose, display_name, target_role_region)
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT purpose,display_name,target_role_region,metadata_version FROM resume_documents WHERE document_id=?",
            (document_id,),
        ).fetchone()
        if current is None:
            raise ValueError("Resume document not found")
        if int(current["metadata_version"]) != expected_metadata_version:
            raise ValueError("This resume changed. Reload the profile before saving.")
        if purpose == "primary":
            _demote_primary(connection, document_id)
        if (current["purpose"], current["display_name"], current["target_role_region"]) != (
                purpose, display_name, target_role_region):
            connection.execute(
                "UPDATE resume_documents SET purpose=?,display_name=?,target_role_region=?,"
                "metadata_version=metadata_version+1,approved_at='' WHERE document_id=?",
                (purpose, display_name, target_role_region, document_id),
            )
        row = connection.execute(
            f"SELECT {_RESUME_METADATA_COLUMNS} FROM resume_documents WHERE document_id=?",
            (document_id,),
        ).fetchone()
        connection.commit()
        return dict(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def approve_resume_document(user: dict[str, object], document_id: int, *,
                            expected_metadata_version: int) -> dict[str, object]:
    if type(document_id) is not int or document_id < 1 or type(expected_metadata_version) is not int or expected_metadata_version < 1:
        raise ValueError("Invalid resume document version")
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT purpose,metadata_version,approved_at FROM resume_documents WHERE document_id=?",
            (document_id,),
        ).fetchone()
        if current is None:
            raise ValueError("Resume document not found")
        if int(current["metadata_version"]) != expected_metadata_version:
            raise ValueError("This resume changed. Reload the profile before approving.")
        if current["purpose"] == "unassigned":
            raise ValueError("Choose a resume type before approving it")
        if not current["approved_at"]:
            connection.execute(
                "UPDATE resume_documents SET approved_at=? WHERE document_id=?",
                (_stamp(), document_id),
            )
        row = connection.execute(
            f"SELECT {_RESUME_METADATA_COLUMNS} FROM resume_documents WHERE document_id=?",
            (document_id,),
        ).fetchone()
        connection.commit()
        return dict(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def revoke_resume_document_approval(user: dict[str, object], document_id: int, *,
                                    expected_metadata_version: int) -> dict[str, object]:
    if type(document_id) is not int or document_id < 1 or type(expected_metadata_version) is not int or expected_metadata_version < 1:
        raise ValueError("Invalid resume document version")
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT metadata_version FROM resume_documents WHERE document_id=?", (document_id,),
        ).fetchone()
        if current is None:
            raise ValueError("Resume document not found")
        if int(current["metadata_version"]) != expected_metadata_version:
            raise ValueError("This resume changed. Reload the profile before changing approval.")
        connection.execute("UPDATE resume_documents SET approved_at='' WHERE document_id=?", (document_id,))
        row = connection.execute(
            f"SELECT {_RESUME_METADATA_COLUMNS} FROM resume_documents WHERE document_id=?",
            (document_id,),
        ).fetchone()
        connection.commit()
        return dict(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def load_resume_document(user: dict[str, object], document_id: int) -> dict[str, object]:
    if isinstance(document_id, bool) or not isinstance(document_id, int) or document_id < 1:
        raise ValueError("Invalid resume document")
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        row = connection.execute(
            "SELECT document_id,filename,file_format,content_ciphertext FROM resume_documents WHERE document_id=?",
            (document_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Resume document not found")
        try:
            content = _fernet(user, "resume-file").decrypt(row["content_ciphertext"])
        except InvalidToken as exc:
            raise RuntimeError("Resume document could not be decrypted") from exc
        return {"document_id": row["document_id"], "filename": row["filename"],
                "file_format": row["file_format"], "content": content}
    finally:
        connection.close()


def get_profile(user: dict[str, object]) -> dict[str, object]:
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        row = connection.execute("SELECT * FROM candidate_profile WHERE singleton=1").fetchone()
        if row is None:
            return {"version": 0, "approved_version": 0, "data": {}}
        try:
            data = json.loads(_fernet(user).decrypt(row["ciphertext"]))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Candidate profile could not be decrypted") from exc
        return {"version": row["version"], "approved_version": row["approved_version"], "data": data}
    finally:
        connection.close()


def save_profile(user: dict[str, object], fields: dict[str, object], *,
                 expected_version: int) -> dict[str, object]:
    if not isinstance(expected_version, int) or expected_version < 0:
        raise ValueError("Invalid profile version")
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT version FROM candidate_profile WHERE singleton=1").fetchone()
        version = int(current["version"]) if current else 0
        if version != expected_version:
            raise ValueError("The profile changed. Reload it before saving.")
        data = profile_schema.validate_profile({
            "schema_version": profile_schema.SCHEMA_VERSION,
            "version": version + 1,
            "approval_state": "draft",
            "approved_version": None,
            "fields": fields,
        })
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ciphertext = _fernet(user).encrypt(encoded)
        connection.execute(
            "INSERT INTO candidate_profile(singleton,ciphertext,version,approved_version,updated_at) "
            "VALUES(1,?,1,0,?) ON CONFLICT(singleton) DO UPDATE SET "
            "ciphertext=excluded.ciphertext,version=candidate_profile.version+1,approved_version=0,updated_at=excluded.updated_at",
            (ciphertext, _stamp()),
        )
        connection.commit()
        return get_profile(user)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def approve_profile(user: dict[str, object], *, expected_version: int) -> dict[str, object]:
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM candidate_profile WHERE singleton=1").fetchone()
        if row is None:
            raise ValueError("Save a profile before approving it")
        if int(row["version"]) != expected_version:
            raise ValueError("The profile changed. Reload it before approving.")
        try:
            data = json.loads(_fernet(user).decrypt(row["ciphertext"]))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Candidate profile could not be decrypted") from exc
        data["approval_state"] = "approved"
        data["approved_version"] = int(row["version"])
        data = profile_schema.validate_profile(data)
        if not any(field["status"] == "known" for field in data["fields"].values()):
            raise ValueError("Enter at least one known fact before approving the profile")
        ciphertext = _fernet(user).encrypt(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        connection.execute(
            "UPDATE candidate_profile SET ciphertext=?,approved_version=?,updated_at=? WHERE singleton=1",
            (ciphertext, row["version"], _stamp()),
        )
        connection.commit()
        return get_profile(user)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_settings(user: dict[str, object]) -> dict[str, object]:
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        row = connection.execute("SELECT * FROM user_settings WHERE singleton=1").fetchone()
        settings = dict(row)
        # Retained as a legacy database column for existing installations;
        # host authorization is no longer part of Submit eligible mode.
        settings.pop("auto_apply_hosts", None)
        unlimited = bool(settings.pop("daily_application_unlimited", 0))
        if unlimited:
            settings["daily_application_limit"] = -1
        return settings
    finally:
        connection.close()


def _source_url_hash(user: dict[str, object], url: str) -> str:
    master = base64.urlsafe_b64decode(MASTER_KEY.read_text(encoding="ascii"))
    key = hmac.new(master, ("discovery-source-index:" + str(user["storage_key"])).encode(),
                   hashlib.sha256).digest()
    return hmac.new(key, url.encode("utf-8"), hashlib.sha256).hexdigest()


def _source_id(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("Invalid source ID")
    try:
        source_id = int(value)
    except (TypeError, ValueError):
        raise ValueError("Invalid source ID") from None
    if source_id < 1:
        raise ValueError("Invalid source ID")
    return source_id


def list_discovery_sources(user: dict[str, object]) -> list[dict[str, object]]:
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        rows = connection.execute(
            "SELECT source_id,name,source_type,url_ciphertext,job_title,max_jobs,interval_minutes,timespan,permitted,enabled,run_requested_at,created_at,updated_at "
            "FROM discovery_sources ORDER BY source_id"
        ).fetchall()
        result: list[dict[str, object]] = []
        cipher = _fernet(user, "discovery-source")
        for row in rows:
            try:
                url = cipher.decrypt(row["url_ciphertext"]).decode("utf-8")
            except (InvalidToken, UnicodeDecodeError) as exc:
                raise RuntimeError("A discovery source could not be decrypted") from exc
            result.append({
                "source_id": int(row["source_id"]),
                "name": str(row["name"]),
                "url": url,
                "job_title": str(row["job_title"] or row["name"]),
                "max_jobs": int(row["max_jobs"] if row["max_jobs"] is not None else 25),
                "interval_minutes": int(row["interval_minutes"] if row["interval_minutes"] is not None else 360),
                "timespan": str(row["timespan"] or "any"),
                "source_type": str(row["source_type"]),
                "permitted": bool(row["permitted"]),
                "enabled": bool(row["enabled"]),
                "run_requested_at": str(row["run_requested_at"] or ""),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            })
        return result
    finally:
        connection.close()


def add_discovery_source(user: dict[str, object], name: str = "", url: str = "", *,
                         job_title: str | list[str] = "",
                         max_jobs: int = 25,
                         interval_minutes: int = 360,
                         timespan: str = "any",
                         source_type: str = "job_page",
                         permitted: bool = False, enabled: bool = False) -> int:
    try:
        from . import local_llm
    except ImportError:
        import local_llm
    allowed_list = local_llm.parse_allowed_titles(job_title or name)
    if not allowed_list:
        raise ValueError("Enter at least one allowed job title")
    clean_title = ", ".join(allowed_list)[:500]
    clean_name = (name or clean_title).strip()[:200]
    if type(permitted) is not bool or permitted is not True:
        raise ValueError("Confirm that this source permits automated access")
    if source_type not in {"feed", "job_page", "scrape_jobs"}:
        raise ValueError("Invalid source type")
    if bool(url.strip()) != (source_type in {"feed", "job_page"}):
        raise ValueError("Choose a URL for a feed or job page, or leave it blank for web search")
    if type(enabled) is not bool:
        raise ValueError("Invalid source setting")
    try:
        from . import source_adapters
    except ImportError:
        import source_adapters
    checked_url = ""
    if url.strip():
        checked_url = source_adapters.validate_feed_url(url.strip())
    try:
        clean_max = int(max_jobs)
    except (TypeError, ValueError):
        raise ValueError("Maximum jobs must be a whole number") from None
    if not 1 <= clean_max <= 200:
        raise ValueError("Maximum jobs must be between 1 and 200")
    try:
        clean_interval = int(interval_minutes)
    except (TypeError, ValueError):
        raise ValueError("Choose a supported scrape interval") from None
    if clean_interval not in DISCOVERY_INTERVAL_MINUTES:
        raise ValueError("Choose a supported scrape interval")
    clean_timespan = timespan if timespan in {"24h", "3d", "7d", "14d", "30d", "any"} else "any"

    ciphertext = _fernet(user, "discovery-source").encrypt(checked_url.encode("utf-8"))
    url_hash = _source_url_hash(user, f"{checked_url}|{clean_title.casefold()}")
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        count = int(connection.execute("SELECT COUNT(*) FROM discovery_sources").fetchone()[0])
        if count >= 100:
            raise ValueError("The source limit is 100 scrape jobs")
        now = _stamp()
        try:
            cursor = connection.execute(
                "INSERT INTO discovery_sources(name,source_type,url_hash,url_ciphertext,job_title,max_jobs,interval_minutes,timespan,permitted,enabled,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (clean_name, source_type, url_hash, ciphertext, clean_title, clean_max, clean_interval,
                 clean_timespan, 1, int(enabled), now, now),
            )
        except sqlite3.IntegrityError:
            raise ValueError("This scrape job is already in your sources") from None
        connection.commit()
        return int(cursor.lastrowid)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_discovery_source_limits(user: dict[str, object], source_id: object,
                                   max_jobs: object, interval_minutes: object) -> None:
    selected_id = _source_id(source_id)
    try:
        clean_max = int(max_jobs)
        clean_interval = int(interval_minutes)
    except (TypeError, ValueError):
        raise ValueError("Target count and interval must be valid numbers") from None
    if not 1 <= clean_max <= 200:
        raise ValueError("Maximum jobs must be between 1 and 200")
    if clean_interval not in DISCOVERY_INTERVAL_MINUTES:
        raise ValueError("Choose a supported scrape interval")
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        cursor = connection.execute(
            "UPDATE discovery_sources SET max_jobs=?,interval_minutes=?,updated_at=? "
            "WHERE source_id=? AND permitted=1",
            (clean_max, clean_interval, _stamp(), selected_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Scrape job not found")
        connection.commit()
    finally:
        connection.close()


def request_discovery_source_run(user: dict[str, object], source_id: object) -> str:
    selected_id = _source_id(source_id)
    settings = get_settings(user)
    if settings["discovery_paused"]:
        raise ValueError("Unpause discovery before running this scrape job")
    profile = get_profile(user)
    if not profile["version"] or profile["approved_version"] != profile["version"]:
        raise ValueError("Approve your current profile before running this scrape job")
    requested_at = _stamp()
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        cursor = connection.execute(
            "UPDATE discovery_sources SET run_requested_at=? "
            "WHERE source_id=? AND permitted=1 AND enabled=1",
            (requested_at, selected_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Enabled scrape job not found")
        connection.commit()
        return requested_at
    finally:
        connection.close()


def clear_discovery_source_run_request(user: dict[str, object], source_id: object,
                                       requested_at: str) -> bool:
    selected_id = _source_id(source_id)
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        cursor = connection.execute(
            "UPDATE discovery_sources SET run_requested_at='' "
            "WHERE source_id=? AND run_requested_at=?",
            (selected_id, requested_at),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def set_discovery_source_enabled(user: dict[str, object], source_id: object,
                                 enabled: bool) -> None:
    selected_id = _source_id(source_id)
    if type(enabled) is not bool:
        raise ValueError("Invalid source setting")
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        cursor = connection.execute(
            "UPDATE discovery_sources SET enabled=?,run_requested_at=CASE WHEN ?=0 THEN '' ELSE run_requested_at END,updated_at=? "
            "WHERE source_id=? AND permitted=1",
            (int(enabled), int(enabled), _stamp(), selected_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Source not found")
        connection.commit()
    finally:
        connection.close()


def remove_discovery_source(user: dict[str, object], source_id: object) -> None:
    selected_id = _source_id(source_id)
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        cursor = connection.execute("DELETE FROM discovery_sources WHERE source_id=?", (selected_id,))
        if cursor.rowcount != 1:
            raise ValueError("Source not found")
        connection.commit()
    finally:
        connection.close()


def _search_query_hash(user: dict[str, object], phrase: str, location: str) -> str:
    master = base64.urlsafe_b64decode(MASTER_KEY.read_text(encoding="ascii"))
    key = hmac.new(master, ("discovery-search-index:" + str(user["storage_key"])).encode(),
                   hashlib.sha256).digest()
    normalized = phrase.casefold() + "\0" + location.casefold()
    return hmac.new(key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def _search_text(value: object, label: str, *, required: bool) -> str:
    if type(value) is not str:
        raise ValueError(f"Invalid {label}")
    result = " ".join(value.split())
    if (required and not result) or len(result) > 120 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"Enter a {label} of at most 120 characters")
    return result


def list_discovery_searches(user: dict[str, object]) -> list[dict[str, object]]:
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        cipher = _fernet(user, "discovery-search")
        result: list[dict[str, object]] = []
        for row in connection.execute(
            "SELECT search_id,phrase_ciphertext,location_ciphertext,enabled,created_at,updated_at "
            "FROM discovery_searches ORDER BY search_id"
        ):
            try:
                phrase = cipher.decrypt(row["phrase_ciphertext"]).decode("utf-8")
                location = cipher.decrypt(row["location_ciphertext"]).decode("utf-8")
            except (InvalidToken, UnicodeDecodeError) as exc:
                raise RuntimeError("A job search could not be decrypted") from exc
            result.append({
                "search_id": int(row["search_id"]), "phrase": phrase,
                "location": location, "enabled": bool(row["enabled"]),
                "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"]),
            })
        return result
    finally:
        connection.close()


def add_discovery_search(user: dict[str, object], phrase: str, location: str = "") -> int:
    checked_phrase = _search_text(phrase, "job field or search phrase", required=True)
    checked_location = _search_text(location, "location", required=False)
    cipher = _fernet(user, "discovery-search")
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if int(connection.execute("SELECT COUNT(*) FROM discovery_searches").fetchone()[0]) >= 50:
            raise ValueError("The job search limit is 50")
        now = _stamp()
        try:
            cursor = connection.execute(
                "INSERT INTO discovery_searches(query_hash,phrase_ciphertext,location_ciphertext,enabled,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (_search_query_hash(user, checked_phrase, checked_location),
                 cipher.encrypt(checked_phrase.encode("utf-8")),
                 cipher.encrypt(checked_location.encode("utf-8")), 1, now, now),
            )
        except sqlite3.IntegrityError:
            raise ValueError("This job search already exists") from None
        connection.commit()
        return int(cursor.lastrowid)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_discovery_search_enabled(user: dict[str, object], search_id: object,
                                 enabled: bool) -> None:
    selected_id = _source_id(search_id)
    if type(enabled) is not bool:
        raise ValueError("Invalid search setting")
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        cursor = connection.execute(
            "UPDATE discovery_searches SET enabled=?,updated_at=? WHERE search_id=?",
            (int(enabled), _stamp(), selected_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Job search not found")
        connection.commit()
    finally:
        connection.close()


def remove_discovery_search(user: dict[str, object], search_id: object) -> None:
    selected_id = _source_id(search_id)
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        cursor = connection.execute("DELETE FROM discovery_searches WHERE search_id=?", (selected_id,))
        if cursor.rowcount != 1:
            raise ValueError("Job search not found")
        connection.commit()
    finally:
        connection.close()


ADZUNA_COUNTRIES = {"us", "gb", "ca", "au"}


def adzuna_status(user: dict[str, object]) -> dict[str, object]:
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        row = connection.execute(
            "SELECT country,enabled,updated_at FROM discovery_integrations WHERE provider='adzuna'"
        ).fetchone()
        return ({"configured": True, "country": str(row["country"]),
                 "enabled": bool(row["enabled"]), "updated_at": str(row["updated_at"])}
                if row else {"configured": False, "country": "us", "enabled": False, "updated_at": ""})
    finally:
        connection.close()


def adzuna_credentials(user: dict[str, object]) -> dict[str, str] | None:
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        row = connection.execute(
            "SELECT credentials_ciphertext,country,enabled,updated_at FROM discovery_integrations "
            "WHERE provider='adzuna'"
        ).fetchone()
        if row is None or not row["enabled"]:
            return None
        try:
            payload = json.loads(_fernet(user, "discovery-integration").decrypt(
                row["credentials_ciphertext"]).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("Adzuna credentials could not be decrypted") from exc
        if type(payload) is not dict or set(payload) != {"app_id", "app_key"}:
            raise RuntimeError("Adzuna credentials are invalid")
        return {"app_id": str(payload["app_id"]), "app_key": str(payload["app_key"]),
                "country": str(row["country"]), "updated_at": str(row["updated_at"])}
    finally:
        connection.close()


def save_adzuna_credentials(user: dict[str, object], app_id: str, app_key: str,
                            country: str, *, rights_confirmed: bool) -> None:
    if rights_confirmed is not True:
        raise ValueError("Confirm your Adzuna API access rights before enabling searches")
    if country not in ADZUNA_COUNTRIES:
        raise ValueError("Choose a supported Adzuna country")
    credentials: dict[str, str] = {}
    for label, value in (("app_id", app_id), ("app_key", app_key)):
        if (type(value) is not str or not value.strip() or len(value.strip()) > 200 or
                any(ord(ch) < 33 or ord(ch) == 127 for ch in value.strip())):
            raise ValueError("Enter a valid Adzuna app ID and key")
        credentials[label] = value.strip()
    encrypted = _fernet(user, "discovery-integration").encrypt(
        json.dumps(credentials, separators=(",", ":")).encode("utf-8"))
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        connection.execute(
            "INSERT INTO discovery_integrations(provider,credentials_ciphertext,country,enabled,updated_at) "
            "VALUES('adzuna',?,?,1,?) ON CONFLICT(provider) DO UPDATE SET "
            "credentials_ciphertext=excluded.credentials_ciphertext,country=excluded.country,"
            "enabled=1,updated_at=excluded.updated_at",
            (encrypted, country, _stamp()),
        )
        connection.commit()
    finally:
        connection.close()


def set_adzuna_enabled(user: dict[str, object], enabled: bool) -> None:
    if type(enabled) is not bool:
        raise ValueError("Invalid Adzuna setting")
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        cursor = connection.execute(
            "UPDATE discovery_integrations SET enabled=?,updated_at=? WHERE provider='adzuna'",
            (int(enabled), _stamp()),
        )
        if cursor.rowcount != 1:
            raise ValueError("Configure Adzuna first")
        connection.commit()
    finally:
        connection.close()


def remove_adzuna_credentials(user: dict[str, object]) -> None:
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        connection.execute("DELETE FROM discovery_integrations WHERE provider='adzuna'")
        connection.commit()
    finally:
        connection.close()




def save_settings(user: dict[str, object], payload: dict[str, object]) -> dict[str, object]:
    allowed = {"discovery_paused", "application_paused", "autonomy_mode",
               "daily_application_limit", "browser_concurrency"}
    if set(payload) != allowed:
        raise ValueError("Invalid settings fields")
    if payload["autonomy_mode"] not in {"discovery_only", "prepare_for_review", "submit_eligible"}:
        raise ValueError("Invalid autonomy mode")
    try:
        daily = int(payload["daily_application_limit"])
        concurrency = int(payload["browser_concurrency"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Limits must be whole numbers") from exc
    if (daily != -1 and not 1 <= daily <= 100) or not 1 <= concurrency <= 8:
        raise ValueError("Limits are outside the allowed range")
    daily_unlimited = int(daily == -1)
    stored_daily = 5 if daily_unlimited else daily
    paused_discovery = int(bool(payload["discovery_paused"]))
    paused_application = int(bool(payload["application_paused"]))
    path = initialize_tenant(user)
    connection = application_store.connect(path)
    try:
        connection.execute(
            "UPDATE user_settings SET discovery_paused=?,application_paused=?,autonomy_mode=?,"
            "daily_application_limit=?,daily_application_unlimited=?,browser_concurrency=?,"
            "auto_apply_hosts='',updated_at=? WHERE singleton=1",
            (paused_discovery, paused_application, payload["autonomy_mode"], stored_daily,
             daily_unlimited, concurrency, _stamp()),
        )
        connection.commit()
        return get_settings(user)
    finally:
        connection.close()
