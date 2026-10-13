#!/usr/bin/env python3
"""Headed Chromium adapter over a loopback-only CDP endpoint.

Discovery uses only public-page inspection. The applier may fill and submit a
pre-approved form, and can keep its browser open for a human verification
handoff. VNC is available only when an application queue ID is supplied.
Browser profiles are private and persistent at
``<state_dir>/browser_profiles/<user_id>``.

The command-line interface reads its request JSON from stdin so a URL (which
may contain sensitive query parameters) does not appear in the process list.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import calendar
import fcntl
import hashlib
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


_MAX_URL_LENGTH = 4096
_MAX_STDIN_BYTES = 16 * 1024
_MAX_WS_FRAME = 8 * 1024 * 1024
_MAX_TEXT_CHARS = 16_000
_MAX_FIELDS = 120
_MAX_FORMS = 30
_USER_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_QUEUE_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_CDP_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_LIVE_HANDOFF: dict[str, Any] | None = None
_HANDOFF_LOCK = threading.RLock()
_HANDOFF_ACTIVE_NAME = "active.json"
_HANDOFF_RELEASE_NAME = "release.json"


class InspectionFailure(Exception):
    """Internal error with a stable, safe code and user-facing message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CDPFailure(Exception):
    pass


class CDPTimeout(CDPFailure):
    pass


def _handoff_directory(state_path: Path) -> Path:
    directory = state_path / "application_browser_handoff"
    if directory.is_symlink():
        raise InspectionFailure("invalid_state_dir", "The application handoff directory must not be a symlink.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise OSError("handoff state file must not be a symlink")
    temporary = path.with_name(path.name + f".{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _read_handoff_file(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        info = path.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _handoff_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _start_browser_handoff(
    state_path: Path,
    user_id: str,
    queue_id: str,
    display: str,
    auth_file: str,
    chromium_proc: subprocess.Popen[bytes],
    xvfb_proc: subprocess.Popen[bytes],
    lock_fd: int,
) -> bool:
    """Keep an application browser open for a human verification handoff."""
    global _LIVE_HANDOFF
    if not _QUEUE_ID_RE.fullmatch(queue_id):
        return False
    x11vnc = _available_binary(("x11vnc",))
    websockify = _available_binary(("websockify",))
    novnc_root = Path("/usr/share/novnc")
    if not x11vnc or not websockify or not novnc_root.is_dir():
        return False
    with _HANDOFF_LOCK:
        if _LIVE_HANDOFF is not None:
            return False
        handoff_dir = _handoff_directory(state_path)
        for stale_name in (_HANDOFF_ACTIVE_NAME, _HANDOFF_RELEASE_NAME):
            stale_path = handoff_dir / stale_name
            if stale_path.is_symlink():
                return False
            try:
                stale_path.unlink()
            except FileNotFoundError:
                pass
        password_path = handoff_dir / "vnc-password"
        if password_path.is_symlink():
            return False
        try:
            password_path.unlink()
        except FileNotFoundError:
            pass
        password = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789")
                           for _ in range(8))
        fd = os.open(password_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as output:
            output.write(password + "\n")
        environment = os.environ.copy()
        environment["DISPLAY"] = display
        environment["XAUTHORITY"] = auth_file
        vnc_proc: subprocess.Popen[bytes] | None = None
        proxy_proc: subprocess.Popen[bytes] | None = None
        try:
            vnc_proc = subprocess.Popen(
                [x11vnc, "-display", display, "-auth", auth_file, "-rfbport", "5900",
                 "-listen", "0.0.0.0", "-forever", "-shared", "-noxdamage", "-quiet",
                 "-passwdfile", f"rm:{password_path}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=environment, start_new_session=True,
            )
            time.sleep(0.25)
            if vnc_proc.poll() is not None:
                raise OSError("VNC server did not start")
            proxy_proc = subprocess.Popen(
                [websockify, "--web", str(novnc_root), "0.0.0.0:6080", "127.0.0.1:5900"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=environment, start_new_session=True,
            )
            time.sleep(0.25)
            if proxy_proc.poll() is not None:
                raise OSError("noVNC proxy did not start")
            now = _handoff_now()
            metadata = {
                "user_id": user_id,
                "queue_id": queue_id,
                "password": password,
                "started_at": now,
                "updated_at": now,
                "vnc_port": 5900,
                "novnc_port": 6080,
            }
            _write_private_json(handoff_dir / _HANDOFF_ACTIVE_NAME, metadata)
            _LIVE_HANDOFF = {
                "state_path": state_path,
                "handoff_dir": handoff_dir,
                "auth_dir": str(Path(auth_file).parent),
                "vnc_proc": vnc_proc,
                "proxy_proc": proxy_proc,
                "chromium_proc": chromium_proc,
                "xvfb_proc": xvfb_proc,
                "lock_fd": lock_fd,
                "metadata": metadata,
            }
            return True
        except (OSError, ValueError):
            _stop_process_group(proxy_proc)
            _stop_process_group(vnc_proc)
            try:
                password_path.unlink()
            except OSError:
                pass
            return False


def live_handoff_info(state_dir: str | os.PathLike[str], user_id: str,
                      queue_id: str) -> dict[str, str] | None:
    """Return connection details only for the authenticated owner and queue item."""
    try:
        user_id = _safe_user_id(user_id)
        if not isinstance(queue_id, str) or not _QUEUE_ID_RE.fullmatch(queue_id):
            return None
        state_path = Path(state_dir).expanduser().resolve(strict=False)
        metadata = _read_handoff_file(_handoff_directory(state_path) / _HANDOFF_ACTIVE_NAME)
        if not metadata or metadata.get("user_id") != user_id or metadata.get("queue_id") != queue_id:
            return None
        updated = time.strptime(str(metadata.get("updated_at") or ""), "%Y-%m-%dT%H:%M:%SZ")
        if time.time() - calendar.timegm(updated) > 45:
            return None
        password = metadata.get("password")
        if not isinstance(password, str) or not re.fullmatch(r"[A-Za-z0-9]{8}", password):
            return None
        return {"password": password, "vnc_address": "127.0.0.1:5900",
                "novnc_url": "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale"}
    except (InspectionFailure, OSError, ValueError, TypeError, OverflowError):
        return None


def request_handoff_release(state_dir: str | os.PathLike[str], user_id: str,
                            queue_id: str) -> bool:
    """Ask the applier process to close a matching live browser handoff."""
    info = live_handoff_info(state_dir, user_id, queue_id)
    if info is None:
        return False
    try:
        state_path = Path(state_dir).expanduser().resolve(strict=False)
        directory = _handoff_directory(state_path)
        _write_private_json(directory / _HANDOFF_RELEASE_NAME, {
            "user_id": user_id, "queue_id": queue_id, "requested_at": _handoff_now(),
        })
        return True
    except (InspectionFailure, OSError, ValueError):
        return False


def has_live_handoff() -> bool:
    with _HANDOFF_LOCK:
        return _LIVE_HANDOFF is not None


def refresh_live_handoff() -> bool:
    """Refresh the shared handoff heartbeat and report whether one is active."""
    with _HANDOFF_LOCK:
        if _LIVE_HANDOFF is None:
            return False
        metadata = _LIVE_HANDOFF["metadata"]
        metadata["updated_at"] = _handoff_now()
        try:
            _write_private_json(_LIVE_HANDOFF["handoff_dir"] / _HANDOFF_ACTIVE_NAME, metadata)
        except OSError:
            _stop_live_handoff()
            return False
        vnc_proc = _LIVE_HANDOFF.get("vnc_proc")
        proxy_proc = _LIVE_HANDOFF.get("proxy_proc")
        chromium_proc = _LIVE_HANDOFF.get("chromium_proc")
        if any(proc is None or proc.poll() is not None for proc in (vnc_proc, proxy_proc, chromium_proc)):
            _stop_live_handoff()
            return False
        return True


def process_handoff_commands(state_dir: str | os.PathLike[str]) -> bool:
    """Close the live applier browser when its owner resolves the handoff."""
    with _HANDOFF_LOCK:
        if _LIVE_HANDOFF is None:
            return False
        try:
            requested_state_path = Path(state_dir).expanduser().resolve(strict=False)
        except (TypeError, OSError):
            return False
        if requested_state_path != _LIVE_HANDOFF.get("state_path"):
            return False
        release_path = _LIVE_HANDOFF["handoff_dir"] / _HANDOFF_RELEASE_NAME
        command = _read_handoff_file(release_path)
        if not command:
            return False
        metadata = _LIVE_HANDOFF["metadata"]
        try:
            release_path.unlink()
        except OSError:
            pass
        if (command.get("user_id") != metadata.get("user_id") or
                command.get("queue_id") != metadata.get("queue_id")):
            return False
        _stop_live_handoff()
        return True


def _stop_live_handoff() -> None:
    global _LIVE_HANDOFF
    with _HANDOFF_LOCK:
        active = _LIVE_HANDOFF
        if active is None:
            return
        _LIVE_HANDOFF = None
        _stop_process_group(active.get("proxy_proc"))
        _stop_process_group(active.get("vnc_proc"))
        _stop_process_group(active.get("chromium_proc"))
        _stop_process_group(active.get("xvfb_proc"))
        if isinstance(active.get("auth_dir"), str):
            shutil.rmtree(active["auth_dir"], ignore_errors=True)
        lock_fd = active.get("lock_fd")
        if isinstance(lock_fd, int):
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass
        handoff_dir = active.get("handoff_dir")
        if isinstance(handoff_dir, Path) and not handoff_dir.is_symlink():
            shutil.rmtree(handoff_dir, ignore_errors=True)


atexit.register(_stop_live_handoff)


def _failure(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": code,
        "status": "error",
        "human_review_required": False,
        "message": message,
    }


def _canonical_host(host: str) -> tuple[str, bool]:
    """Return a lowercase IDNA/IP host and whether it is an IP literal."""
    if not host or "%" in host:
        raise InspectionFailure("invalid_url", "The URL host is invalid.")
    try:
        address = ipaddress.ip_address(host)
        return address.compressed.lower(), True
    except ValueError:
        pass
    try:
        encoded = host.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise InspectionFailure("invalid_url", "The URL host is invalid.") from None
    if not encoded or len(encoded) > 253:
        raise InspectionFailure("invalid_url", "The URL host is invalid.")
    labels = encoded.split(".")
    if any(
        not label
        or len(label) > 63
        or label[0] == "-"
        or label[-1] == "-"
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        raise InspectionFailure("invalid_url", "The URL host is invalid.")
    return encoded, False


def _validate_url(url: Any) -> tuple[str, str, str]:
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_LENGTH:
        raise InspectionFailure("invalid_url", "Provide one valid HTTPS URL.")
    if any(ord(char) < 32 or char.isspace() for char in url):
        raise InspectionFailure("invalid_url", "Provide one valid HTTPS URL.")
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise InspectionFailure("invalid_url", "Provide one valid HTTPS URL.") from None
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise InspectionFailure("invalid_url", "Only public HTTPS URLs on port 443 are allowed.")
    canonical, is_ip = _canonical_host(hostname)
    # Explicitly reject trailing-dot or unusual authority forms that can be
    # interpreted differently by proxies and browser URL parsers.
    if hostname.endswith(".") or "\\" in parsed.netloc:
        raise InspectionFailure("invalid_url", "The URL authority is invalid.")
    display_host = f"[{canonical}]" if is_ip and ":" in canonical else canonical
    normalized_netloc = display_host
    if port == 443 and ":" in canonical and is_ip:
        normalized_netloc = f"[{canonical}]"
    normalized = urllib.parse.urlunsplit(
        ("https", normalized_netloc, parsed.path or "/", parsed.query, "")
    )
    safe_url = urllib.parse.urlunsplit(("https", display_host, parsed.path or "/", "", ""))
    if not parsed.path:
        normalized = urllib.parse.urlunsplit(("https", parsed.netloc, "/", parsed.query, ""))
    return normalized, canonical, safe_url


def _is_public_ip(raw: str) -> bool:
    try:
        addr = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_global


def _resolve_public_host(host: str, is_ip_literal: bool, timeout: float = 5.0) -> str:
    if is_ip_literal:
        if not _is_public_ip(host):
            raise InspectionFailure("non_public_host", "The URL must use a public host.")
        return host

    result_queue: queue.Queue[tuple[list[str] | None, BaseException | None]] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            values = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            result_queue.put(([item[4][0] for item in values], None))
        except BaseException as exc:  # returned only to the bounded caller
            result_queue.put((None, exc))

    thread = threading.Thread(target=resolve, name="jobbot-dns-check", daemon=True)
    thread.start()
    try:
        addresses, error = result_queue.get(timeout=timeout)
    except queue.Empty:
        raise InspectionFailure("host_resolution_timeout", "The public host check timed out.") from None
    if error is not None or not addresses:
        raise InspectionFailure("host_resolution_failed", "The URL host could not be resolved.")
    # Reject a host if any returned address is local, private, reserved, or
    # otherwise non-public; accepting only one public answer is insufficient.
    if any(not _is_public_ip(address) for address in addresses):
        raise InspectionFailure("non_public_host", "The URL must resolve only to public addresses.")
    # Pin Chromium to one of the validated DNS answers to prevent a second
    # lookup from changing a public hostname to a private address.
    unique = list(dict.fromkeys(addresses))
    unique.sort(key=lambda address: (ipaddress.ip_address(address).version != 4, address))
    return unique[0]


def _safe_location(url: Any, expected_host: str) -> tuple[str, bool]:
    """Redact query/fragment data while preserving a useful final page URL."""
    if not isinstance(url, str) or len(url) > _MAX_URL_LENGTH:
        return "", False
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        if not host:
            return "", bool(parsed.query or parsed.fragment)
        if parsed.port not in (None, 443):
            return "", bool(parsed.query or parsed.fragment)
        canonical, _ = _canonical_host(host)
        if parsed.scheme.lower() != "https" or canonical != expected_host:
            return "", bool(parsed.query or parsed.fragment)
        if ":" in canonical:
            shown_host = f"[{canonical}]"
        else:
            shown_host = canonical
        safe = urllib.parse.urlunsplit(("https", shown_host, parsed.path or "/", "", ""))
        return safe, bool(parsed.query or parsed.fragment)
    except (ValueError, InspectionFailure):
        return "", False


def _url_policy_allows(url: str, expected_host: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() != "https" or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.port not in (None, 443) or not parsed.hostname:
            return False
        canonical, _ = _canonical_host(parsed.hostname)
        return canonical == expected_host
    except (ValueError, InspectionFailure):
        return False


def _safe_user_id(user_id: Any) -> str:
    if not isinstance(user_id, str) or not _USER_ID_RE.fullmatch(user_id):
        raise InspectionFailure("invalid_user_id", "The user ID must be a short alphanumeric identifier.")
    return user_id


def _profile_lock(state_dir: Path, user_id: str) -> tuple[int, Path]:
    """Create a private user profile and acquire a nonblocking per-user lock."""
    old_umask = os.umask(0o077)
    try:
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        profiles_root = state_dir / "browser_profiles"
        if profiles_root.is_symlink():
            raise InspectionFailure("invalid_state_dir", "The browser profile directory must not be a symlink.")
        profiles_root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(profiles_root, 0o700)
        profile = profiles_root / user_id
        if profile.is_symlink():
            raise InspectionFailure("invalid_state_dir", "The user browser profile must not be a symlink.")
        profile.mkdir(mode=0o700, exist_ok=True)
        stat_result = profile.stat()
        if not profile.is_dir() or stat_result.st_uid != os.geteuid():
            raise InspectionFailure("invalid_state_dir", "The user browser profile is not privately owned.")
        os.chmod(profile, 0o700)
        lock_path = profiles_root / f".{user_id}.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock_fd = os.open(lock_path, flags, 0o600)
        os.fchmod(lock_fd, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            raise InspectionFailure("profile_in_use", "This user's browser profile is already in use.") from None
        return lock_fd, profile
    except InspectionFailure:
        raise
    except OSError:
        raise InspectionFailure("invalid_state_dir", "The private browser profile could not be prepared.") from None
    finally:
        os.umask(old_umask)


def _chromium_singletons_are_stale(profile: Path) -> None:
    """Remove only stale Chromium singleton links while the profile lock is held.

    Chromium records the hostname and PID of its owner in ``SingletonLock``.
    Persistent profiles mounted into a replacement container otherwise look
    locked forever because each container has a different hostname. Active
    browser processes started here inherit the per-user lock descriptor, so
    another inspection cannot recover a profile while that browser is alive.
    """
    names = ("SingletonLock", "SingletonSocket", "SingletonCookie")
    artifacts: dict[str, str] = {}
    for name in names:
        path = profile / name
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise InspectionFailure("profile_in_use", "The browser profile lock could not be checked.") from None
        if not stat.S_ISLNK(metadata.st_mode):
            raise InspectionFailure("profile_in_use", "The browser profile contains an unsafe singleton marker.")
        try:
            artifacts[name] = os.readlink(path)
        except OSError:
            raise InspectionFailure("profile_in_use", "The browser profile lock could not be checked.") from None

    if not artifacts:
        return

    owner = artifacts.get("SingletonLock")
    if owner is not None:
        try:
            host, pid_text = owner.rsplit("-", 1)
        except ValueError:
            raise InspectionFailure("profile_in_use", "The browser profile lock is malformed.") from None
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", host)
            or not pid_text.isdecimal()
            or len(pid_text) > 10
            or int(pid_text) < 1
        ):
            raise InspectionFailure("profile_in_use", "The browser profile lock is malformed.")
        if host == socket.gethostname():
            pid = int(pid_text)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError:
                raise InspectionFailure("profile_in_use", "The browser profile is still in use.") from None
            except OSError:
                raise InspectionFailure("profile_in_use", "The browser profile owner could not be checked.") from None
            else:
                proc_dir = Path("/proc") / str(pid)
                try:
                    stat_fields = (proc_dir / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
                    if stat_fields and stat_fields[0] in {"Z", "X"}:
                        process_args: list[bytes] = []
                    else:
                        process_args = (proc_dir / "cmdline").read_bytes().split(b"\0")
                except (OSError, IndexError):
                    raise InspectionFailure("profile_in_use", "The browser profile owner could not be checked.") from None
                expected_arg = b"--user-data-dir=" + os.fsencode(str(profile))
                uses_profile = expected_arg in process_args or any(
                    arg == b"--user-data-dir"
                    and index + 1 < len(process_args)
                    and process_args[index + 1] == os.fsencode(str(profile))
                    for index, arg in enumerate(process_args)
                )
                if uses_profile:
                    raise InspectionFailure("profile_in_use", "The browser profile is still in use.")

    socket_target = artifacts.get("SingletonSocket")
    if socket_target is not None:
        socket_path = Path(socket_target)
        if (
            not socket_path.is_absolute()
            or socket_path.name != "SingletonSocket"
            or not socket_path.parent.name.startswith("org.chromium.Chromium.")
        ):
            raise InspectionFailure("profile_in_use", "The browser profile socket marker is malformed.")
    cookie_target = artifacts.get("SingletonCookie")
    if cookie_target is not None and (not cookie_target.isdecimal() or len(cookie_target) > 32):
        raise InspectionFailure("profile_in_use", "The browser profile cookie marker is malformed.")

    # The per-user flock excludes every adapter instance using this profile.
    # Unlink only Chromium's three symlinks; leave their targets and all real
    # browser profile data untouched.
    for name in artifacts:
        try:
            (profile / name).unlink()
        except FileNotFoundError:
            continue
        except OSError:
            raise InspectionFailure("profile_in_use", "The stale browser profile lock could not be cleared.") from None


def _available_binary(names: tuple[str, ...]) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _start_xvfb(timeout_seconds: float) -> tuple[subprocess.Popen[bytes], str, str]:
    xvfb = _available_binary(("Xvfb",))
    xauth = _available_binary(("xauth",))
    if not xvfb or not xauth:
        raise InspectionFailure("missing_xvfb", "Install the Xvfb and xauth system packages.")
    auth_dir = tempfile.mkdtemp(prefix="jobbot-xauth-")
    os.chmod(auth_dir, 0o700)
    auth_file = os.path.join(auth_dir, "Xauthority")
    fd = os.open(auth_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    deadline = time.monotonic() + min(max(timeout_seconds, 1.0), 8.0)
    tried: set[int] = set()
    while time.monotonic() < deadline and len(tried) < 8:
        display_number = secrets.randbelow(450) + 100
        if display_number in tried:
            continue
        tried.add(display_number)
        if os.path.exists(f"/tmp/.X11-unix/X{display_number}") or os.path.exists(
            f"/tmp/.X{display_number}-lock"
        ):
            continue
        cookie = secrets.token_hex(16)
        try:
            auth = subprocess.run(
                [xauth, "-q", "-f", auth_file],
                input=f"add :{display_number} MIT-MAGIC-COOKIE-1 {cookie}\nexit\n",
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            shutil.rmtree(auth_dir, ignore_errors=True)
            raise InspectionFailure("xvfb_start_failed", "The private X display could not be initialized.") from None
        if auth.returncode != 0:
            shutil.rmtree(auth_dir, ignore_errors=True)
            raise InspectionFailure("xvfb_start_failed", "The private X display could not be initialized.")
        env = os.environ.copy()
        env["DISPLAY"] = f":{display_number}"
        env["XAUTHORITY"] = auth_file
        try:
            proc = subprocess.Popen(
                [
                    xvfb,
                    f":{display_number}",
                    "-screen",
                    "0",
                    "1280x900x24",
                    "-nolisten",
                    "tcp",
                    "-auth",
                    auth_file,
                    "-noreset",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        except OSError:
            shutil.rmtree(auth_dir, ignore_errors=True)
            raise InspectionFailure("xvfb_start_failed", "The private X display could not be started.") from None
        time.sleep(0.12)
        if proc.poll() is None:
            return proc, env["DISPLAY"], auth_dir
        _stop_process_group(proc)
    shutil.rmtree(auth_dir, ignore_errors=True)
    raise InspectionFailure("xvfb_start_failed", "A private X display could not be started in time.")


def _stop_process_group(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass
    # Chromium has a multi-process tree. Kill any remaining children in its
    # private process group after the graceful window, even if the parent left.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def _read_json_endpoint(port: int, path: str, timeout: float = 2.0, method: str = "GET") -> Any:
    if not path.startswith("/") or "\r" in path or "\n" in path:
        raise CDPFailure("invalid endpoint")
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.geturl().split("/", 3)[:3] != ["http:", "", "127.0.0.1:{0}".format(port)]:
                raise CDPFailure("unexpected endpoint redirect")
            raw = response.read(1024 * 1024 + 1)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise CDPFailure("endpoint unavailable") from exc
    if len(raw) > 1024 * 1024:
        raise CDPFailure("endpoint response too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CDPFailure("invalid endpoint response") from exc


def _validate_websocket_url(url: Any, port: int, expected_path_prefix: str) -> tuple[str, int, str]:
    if not isinstance(url, str):
        raise CDPFailure("missing websocket endpoint")
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        endpoint_port = parsed.port
    except ValueError as exc:
        raise CDPFailure("invalid websocket endpoint") from exc
    if (
        parsed.scheme != "ws"
        or host != "127.0.0.1"
        or endpoint_port != port
        or not parsed.path.startswith(expected_path_prefix)
        or parsed.query
        or parsed.fragment
    ):
        raise CDPFailure("websocket endpoint is not loopback-only")
    return "127.0.0.1", port, parsed.path


class CDPConnection:
    def __init__(self, host: str, port: int, path: str, timeout: float = 4.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._responses: dict[int, dict[str, Any]] = {}
        self._discard_response_ids: set[int] = set()
        self._next_id = 0
        self._event_handler: Callable[[dict[str, Any]], None] | None = None
        self._closed = False
        self._handshake(path, host, port)

    def _handshake(self, path: str, host: str, port: int) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.sock.sendall(request)
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPFailure("websocket handshake closed")
            data.extend(chunk)
            if len(data) > 32 * 1024:
                raise CDPFailure("websocket handshake too large")
        head, extra = bytes(data).split(b"\r\n\r\n", 1)
        if extra:
            # A server should not send frame bytes before completing upgrade.
            raise CDPFailure("unexpected websocket handshake data")
        try:
            lines = head.decode("iso-8859-1").split("\r\n")
        except UnicodeDecodeError as exc:
            raise CDPFailure("invalid websocket handshake") from exc
        if not lines or not re.match(r"^HTTP/1\.[01] 101(?:\s|$)", lines[0]):
            raise CDPFailure("websocket upgrade rejected")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + _CDP_ACCEPT_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise CDPFailure("websocket handshake validation failed")

    def set_event_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._event_handler = handler

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise CDPFailure("websocket closed")
        if len(payload) > _MAX_WS_FRAME:
            raise CDPFailure("websocket message too large")
        first = 0x80 | (opcode & 0x0F)
        mask = secrets.token_bytes(4)
        length = len(payload)
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length < (1 << 16):
            header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
        masked = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _recv_exact(self, count: int, deadline: float) -> bytes:
        parts = bytearray()
        while len(parts) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CDPTimeout("websocket read timed out")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(count - len(parts))
            except socket.timeout as exc:
                raise CDPTimeout("websocket read timed out") from exc
            if not chunk:
                raise CDPFailure("websocket closed")
            parts.extend(chunk)
        return bytes(parts)

    def _recv_frame(self, deadline: float) -> tuple[int, bytes, bool]:
        first, second = self._recv_exact(2, deadline)
        if first & 0x70:
            raise CDPFailure("unsupported websocket frame")
        opcode = first & 0x0F
        final = bool(first & 0x80)
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2, deadline))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8, deadline))[0]
        if length > _MAX_WS_FRAME:
            raise CDPFailure("websocket frame too large")
        mask = self._recv_exact(4, deadline) if masked else b""
        payload = self._recv_exact(length, deadline) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
        if opcode in (0x8, 0x9, 0xA) and not final:
            raise CDPFailure("invalid websocket control frame")
        return opcode, payload, final

    def _recv_message(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        fragments = bytearray()
        message_opcode: int | None = None
        while True:
            opcode, payload, final = self._recv_frame(deadline)
            if opcode == 0x8:
                self._closed = True
                raise CDPFailure("websocket closed by peer")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                if message_opcode is not None:
                    raise CDPFailure("nested websocket message")
                message_opcode = opcode
                fragments.extend(payload)
            elif opcode == 0x0 and message_opcode is not None:
                fragments.extend(payload)
            else:
                raise CDPFailure("invalid websocket opcode")
            if len(fragments) > _MAX_WS_FRAME:
                raise CDPFailure("websocket message too large")
            if final and message_opcode in (0x1, 0x2):
                break
        if message_opcode != 0x1:
            raise CDPFailure("non-text CDP message")
        try:
            result = json.loads(fragments.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CDPFailure("invalid CDP message") from exc
        if not isinstance(result, dict):
            raise CDPFailure("invalid CDP message")
        return result

    def _read_message(self, timeout: float) -> dict[str, Any]:
        message = self._recv_message(timeout)
        if isinstance(message.get("id"), int):
            response_id = message["id"]
            if response_id in self._discard_response_ids:
                self._discard_response_ids.discard(response_id)
            else:
                self._responses[response_id] = message
        elif isinstance(message.get("method"), str) and self._event_handler:
            self._event_handler(message)
        return message

    def _send_command(self, method: str, params: dict[str, Any] | None = None) -> int:
        self._next_id += 1
        command_id = self._next_id
        payload: dict[str, Any] = {"id": command_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._send_frame(0x1, json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        return command_id

    def send_no_wait(self, method: str, params: dict[str, Any] | None = None) -> int:
        command_id = self._send_command(method, params)
        self._discard_response_ids.add(command_id)
        return command_id

    def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        command_id = self._send_command(method, params)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self._responses.pop(command_id, None)
            if response is not None:
                if "error" in response:
                    raise CDPFailure("CDP command failed")
                result = response.get("result", {})
                return result if isinstance(result, dict) else {}
            try:
                self._read_message(max(0.01, deadline - time.monotonic()))
            except CDPTimeout:
                break
        raise CDPTimeout("CDP command timed out")

    def pump_for(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            try:
                self._read_message(min(0.15, deadline - time.monotonic()))
            except CDPTimeout:
                continue

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._send_frame(0x8, b"")
        except (OSError, CDPFailure):
            pass
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass


def _wait_for_browser(profile: Path, chromium_proc: subprocess.Popen[bytes], timeout: float) -> tuple[int, dict[str, Any]]:
    active_port = profile / "DevToolsActivePort"
    deadline = time.monotonic() + min(max(timeout, 1.0), 20.0)
    while time.monotonic() < deadline:
        if chromium_proc.poll() is not None:
            raise InspectionFailure("browser_start_failed", "Chromium exited during startup.")
        try:
            content = active_port.read_text(encoding="ascii").splitlines()
            if len(content) < 2 or not content[0].isdigit():
                raise ValueError("bad port file")
            port = int(content[0])
            if not 1 <= port <= 65535 or not content[1].startswith("/devtools/browser/"):
                raise ValueError("bad port file")
            version = _read_json_endpoint(port, "/json/version", timeout=1.0)
            if not isinstance(version, dict) or not isinstance(version.get("Browser"), str):
                raise ValueError("bad version")
            _validate_websocket_url(version.get("webSocketDebuggerUrl"), port, "/devtools/browser/")
            return port, version
        except InspectionFailure:
            raise
        except (OSError, ValueError, CDPFailure, urllib.error.URLError):
            time.sleep(0.1)
    raise InspectionFailure("cdp_unavailable", "Chromium's loopback CDP endpoint did not become ready.")


def _get_page_target(port: int) -> tuple[dict[str, Any], bool]:
    try:
        targets = _read_json_endpoint(port, "/json/list", timeout=2.0)
    except CDPFailure:
        raise InspectionFailure("cdp_unavailable", "Chromium's loopback CDP endpoint is unavailable.") from None
    if not isinstance(targets, list):
        raise InspectionFailure("cdp_unavailable", "Chromium returned an invalid page list.")
    pages = [item for item in targets if isinstance(item, dict) and item.get("type") == "page"]
    # Do not attach to a restored tab. It may represent a different task or URL.
    if any(item.get("url") != "about:blank" for item in pages):
        raise InspectionFailure("unexpected_browser_target", "The browser profile opened an unexpected page.")
    if len(pages) > 1:
        raise InspectionFailure("unexpected_browser_target", "The browser profile opened more than one page.")
    if pages:
        target = pages[0]
        created = False
    else:
        try:
            target = _read_json_endpoint(port, "/json/new?about:blank", timeout=2.0, method="PUT")
        except CDPFailure:
            raise InspectionFailure("cdp_unavailable", "Chromium could not create a blank inspection page.") from None
        if not isinstance(target, dict) or target.get("type") != "page" or target.get("url") != "about:blank":
            raise InspectionFailure("unexpected_browser_target", "Chromium created an unexpected page.")
        created = True
    _validate_websocket_url(target.get("webSocketDebuggerUrl"), port, "/devtools/page/")
    return target, created


_READ_PAGE_EXPRESSION = r"""(() => {
  const cut = (value, limit) => String(value || '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, ' ').trim().slice(0, limit);
  const visible = (el) => {
    try {
      const style = getComputedStyle(el);
      return style.display !== 'none' && style.visibility !== 'hidden' && style.visibility !== 'collapse' && el.getClientRects().length > 0;
    } catch (_) { return false; }
  };
  const labelText = (el) => {
    const parts = [];
    try { if (el.labels) for (const label of el.labels) parts.push(label.innerText || label.textContent || ''); } catch (_) {}
    if (!parts.length) {
      try { if (el.getAttribute('aria-label')) parts.push(el.getAttribute('aria-label')); } catch (_) {}
    }
    if (!parts.length) {
      try {
        const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
        for (const id of ids) { const label = document.getElementById(id); if (label) parts.push(label.innerText || label.textContent || ''); }
      } catch (_) {}
    }
    return cut(parts.join(' ').replace(/\s+/g, ' '), 240);
  };
  const fields = [];
  try {
    for (const [domIndex, el] of Array.from(document.querySelectorAll('input, select, textarea')).slice(0, 120).entries()) {
      let type = el.tagName.toLowerCase();
      if (el.tagName === 'INPUT') type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'hidden') continue;
      const formIndex = el.form ? Array.prototype.indexOf.call(document.forms, el.form) : -1;
      let optionsCount = null;
      let options = [];
      if (el.tagName === 'SELECT') {
        optionsCount = el.options ? Math.min(el.options.length, 500) : 0;
        options = Array.from(el.options || []).slice(0, 100).map(option => ({
          value: cut(option.value, 500), text: cut(option.innerText || option.textContent || '', 300)
        }));
      }
      fields.push({
        dom_index: domIndex,
        form_index: formIndex,
        type: cut(type, 32),
        name: cut(el.getAttribute('name'), 120),
        id: cut(el.id, 120),
        label: labelText(el),
        placeholder: cut(el.getAttribute('placeholder'), 180),
        autocomplete: cut(el.getAttribute('autocomplete'), 80),
        required: !!el.required || el.getAttribute('aria-required') === 'true' ||
          /[✱*]/.test(labelText(el)),
        disabled: !!el.disabled,
        read_only: !!el.readOnly,
        multiple: !!el.multiple,
        accept: cut(el.getAttribute('accept'), 300),
        value_present: !!String(el.value || '').trim(),
        options,
        visible: visible(el),
        options_count: optionsCount
      });
      if (fields.length >= 120) break;
    }
  } catch (_) {}
  const forms = [];
  try {
    for (const [index, form] of Array.from(document.forms).slice(0, 30).entries()) {
      forms.push({
        index,
        method: cut(form.getAttribute('method') || 'get', 16).toLowerCase(),
        field_count: fields.filter((field) => field.form_index === index).length
      });
    }
  } catch (_) {}
  const buttons = [];
  try {
    for (const [domIndex, el] of Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"]')).slice(0, 80).entries()) {
      buttons.push({
        dom_index: domIndex,
        text: cut(el.innerText || el.value || el.getAttribute('aria-label') || '', 240),
        type: cut(el.getAttribute('type') || '', 32).toLowerCase(),
        disabled: !!el.disabled,
        visible: visible(el)
      });
    }
  } catch (_) {}
  const applyLinks = [];
  try {
    for (const el of Array.from(document.querySelectorAll('a[href]')).slice(0, 250)) {
      if (!visible(el)) continue;
      const label = cut(el.innerText || el.getAttribute('aria-label') || '', 160).replace(/\s+/g, ' ').trim();
      if (!/^(apply|apply now|apply for (this|the) (job|position)|start application)$/i.test(label)) continue;
      const href = cut(el.href || '', 4096);
      if (href && applyLinks.length < 12) applyLinks.push({href, label});
    }
  } catch (_) {}
  const bodyText = document.body ? (document.body.innerText || '') : '';
  const captchaNodes = (() => {
    try {
      const candidates = document.querySelectorAll('iframe[src*="captcha" i], iframe[src*="challenge" i], [id*="captcha" i], [class*="captcha" i], [data-sitekey]');
      const challengeCopy = [
        "i'm not a robot", "im not a robot", "select all images", "select all squares",
        "complete the captcha", "solve the captcha", "enter the captcha",
        "captcha challenge", "captcha verification", "verify you are human",
        "verify that you are human", "prove you are human"
      ];
      return Array.from(candidates).some(el => {
        // Google keeps this passive branding badge visible even without a challenge.
        if (!visible(el) || el.closest('.grecaptcha-badge, .grecaptcha-logo')) return false;
        const rect = el.getBoundingClientRect();
        if (el.hasAttribute('data-sitekey') && rect.width >= 100 && rect.height >= 30) return true;
        if (el.tagName === 'IFRAME') {
          const src = String(el.src || '').toLowerCase();
          return /captcha|challenge|turnstile/.test(src);
        }
        const ownText = String(el.innerText || el.textContent || '').toLowerCase();
        if (challengeCopy.some(phrase => ownText.includes(phrase))) return true;
        return !!el.querySelector('input[type="checkbox"], [role="checkbox"]');
      });
    }
    catch (_) { return false; }
  })();
  return {
    href: cut(location.href, 4096),
    title: cut(document.title, 300),
    visible_text: cut(bodyText, 16000),
    fields,
    forms,
    buttons,
    apply_links: applyLinks,
    captcha_nodes: captchaNodes
  };
})()"""


def _create_readonly_world(conn: CDPConnection, main_frame_id: str, timeout: float) -> int:
    try:
        world = conn.command(
            "Page.createIsolatedWorld",
            {"frameId": main_frame_id, "worldName": "jobbot-readonly-inspection", "grantUniveralAccess": False},
            timeout=min(5.0, timeout),
        )
        context_id = world.get("executionContextId")
        if not isinstance(context_id, int):
            raise CDPFailure("isolated world unavailable")
        return context_id
    except CDPFailure as exc:
        raise InspectionFailure("page_inspection_failed", "The page could not be inspected safely.") from exc


def _read_page_data(conn: CDPConnection, context_id: int, timeout: float) -> dict[str, Any]:
    try:
        evaluation = conn.command(
            "Runtime.evaluate",
            {
                "expression": _READ_PAGE_EXPRESSION,
                "contextId": context_id,
                "returnByValue": True,
                "awaitPromise": False,
                "silent": True,
                "timeout": 2500,
            },
            timeout=min(5.0, timeout),
        )
    except CDPFailure as exc:
        raise InspectionFailure("page_inspection_failed", "The page could not be inspected safely.") from exc
    if evaluation.get("exceptionDetails"):
        raise InspectionFailure("page_inspection_failed", "The page could not be inspected safely.")
    result = evaluation.get("result", {}).get("value")
    if not isinstance(result, dict):
        raise InspectionFailure("page_inspection_failed", "The page returned no safe inspection data.")
    return result


def _extract_page_data(conn: CDPConnection, main_frame_id: str, timeout: float) -> dict[str, Any]:
    context_id = _create_readonly_world(conn, main_frame_id, timeout)
    return _read_page_data(conn, context_id, timeout)


def _page_data_signature(data: dict[str, Any]) -> str:
    """Fingerprint only the bounded, value-free data used by inspection."""
    bounded = {
        "href": data.get("href"),
        "title": data.get("title"),
        "visible_text": data.get("visible_text"),
        "fields": data.get("fields"),
        "forms": data.get("forms"),
        "buttons": data.get("buttons"),
        "apply_links": data.get("apply_links"),
        "captcha_nodes": data.get("captcha_nodes"),
    }
    encoded = json.dumps(bounded, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _wait_for_page_render(
    conn: CDPConnection,
    main_frame_id: str,
    timeout: float,
    *,
    maximum_wait: float = 8.0,
    stable_for: float = 1.4,
) -> dict[str, Any]:
    """Wait briefly for GET-backed client rendering, using read-only snapshots.

    This does not interact with controls. A hard deadline keeps pages with
    continuous animation or polling bounded, while repeated DOM snapshots let
    a conventional SPA finish rendering after the top-level load event.
    """
    deadline = time.monotonic() + max(0.1, min(maximum_wait, timeout))
    context_id = _create_readonly_world(conn, main_frame_id, timeout)
    last_data: dict[str, Any] | None = None
    last_signature = ""
    stable_since = 0.0
    while time.monotonic() < deadline:
        remaining = max(0.1, min(4.0, deadline - time.monotonic()))
        data = _read_page_data(conn, context_id, remaining)
        signature = _page_data_signature(data)
        now = time.monotonic()
        if signature != last_signature:
            last_signature = signature
            stable_since = now
        elif stable_since and now - stable_since >= stable_for:
            return data
        last_data = data
        if now < deadline:
            time.sleep(min(0.35, deadline - now))
    if last_data is None:
        raise InspectionFailure("page_inspection_failed", "The page could not be inspected safely.")
    return last_data


def _detect_handoff(data: dict[str, Any], blocked: set[str]) -> list[str]:
    text = str(data.get("visible_text", ""))[:_MAX_TEXT_CHARS].lower()
    title = str(data.get("title", ""))[:300].lower()
    href = str(data.get("href", ""))[:_MAX_URL_LENGTH].lower()
    signals: list[str] = []
    captcha_phrases = (
        "complete the captcha",
        "solve the captcha",
        "enter the captcha",
        "captcha challenge",
        "captcha verification",
        "complete the recaptcha",
        "solve the recaptcha",
        "verify you are human",
        "verify that you are human",
        "prove you are human",
        "checking your browser",
        "security verification",
        "complete the security check",
        "unusual traffic",
        "robot or human",
    )
    if data.get("captcha_nodes") or any(term in text or term in title for term in captcha_phrases):
        signals.append("captcha_or_verification")
    block_phrases = (
        "access denied",
        "request blocked",
        "automated requests are not allowed",
        "automated access is not allowed",
        "your request has been blocked",
        "temporarily blocked",
    )
    if any(term in text or term in title for term in block_phrases):
        signals.append("site_block")
    fields = data.get("fields")
    has_password = isinstance(fields, list) and any(
        isinstance(field, dict) and field.get("type") == "password" for field in fields
    )
    login_path = bool(re.search(r"/(?:login|log-in|signin|sign-in|auth)(?:/|\?|$)", href))
    login_phrases = (
        "sign in to continue",
        "log in to continue",
        "please sign in",
        "please log in",
        "create an account to continue",
    )
    if has_password or login_path or any(term in text or term in title for term in login_phrases):
        signals.append("login_or_authentication")
    if "cross_host_redirect_blocked" in blocked or "unexpected_navigation_blocked" in blocked:
        signals.append("unexpected_navigation")
    # A background POST blocked during read-only inspection is common for
    # analytics or hydration. It is not, by itself, a human challenge. The
    # network gate still blocks every such request until an approved click.
    return list(dict.fromkeys(signals))


def _needs_human_verification(signals: list[str]) -> bool:
    return "site_block" not in signals and any(
        signal in {"captcha_or_verification", "login_or_authentication"}
        for signal in signals
    )


def _make_result(
    *,
    requested_url: str,
    final_url: str,
    query_redacted: bool,
    title: str,
    visible_text: str,
    forms: list[dict[str, Any]],
    buttons: list[dict[str, Any]] | None = None,
    form_signature: str = "",
    challenge_signals: list[str],
    navigation_error_code: str | None = None,
) -> dict[str, Any]:
    human_review = bool(challenge_signals)
    status = "human_review_required" if human_review else "inspected"
    # Login and challenge screens can display account-specific values. Keep
    # those out of worker/model results; the signal is enough for handoff.
    sensitive_screen = any(
        signal in challenge_signals
        for signal in ("captcha_or_verification", "site_block", "login_or_authentication")
    )
    result: dict[str, Any] = {
        "ok": True,
        "error_code": navigation_error_code,
        "status": status,
        "human_review_required": human_review,
        "requested_url": requested_url,
        "final_url": final_url,
        "final_url_query_redacted": query_redacted,
        "title": title[:300],
        "visible_text": "" if sensitive_screen else visible_text[:_MAX_TEXT_CHARS],
        "forms": forms[:_MAX_FORMS],
        "buttons": (buttons or [])[:80],
        "form_signature": form_signature,
        "challenge_detected": any(
            signal in challenge_signals
            for signal in ("captcha_or_verification", "login_or_authentication", "site_block")
        ),
        "challenge_signals": challenge_signals,
    }
    return result


def _form_signature(forms: list[dict[str, Any]], buttons: list[dict[str, Any]]) -> str:
    signature_data = {
        "forms": [
            {
                "index": form.get("index"),
                "method": form.get("method"),
                "fields": [
                    {key: field.get(key) for key in (
                        "dom_index", "form_index", "type", "name", "id", "label",
                        "placeholder", "autocomplete", "required", "disabled",
                        "read_only", "multiple", "accept", "options",
                    )}
                    for field in form.get("fields", []) if isinstance(field, dict)
                ],
            }
            for form in forms
        ],
        "buttons": [
            {key: button.get(key) for key in ("dom_index", "text", "type", "disabled", "visible")}
            for button in buttons if isinstance(button, dict)
        ],
    }
    encoded = json.dumps(signature_data, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _execute_submit_plan(
    conn: CDPConnection,
    frame_id: str,
    plan: dict[str, Any],
    resume_path: str,
    state_path: Path,
    state: dict[str, Any],
    canonical_host: str,
    deadline: float,
    before_submit: Callable[[], object] | None,
) -> dict[str, str]:
    raw_fields = plan.get("fields")
    if not isinstance(raw_fields, list) or len(raw_fields) > 80:
        return {"status": "needs_review", "reason": "manual_application_required"}
    safe_fields: list[dict[str, Any]] = []
    total_chars = 0
    for item in raw_fields:
        if (not isinstance(item, dict) or set(item) != {"dom_index", "value"} or
                isinstance(item["dom_index"], bool) or not isinstance(item["dom_index"], int) or
                not 0 <= item["dom_index"] < _MAX_FIELDS or
                not isinstance(item["value"], str) or len(item["value"]) > 5000):
            return {"status": "needs_review", "reason": "manual_application_required"}
        total_chars += len(item["value"])
        safe_fields.append({"dom_index": item["dom_index"], "value": item["value"]})
    if total_chars > 40_000:
        return {"status": "needs_review", "reason": "manual_application_required"}
    button_index = plan.get("submit_button_index")
    if isinstance(button_index, bool) or not isinstance(button_index, int) or not 0 <= button_index < 80:
        return {"status": "needs_review", "reason": "manual_application_required"}

    resume_dom_index = plan.get("resume_dom_index")
    if resume_dom_index is not None:
        if (isinstance(resume_dom_index, bool) or not isinstance(resume_dom_index, int) or
                not 0 <= resume_dom_index < _MAX_FIELDS or not resume_path):
            return {"status": "needs_review", "reason": "missing_required_answer"}
        try:
            candidate_path = Path(resume_path)
            resolved_state = state_path.resolve(strict=False)
            resolved_file = candidate_path.resolve(strict=True)
            resolved_file.relative_to(resolved_state)
            if candidate_path.is_symlink() or not resolved_file.is_file():
                raise ValueError("unsafe_resume_path")
        except (OSError, ValueError):
            return {"status": "needs_review", "reason": "missing_required_answer"}
    else:
        resolved_file = None

    try:
        world = conn.command(
            "Page.createIsolatedWorld",
            {"frameId": frame_id, "worldName": "jobbot-application", "grantUniveralAccess": False},
            timeout=min(5.0, max(0.1, deadline - time.monotonic())),
        )
        context_id = world.get("executionContextId")
        if not isinstance(context_id, int):
            return {"status": "needs_review", "reason": "manual_application_required"}
        encoded_fields = json.dumps(safe_fields, ensure_ascii=True, separators=(",", ":"))
        fill_expression = f"""(() => {{
          const actions = {encoded_fields};
          const controls = Array.from(document.querySelectorAll('input, select, textarea'));
          const visible = el => {{ try {{ const s=getComputedStyle(el); return s.display!=='none' && s.visibility!=='hidden' && el.getClientRects().length>0; }} catch (_) {{ return false; }} }};
          for (const action of actions) {{
            const el = controls[action.dom_index];
            if (!el || !visible(el) || el.disabled || el.readOnly || String(el.value || '').trim()) return {{ok:false}};
            if (el.tagName === 'INPUT' && ['file','password','checkbox','radio','hidden','submit','button'].includes((el.type || '').toLowerCase())) return {{ok:false}};
            if (el.tagName === 'SELECT' && !Array.from(el.options || []).some(o => o.value === action.value && o.value !== '')) return {{ok:false}};
            const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : (el.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype);
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(el, action.value); else el.value = action.value;
            el.dispatchEvent(new Event('input', {{bubbles:true}}));
            el.dispatchEvent(new Event('change', {{bubbles:true}}));
          }}
          return {{ok:true}};
        }})()"""
        filled = conn.command(
            "Runtime.evaluate",
            {"expression": fill_expression, "contextId": context_id,
             "returnByValue": True, "awaitPromise": False, "silent": True,
             "timeout": 5000},
            timeout=min(7.0, max(0.1, deadline - time.monotonic())),
        )
        if filled.get("exceptionDetails") or filled.get("result", {}).get("value") != {"ok": True}:
            return {"status": "needs_review", "reason": "missing_required_answer"}
        if resolved_file is not None:
            conn.command("DOM.enable", timeout=min(5.0, max(0.1, deadline - time.monotonic())))
            document = conn.command(
                "DOM.getDocument", {"depth": -1, "pierce": True},
                timeout=min(5.0, max(0.1, deadline - time.monotonic())),
            )
            root_id = document.get("root", {}).get("nodeId")
            if not isinstance(root_id, int):
                return {"status": "needs_review", "reason": "missing_required_answer"}
            nodes = conn.command(
                "DOM.querySelectorAll",
                {"nodeId": root_id, "selector": "input, select, textarea"},
                timeout=min(5.0, max(0.1, deadline - time.monotonic())),
            ).get("nodeIds")
            if (not isinstance(nodes, list) or resume_dom_index >= len(nodes) or
                    not isinstance(nodes[resume_dom_index], int)):
                return {"status": "needs_review", "reason": "missing_required_answer"}
            conn.command(
                "DOM.setFileInputFiles",
                {"nodeId": nodes[resume_dom_index], "files": [str(resolved_file)]},
                timeout=min(5.0, max(0.1, deadline - time.monotonic())),
            )
        if before_submit is not None:
            gate_result = before_submit()
            if gate_result is not True:
                state["submit_armed"] = False
                reason = (gate_result if isinstance(gate_result, str) and gate_result in {
                    "daily_limit_reached", "settings_changed",
                    "uncertain_submission",
                } else "settings_changed")
                return {"status": "not_submitted", "reason": reason}
        button_expression = f"""(() => {{
          const buttons = Array.from(document.querySelectorAll('button, input[type=\\"submit\\"], [role=\\"button\\"]')).slice(0,80);
          const button = buttons[{button_index}];
          const norm = value => String(value || '').replace(/\\s+/g,' ').trim().toLowerCase();
          const text = norm(button && (button.innerText || button.value || button.getAttribute('aria-label')));
          const allowed = new Set(['submit application','submit your application','send application','send your application','apply for this job']);
          const visible = el => {{ try {{ const s=getComputedStyle(el); return s.display!=='none' && s.visibility!=='hidden' && el.getClientRects().length>0; }} catch (_) {{ return false; }} }};
          if (!button || button.disabled || !visible(button) || !allowed.has(text)) return {{clicked:false}};
          button.click();
          return {{clicked:true}};
        }})()"""
        state["submit_armed"] = True
        state["submit_deadline"] = time.monotonic() + 8.0
        state["submit_requests"] = 0
        clicked = conn.command(
            "Runtime.evaluate",
            {"expression": button_expression, "contextId": context_id,
             "returnByValue": True, "awaitPromise": False, "silent": True,
             "timeout": 5000},
            timeout=min(7.0, max(0.1, deadline - time.monotonic())),
        )
        if clicked.get("exceptionDetails") or clicked.get("result", {}).get("value") != {"clicked": True}:
            state["submit_armed"] = False
            return {"status": "needs_review", "reason": "manual_application_required"}
    except CDPFailure:
        state["submit_armed"] = False
        return {"status": "needs_review", "reason": "uncertain_submission"}
    except (OSError, ValueError, TypeError):
        state["submit_armed"] = False
        return {"status": "needs_review", "reason": "uncertain_submission"}

    confirmation_phrases = (
        "thank you for applying", "thank you for your application",
        "your application has been submitted", "application submitted",
        "application received", "we have received your application",
        "successfully applied",
    )
    already_phrases = ("already applied", "application already received", "you have already applied")
    poll_deadline = min(deadline, time.monotonic() + 20.0)
    while time.monotonic() < poll_deadline:
        try:
            conn.pump_for(min(0.4, max(0.1, poll_deadline - time.monotonic())))
            current_tree = conn.command(
                "Page.getFrameTree", timeout=min(4.0, max(0.1, poll_deadline - time.monotonic()))
            )
            current_frame = current_tree.get("frameTree", {}).get("frame", {})
            current_frame_id = current_frame.get("id", frame_id)
            page_data = _extract_page_data(
                conn, current_frame_id, timeout=min(4.0, max(0.1, poll_deadline - time.monotonic()))
            )
        except (CDPFailure, InspectionFailure):
            time.sleep(0.2)
            continue
        current_url, _ = _safe_location(page_data.get("href", ""), canonical_host)
        current_text = str(page_data.get("visible_text", "")).casefold()
        if current_text and any(phrase in current_text for phrase in confirmation_phrases):
            state["submit_armed"] = False
            return {"status": "submitted", "reason": ""}
        if current_text and any(phrase in current_text for phrase in already_phrases):
            state["submit_armed"] = False
            return {"status": "needs_review", "reason": "possible_duplicate"}
        signals = _detect_handoff(page_data, state.get("blocked", set()))
        if any("captcha" in signal for signal in signals):
            state["submit_armed"] = False
            return {"status": "needs_review", "reason": "captcha"}
        if "login_or_authentication" in signals:
            state["submit_armed"] = False
            return {"status": "needs_review", "reason": "verification_challenge"}
        if "site_block" in signals:
            state["submit_armed"] = False
            return {"status": "needs_review", "reason": "site_block"}
        if "cross_host_redirect_blocked" in state.get("blocked", set()) or not current_url:
            state["submit_armed"] = False
            return {"status": "needs_review", "reason": "uncertain_submission"}
        if any(term in current_text for term in (
                "please complete required fields", "required field", "please correct the following",
                "there was an error submitting", "unable to submit your application")):
            state["submit_armed"] = False
            return {"status": "needs_review", "reason": "missing_required_answer"}
        time.sleep(0.25)
    state["submit_armed"] = False
    return {"status": "needs_review", "reason": "uncertain_submission"}


def inspect_public_page(
    url: str,
    state_dir: str | os.PathLike[str],
    user_id: str,
    timeout_seconds: int = 45,
    *,
    submit_plan: dict[str, Any] | None = None,
    resume_path: str = "",
    before_submit: Callable[[], object] | None = None,
    handoff_queue_id: str = "",
) -> dict[str, Any]:
    """Inspect a public HTTPS page, or execute a tightly checked submit plan.

    Returns a JSON-safe dictionary. The final URL omits query and fragment
    values; ``final_url_query_redacted`` indicates when those values existed.
    Calls are bounded to 10–120 seconds and a per-user profile lock prevents
    concurrent Chromium instances from sharing the same profile. The default
    is read-only. A submit plan is accepted only by the in-process worker after
    it has independently mapped every required field to an approved answer.
    """
    xvfb_proc: subprocess.Popen[bytes] | None = None
    chromium_proc: subprocess.Popen[bytes] | None = None
    conn: CDPConnection | None = None
    auth_dir: str | None = None
    lock_fd: int | None = None
    preserve_browser = False
    try:
        normalized_url, canonical_host, safe_requested_url = _validate_url(url)
        user_id = _safe_user_id(user_id)
        if handoff_queue_id and not _QUEUE_ID_RE.fullmatch(handoff_queue_id):
            raise InspectionFailure("invalid_handoff", "The application queue handoff ID is invalid.")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
            raise InspectionFailure("invalid_timeout", "The timeout must be an integer from 10 to 120 seconds.")
        timeout_seconds = min(120, max(10, timeout_seconds))
        deadline = time.monotonic() + timeout_seconds

        def remaining(maximum: float) -> float:
            return max(0.1, min(maximum, deadline - time.monotonic()))

        def preserve_for_human(signals: list[str]) -> bool:
            nonlocal preserve_browser
            if (not handoff_queue_id or not _needs_human_verification(signals) or
                    not state_path or not auth_dir or not display or
                    chromium_proc is None or xvfb_proc is None or lock_fd is None):
                return False
            preserve_browser = _start_browser_handoff(
                state_path, user_id, handoff_queue_id, display,
                os.path.join(auth_dir, "Xauthority"), chromium_proc, xvfb_proc, lock_fd,
            )
            return preserve_browser

        try:
            parsed = urllib.parse.urlsplit(url)
            is_ip_literal = False
            if parsed.hostname:
                try:
                    ipaddress.ip_address(parsed.hostname)
                    is_ip_literal = True
                except ValueError:
                    pass
        except ValueError:
            raise InspectionFailure("invalid_url", "Provide one valid HTTPS URL.") from None
        pinned_ip = _resolve_public_host(canonical_host, is_ip_literal, timeout=remaining(5.0))
        if os.geteuid() == 0:
            raise InspectionFailure("unsafe_execution_context", "Run Chromium as an unprivileged user.")
        try:
            state_path = Path(state_dir).expanduser().resolve(strict=False)
        except (TypeError, OSError):
            raise InspectionFailure("invalid_state_dir", "The browser state directory is invalid.") from None
        lock_fd, profile = _profile_lock(state_path, user_id)
        _chromium_singletons_are_stale(profile)

        active_port_file = profile / "DevToolsActivePort"
        if active_port_file.is_symlink():
            raise InspectionFailure("cdp_unavailable", "Chromium did not create a safe CDP endpoint.")
        if active_port_file.exists():
            try:
                if not active_port_file.is_file():
                    raise InspectionFailure("cdp_unavailable", "Chromium did not create a safe CDP endpoint.")
                active_port_file.unlink()
            except OSError:
                raise InspectionFailure("cdp_unavailable", "The previous CDP endpoint could not be cleared.") from None

        chromium = _available_binary(("chromium", "chromium-browser", "google-chrome"))
        if not chromium:
            raise InspectionFailure("missing_chromium", "Install the Chromium system package.")
        xvfb_proc, display, auth_dir = _start_xvfb(remaining(8.0))
        auth_file = os.path.join(auth_dir, "Xauthority")
        env = os.environ.copy()
        env["DISPLAY"] = display
        env["XAUTHORITY"] = auth_file
        # Chromium is never given a target URL on its process command line.
        # This profile is user scoped and browser remote debugging binds only
        # to loopback; no remote-allow-origins wildcard is used.
        chromium_args = [
            chromium,
            f"--user-data-dir={profile}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1280,900",
            "--disable-session-restore",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            # Use Chromium's unprivileged user-namespace sandbox in Docker;
            # the applier seccomp profile permits its namespace calls.
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-proxy-server",
            "--new-window",
            "about:blank",
        ]
        if not is_ip_literal:
            chromium_args.append(f"--host-resolver-rules=MAP {canonical_host} {pinned_ip}")
        try:
            chromium_proc = subprocess.Popen(
                chromium_args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
                pass_fds=(lock_fd,),
            )
        except OSError:
            raise InspectionFailure("browser_start_failed", "Chromium could not be started.") from None
        port, _version = _wait_for_browser(profile, chromium_proc, remaining(20.0))
        target, _created = _get_page_target(port)
        try:
            host, ws_port, ws_path = _validate_websocket_url(
                target.get("webSocketDebuggerUrl"), port, "/devtools/page/"
            )
            conn = CDPConnection(host, ws_port, ws_path, timeout=remaining(4.0))
        except CDPFailure:
            raise InspectionFailure("cdp_unavailable", "Chromium's page inspection connection failed.") from None

        try:
            conn.command("Page.enable", timeout=remaining(5.0))
            conn.command("Runtime.enable", timeout=remaining(5.0))
            conn.command("Network.enable", timeout=remaining(5.0))
            conn.command("Network.setBypassServiceWorker", {"bypass": True}, timeout=remaining(5.0))
            conn.command("Network.setCacheDisabled", {"cacheDisabled": True}, timeout=remaining(5.0))
            frame_tree = conn.command("Page.getFrameTree", timeout=remaining(5.0))
            main_frame = frame_tree.get("frameTree", {}).get("frame", {})
            main_frame_id = main_frame.get("id")
            if not isinstance(main_frame_id, str):
                raise CDPFailure("main frame unavailable")
        except CDPFailure:
            raise InspectionFailure("cdp_unavailable", "Chromium could not prepare a read-only page inspection.") from None

        state: dict[str, Any] = {
            "main_frame_id": main_frame_id,
            "main_doc_count": 0,
            "network_ids": set(),
            "blocked": set(),
            "load_seen": False,
            "submit_armed": False,
            "submit_deadline": 0.0,
            "submit_requests": 0,
        }

        def handle_event(message: dict[str, Any]) -> None:
            method = message.get("method")
            params = message.get("params", {})
            if not isinstance(params, dict):
                return
            if method == "Page.loadEventFired":
                state["load_seen"] = True
                return
            if method != "Fetch.requestPaused":
                return
            request_id = params.get("requestId")
            request = params.get("request", {})
            if not isinstance(request_id, str) or not isinstance(request, dict):
                return
            method_name = str(request.get("method", "GET")).upper()
            resource_type = params.get("resourceType")
            frame_id = params.get("frameId")
            request_url = request.get("url", "")
            blocked_reason: str | None = None
            submit_request = False
            if (method_name == "POST" and state["submit_armed"] and
                    time.monotonic() <= state["submit_deadline"] and
                    state["submit_requests"] == 0 and
                    resource_type in {"Document", "XHR", "Fetch"} and
                    frame_id == state["main_frame_id"]):
                try:
                    parsed_request = urllib.parse.urlsplit(str(request_url))
                    request_host = _canonical_host(parsed_request.hostname or "")[0]
                    submit_request = (parsed_request.scheme.lower() == "https" and
                                      request_host == canonical_host and
                                      parsed_request.port in (None, 443))
                except ValueError:
                    submit_request = False
            if submit_request:
                state["submit_requests"] += 1
                state["submit_armed"] = False
                network_id = params.get("networkId")
                if resource_type == "Document" and isinstance(network_id, str):
                    state["network_ids"].add(network_id)
            elif method_name not in ("GET", "HEAD"):
                blocked_reason = "non_get_request_blocked"
            elif resource_type == "Document":
                if frame_id != state["main_frame_id"]:
                    blocked_reason = "subframe_blocked"
                elif not _url_policy_allows(str(request_url), canonical_host):
                    blocked_reason = "cross_host_redirect_blocked"
                else:
                    redirected_id = params.get("redirectedRequestId")
                    network_id = params.get("networkId")
                    first = state["main_doc_count"] == 0
                    redirect = bool(redirected_id) or (
                        isinstance(network_id, str) and network_id in state["network_ids"]
                    )
                    if first:
                        # The first top-frame document must match the exact
                        # requested URL (ignoring a fragment, which is local).
                        try:
                            requested = urllib.parse.urlsplit(normalized_url)
                            candidate = urllib.parse.urlsplit(str(request_url))
                            candidate_host = _canonical_host(candidate.hostname or "")[0]
                            same_initial = (
                                candidate.scheme.lower() == "https"
                                and candidate_host == canonical_host
                                and candidate.port in (None, 443)
                                and candidate.path == requested.path
                                and candidate.query == requested.query
                            )
                        except ValueError:
                            same_initial = False
                        if not same_initial:
                            blocked_reason = "unexpected_navigation_blocked"
                    elif not redirect or state["main_doc_count"] >= 10:
                        blocked_reason = "unexpected_navigation_blocked"
                    if blocked_reason is None:
                        state["main_doc_count"] += 1
                        if isinstance(network_id, str):
                            state["network_ids"].add(network_id)
            if blocked_reason:
                state["blocked"].add(blocked_reason)
                try:
                    conn.send_no_wait(
                        "Fetch.failRequest",
                        {"requestId": request_id, "errorReason": "BlockedByClient"},
                    )
                except CDPFailure:
                    pass
            else:
                try:
                    conn.send_no_wait("Fetch.continueRequest", {"requestId": request_id})
                except CDPFailure:
                    pass

        conn.set_event_handler(handle_event)
        try:
            conn.command(
                "Fetch.enable",
                {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
                timeout=remaining(5.0),
            )
        except CDPFailure:
            raise InspectionFailure("cdp_unavailable", "Chromium could not enforce safe request handling.") from None
        try:
            navigation = conn.command("Page.navigate", {"url": normalized_url}, timeout=remaining(8.0))
        except CDPTimeout:
            raise InspectionFailure("navigation_timeout", "The page navigation timed out.") from None
        except CDPFailure:
            raise InspectionFailure("navigation_failed", "Chromium could not navigate to the requested page.") from None
        if navigation.get("errorText"):
            raise InspectionFailure("navigation_failed", "Chromium could not load the requested page.")

        navigation_deadline = min(deadline, time.monotonic() + remaining(22.0))
        blocked_before_load = {"cross_host_redirect_blocked", "unexpected_navigation_blocked"}
        while (
            not state["load_seen"]
            and not (state["blocked"] & blocked_before_load)
            and time.monotonic() < navigation_deadline
        ):
            try:
                conn._read_message(min(0.2, navigation_deadline - time.monotonic()))
            except CDPTimeout:
                continue
            except CDPFailure:
                break
        if not state["load_seen"] and state["blocked"] & blocked_before_load:
            blocked = state["blocked"]
            signals = _detect_handoff({}, blocked)
            error_code = "navigation_policy_blocked"
            return _make_result(
                requested_url=safe_requested_url,
                final_url="",
                query_redacted=bool(urllib.parse.urlsplit(url).query),
                title="",
                visible_text="",
                forms=[],
                challenge_signals=signals,
                navigation_error_code=error_code,
            )
        if not state["load_seen"]:
            try:
                conn.send_no_wait("Page.stopLoading", {})
            except CDPFailure:
                pass
            raise InspectionFailure("navigation_timeout", "The page did not finish loading in time.")
        # Let GET-backed SPA rendering settle after the document load. The
        # snapshot loop remains read-only and the Fetch gate continues to block
        # all non-GET/HEAD requests unless the explicit submit path is armed.
        try:
            current_tree = conn.command("Page.getFrameTree", timeout=remaining(4.0))
            current_frame = current_tree.get("frameTree", {}).get("frame", {})
            current_frame_id = current_frame.get("id", main_frame_id)
            page_data = _wait_for_page_render(
                conn, current_frame_id, timeout=remaining(8.5), maximum_wait=8.0,
            )
        except InspectionFailure:
            raise
        except CDPFailure:
            raise InspectionFailure("page_inspection_failed", "The page could not be inspected safely.") from None

        raw_final_url = page_data.get("href", "")
        final_url, query_redacted = _safe_location(raw_final_url, canonical_host)
        page_title = str(page_data.get("title", ""))[:300]
        visible_text = str(page_data.get("visible_text", ""))[:_MAX_TEXT_CHARS]
        forms: list[dict[str, Any]] = []
        raw_fields = page_data.get("fields", [])
        raw_forms = page_data.get("forms", [])
        normalized_fields = [
            field
            for field in raw_fields[:_MAX_FIELDS]
            if isinstance(field, dict)
        ] if isinstance(raw_fields, list) else []
        if isinstance(raw_forms, list):
            for raw_form in raw_forms[:_MAX_FORMS]:
                if not isinstance(raw_form, dict):
                    continue
                index = raw_form.get("index")
                if not isinstance(index, int):
                    continue
                fields = [field for field in normalized_fields if field.get("form_index") == index]
                forms.append(
                    {
                        "index": index,
                        "method": str(raw_form.get("method", "get"))[:16].lower(),
                        "field_count": len(fields),
                        "fields": fields,
                    }
                )
        ungrouped = [field for field in normalized_fields if field.get("form_index") == -1]
        if ungrouped:
            forms.append(
                {
                    "index": -1,
                    "method": "none",
                    "field_count": len(ungrouped),
                    "fields": ungrouped,
                }
            )
        raw_buttons = page_data.get("buttons", [])
        buttons = [button for button in raw_buttons[:80] if isinstance(button, dict)] \
            if isinstance(raw_buttons, list) else []
        form_signature = _form_signature(forms, buttons)
        blocked = state["blocked"]
        signals = _detect_handoff(page_data, blocked)
        if "non_get_request_blocked" in blocked:
            error_code = "unsafe_request_blocked"
        elif "cross_host_redirect_blocked" in blocked or "unexpected_navigation_blocked" in blocked:
            error_code = "navigation_policy_blocked"
        else:
            error_code = None
        result = _make_result(
            requested_url=safe_requested_url,
            final_url=final_url,
            query_redacted=query_redacted,
            title=page_title,
            visible_text=visible_text,
            forms=forms,
            buttons=buttons,
            form_signature=form_signature,
            challenge_signals=signals,
            navigation_error_code=error_code,
        )
        raw_links = page_data.get("apply_links", [])
        if isinstance(raw_links, list) and not signals:
            result["apply_links"] = [entry for entry in raw_links[:12]
                                     if isinstance(entry, dict) and
                                     isinstance(entry.get("href"), str) and
                                     isinstance(entry.get("label"), str)]
        if submit_plan is None:
            preserve_for_human(signals)
            return result
        if (not isinstance(submit_plan, dict) or
                submit_plan.get("form_signature") != form_signature or signals):
            if any("captcha" in str(signal).lower() for signal in signals):
                reason = "captcha"
            elif "site_block" in signals:
                reason = "site_block"
            elif signals:
                reason = "verification_challenge"
            else:
                reason = "manual_application_required"
            if reason in {"captcha", "verification_challenge"}:
                preserve_for_human(signals)
            result["application_result"] = {"status": "needs_review", "reason": reason}
            result["visible_text"] = ""
            result["forms"] = []
            result["buttons"] = []
            return result
        action_result = _execute_submit_plan(
            conn, current_frame_id, submit_plan, resume_path, state_path, state,
            canonical_host, deadline, before_submit,
        )
        if action_result.get("reason") == "captcha":
            preserve_for_human(["captcha_or_verification"])
        elif action_result.get("reason") == "verification_challenge":
            preserve_for_human(["login_or_authentication"])
        return {
            "ok": True,
            "status": action_result["status"],
            "final_url": final_url,
            "final_url_query_redacted": query_redacted,
            "application_result": action_result,
            "browser_handoff_active": preserve_browser,
        }
    except InspectionFailure as exc:
        return _failure(exc.code, exc.message)
    except (OSError, ValueError, TypeError):
        return _failure("inspection_failed", "The page inspection failed safely.")
    except SystemExit:
        raise
    except BaseException:
        # Never expose exception text, browser logs, URLs, or page content.
        return _failure("inspection_failed", "The page inspection failed safely.")
    finally:
        if conn is not None:
            conn.close()
        if not preserve_browser:
            _stop_process_group(chromium_proc)
            _stop_process_group(xvfb_proc)
            if auth_dir:
                shutil.rmtree(auth_dir, ignore_errors=True)
        if lock_fd is not None and not preserve_browser:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass


def apply_public_page(
    url: str,
    state_dir: str | os.PathLike[str],
    user_id: str,
    submit_plan: dict[str, Any],
    *,
    resume_path: str = "",
    before_submit: Callable[[], object] | None = None,
    timeout_seconds: int = 45,
    handoff_queue_id: str = "",
) -> dict[str, Any]:
    """Apply a pre-approved deterministic plan, using the headed CDP browser."""
    return inspect_public_page(
        url, state_dir, user_id, timeout_seconds,
        submit_plan=submit_plan, resume_path=resume_path,
        before_submit=before_submit, handoff_queue_id=handoff_queue_id,
    )


def _read_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if len(raw) > _MAX_STDIN_BYTES:
        raise InspectionFailure("invalid_request", "The JSON request is too large.")
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InspectionFailure("invalid_request", "Provide a JSON object on stdin.") from None
    if not isinstance(request, dict):
        raise InspectionFailure("invalid_request", "Provide a JSON object on stdin.")
    allowed = {"url", "user_id", "timeout_seconds"}
    if set(request) - allowed:
        raise InspectionFailure("invalid_request", "The JSON request contains unsupported fields.")
    return request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only headed Chromium CDP page inspection")
    parser.add_argument("--state-dir", required=True, help="Private persistent application state directory")
    args = parser.parse_args(argv)
    try:
        request = _read_request()
        result = inspect_public_page(
            request.get("url"),
            args.state_dir,
            request.get("user_id"),
            request.get("timeout_seconds", 45),
        )
    except InspectionFailure as exc:
        result = _failure(exc.code, exc.message)
    except BaseException:
        result = _failure("inspection_failed", "The page inspection failed safely.")
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
