"""Always-on, per-user queue consumer for browser application work.

In submit-eligible mode, the worker can submit simple forms across queued job
hosts using approved profile facts and an approved resume. Unknown required
answers, legal declarations, challenges, and uncertain outcomes go to review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

try:
    from . import (application_forms, application_store, browser_cdp, control_store,
                   job_queue, resume_parser, search_adapters, source_adapters)
except ImportError:
    import application_forms
    import application_store
    import browser_cdp
    import control_store
    import job_queue
    import resume_parser
    import search_adapters
    import source_adapters


LOG = logging.getLogger("jobbot.application")
WORKER_ID = f"application-worker-{os.getpid()}"
STATUS_PATH = control_store.STATE_DIR / "application_worker_status.json"


class InfrastructureBlocked(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _status(state: str, code: str = "") -> None:
    """Expose only process health; never put URLs or account data in status."""
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = STATUS_PATH.with_name(STATUS_PATH.name + f".{os.getpid()}.tmp")
    path.write_text(json.dumps({"updated_at": _stamp(), "state": state,
                                "code": code}), encoding="utf-8")
    os.chmod(path, 0o600)
    os.replace(path, STATUS_PATH)


def _ready(user: dict[str, object]) -> bool:
    if not user.get("active") or user.get("must_change_password"):
        return False
    settings = control_store.get_settings(user)
    if settings["application_paused"] or settings["autonomy_mode"] == "discovery_only":
        return False
    profile = control_store.get_profile(user)
    if not profile["version"] or profile["version"] != profile["approved_version"]:
        return False
    # Require an approved, readable primary resume before any application
    # workflow can run. Resume bytes are loaded only for an authorized submit.
    documents = control_store.list_resume_documents(user)
    return any(doc["purpose"] == "primary" and doc["approved_at"] and
               int(doc["text_chars"]) > 0 for doc in documents)


def _submissions_today(db_path: Path) -> int:
    today = datetime.now(timezone.utc).date()
    count = 0
    for application in application_store.list_applications(db_path):
        if application.get("status") == "in_progress":
            # Reserve capacity while a prior submission outcome is unresolved.
            count += 1
            continue
        submitted_at = str(application.get("submitted_at") or "")
        if not submitted_at:
            continue
        try:
            submitted = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
            if submitted.tzinfo is None:
                submitted = submitted.replace(tzinfo=timezone.utc)
            if submitted.astimezone(timezone.utc).date() == today:
                count += 1
        except ValueError:
            continue
    return count


def _daily_limit_reached(settings: dict[str, object], db_path: Path) -> bool:
    """Return whether today's cap is reached; -1 explicitly means unlimited."""
    limit = int(settings["daily_application_limit"])
    return limit != -1 and _submissions_today(db_path) >= limit


def _primary_resume(user: dict[str, object]) -> dict[str, object] | None:
    documents = [doc for doc in control_store.list_resume_documents(user)
                 if doc["purpose"] == "primary" and doc["approved_at"] and
                 int(doc["text_chars"]) > 0]
    return documents[0] if len(documents) == 1 else None


def _fresh_user(user_id: str) -> dict[str, object] | None:
    return next((candidate for candidate in control_store.list_worker_users()
                 if str(candidate["user_id"]) == user_id), None)


def _write_temporary_resume(user: dict[str, object], document: dict[str, object],
                            loaded: dict[str, object] | None = None) -> Path:
    loaded = loaded or control_store.load_resume_document(user, int(document["document_id"]))
    extension = str(loaded["file_format"]).lower()
    if extension not in {"pdf", "docx", "txt"} or not isinstance(loaded.get("content"), bytes):
        raise ValueError("resume_unavailable")
    temporary_dir = control_store.STATE_DIR / "application-tmp"
    temporary_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if temporary_dir.is_symlink() or not temporary_dir.is_dir():
        raise ValueError("resume_temporary_directory_unavailable")
    fd, raw_path = tempfile.mkstemp(prefix="application-resume-", suffix=f".{extension}",
                                    dir=temporary_dir)
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(loaded["content"])
        os.chmod(path, 0o600)
        return path
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _review(user_id: str, item: dict[str, object], db_path: Path, reason: str) -> None:
    job_queue.require_review(user_id, str(item["queue_id"]), WORKER_ID,
                             str(item["lease_token"]), db_path=db_path, reason=reason)


def _fail(user_id: str, item: dict[str, object], db_path: Path, reason: str) -> None:
    job_queue.record_failure(user_id, str(item["queue_id"]), WORKER_ID,
                             str(item["lease_token"]), db_path=db_path,
                             reason=reason, retry_after_seconds=300)


def _process(user: dict[str, object], item: dict[str, object], db_path: Path) -> None:
    user_id = str(user["user_id"])
    url = str(item["job_url"])
    # Settings can change after the claim and before any external request.
    if not _ready(user):
        job_queue.release_unstarted(user_id, str(item["queue_id"]), WORKER_ID,
                                    str(item["lease_token"]), db_path=db_path)
        return
    existing = application_store.find_existing_application(
        job_url=url, employer_job_id=str(item["employer_job_id"] or ""),
        job_source=str(item["job_source"] or ""), company=str(item["company"] or ""),
        role=str(item["role"] or ""), db_path=db_path,
    )
    linked = existing.get("application")
    if (existing.get("match_type") != "exact_url" or not isinstance(linked, dict) or
            linked.get("application_key") != item["application_key"] or
            linked.get("status") != "discovered"):
        _review(user_id, item, db_path, "possible_duplicate")
        return
    settings = control_store.get_settings(user)
    automatic = settings["autonomy_mode"] == "submit_eligible"
    if automatic:
        if not str(item["role"] or "").strip() or not str(item["company"] or "").strip():
            _review(user_id, item, db_path, "manual_application_required")
            return
        if _daily_limit_reached(settings, db_path):
            job_queue.release_unstarted(user_id, str(item["queue_id"]), WORKER_ID,
                                        str(item["lease_token"]), db_path=db_path,
                                        reason="daily_limit_reached")
            return
    try:
        source_adapters.validate_feed_url(url)
    except ValueError:
        if item["pool_id"]:
            job_queue.report_invalid_url(user_id, str(item["queue_id"]), WORKER_ID,
                                         str(item["lease_token"]), db_path=db_path)
        else:
            _review(user_id, item, db_path, "source_access_unavailable")
        return
    if search_adapters.is_known_collection_page(url):
        _review(user_id, item, db_path, "collection_page")
        return
    try:
        # Check public DNS and robots.txt without a plain HTTP page fetch: some
        # sites serve a normal browser while rejecting simple HTTP clients.
        source_adapters.check_public_page_access(url)
    except source_adapters.SourceError as exc:
        if exc.code in {"robots_disallowed", "robots_unavailable", "unsafe_redirect",
                        "unsafe_page_url", "not_html", "page_too_large"}:
            _review(user_id, item, db_path, "source_access_unavailable")
        elif exc.code == "http_error":
            _review(user_id, item, db_path, "site_block")
        else:
            _fail(user_id, item, db_path, "network_error")
        return
    if not _ready(user):
        job_queue.release_unstarted(user_id, str(item["queue_id"]), WORKER_ID,
                                    str(item["lease_token"]), db_path=db_path)
        return
    job_queue.heartbeat(user_id, str(item["queue_id"]), WORKER_ID,
                        str(item["lease_token"]), db_path=db_path, lease_seconds=180)
    result = browser_cdp.inspect_public_page(
        url, control_store.STATE_DIR, user_id, timeout_seconds=45,
        handoff_queue_id=str(item["queue_id"]),
    )
    # Page content is transient. The model sees only bounded form labels and
    # approved reusable facts, never the full page or credentials.
    if not result.get("ok"):
        code = str(result.get("error_code") or "")
        if code in {"missing_chromium", "missing_xvfb", "browser_start_failed",
                    "xvfb_start_failed", "cdp_unavailable", "invalid_state_dir",
                    "profile_in_use", "unexpected_browser_target"}:
            job_queue.release_unstarted(user_id, str(item["queue_id"]), WORKER_ID,
                                        str(item["lease_token"]), db_path=db_path)
            raise InfrastructureBlocked(code)
        if code in {"site_block", "http_error", "challenge", "captcha"}:
            _review(user_id, item, db_path, "site_block")
        elif code in {"invalid_url", "unsafe_redirect", "unsafe_destination", "blocked_url",
                      "non_public_host", "navigation_policy_blocked"}:
            _review(user_id, item, db_path, "source_access_unavailable")
        else:
            _fail(user_id, item, db_path,
                  "page_load_timeout" if "timeout" in code else "browser_crash")
        return
    if result.get("challenge_detected") or result.get("human_review_required"):
        signals = result.get("challenge_signals") or []
        if "site_block" in signals:
            reason = "site_block"
        elif "unexpected_navigation" in signals:
            reason = "source_access_unavailable"
        elif any("captcha" in str(signal).lower() for signal in signals):
            reason = "captcha"
        elif signals == ["non_get_request_blocked"]:
            # Many sites send a background POST while rendering an otherwise
            # readable job page. Blocking that request is expected for this
            # read-only worker; it is not proof of a verification challenge.
            reason = "manual_application_required"
        else:
            reason = "verification_challenge"
        _review(user_id, item, db_path, reason)
        return
    final_url = str(result.get("final_url") or "")
    if urlsplit(final_url).hostname != urlsplit(url).hostname:
        _review(user_id, item, db_path, "source_access_unavailable")
        return
    application_url = url
    has_post_form = any(
        isinstance(form, dict) and str(form.get("method") or "").lower() == "post" and
        form.get("fields") for form in result.get("forms", [])
    )
    if automatic and not has_post_form:
        # Follow one explicit same-host Apply link from the public posting.
        # Keep the original exact listing URL as the ledger key.
        candidates = []
        for link in result.get("apply_links", []):
            if not isinstance(link, dict):
                continue
            target = str(link.get("href") or "")
            try:
                if target != url and urlsplit(target).hostname == urlsplit(url).hostname:
                    candidates.append(target)
            except ValueError:
                continue
        candidates = list(dict.fromkeys(candidates))
        if len(candidates) == 1:
            target = candidates[0]
            try:
                source_adapters.validate_feed_url(target)
                source_adapters.check_public_page_access(target)
            except (ValueError, source_adapters.SourceError):
                _review(user_id, item, db_path, "source_access_unavailable")
                return
            target_result = browser_cdp.inspect_public_page(
                target, control_store.STATE_DIR, user_id, timeout_seconds=45,
                handoff_queue_id=str(item["queue_id"]),
            )
            if not target_result.get("ok"):
                _review(user_id, item, db_path, "source_access_unavailable")
                return
            signals = target_result.get("challenge_signals") or []
            if target_result.get("challenge_detected") or target_result.get("human_review_required"):
                reason = "captcha" if any("captcha" in str(s).lower() for s in signals) else "verification_challenge"
                _review(user_id, item, db_path, reason)
                return
            if urlsplit(str(target_result.get("final_url") or "")).hostname != urlsplit(target).hostname:
                _review(user_id, item, db_path, "source_access_unavailable")
                return
            application_url, result = target, target_result
    if not automatic:
        _review(user_id, item, db_path, "manual_application_required")
        return

    profile = control_store.get_profile(user)
    resume = _primary_resume(user)
    if (profile["version"] != profile["approved_version"] or not resume or
            not resume.get("sha256")):
        _review(user_id, item, db_path, "missing_required_answer")
        return
    try:
        loaded_resume = control_store.load_resume_document(user, int(resume["document_id"]))
        resume_bytes = loaded_resume["content"]
        if (not isinstance(resume_bytes, bytes) or
                hashlib.sha256(resume_bytes).hexdigest() != resume["sha256"]):
            raise ValueError("resume_checksum_mismatch")
        extracted = resume_parser.extract_resume(str(loaded_resume["filename"]), resume_bytes)
        if not isinstance(extracted.get("text"), str) or not extracted["text"].strip():
            raise ValueError("resume_has_no_readable_text")
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        _review(user_id, item, db_path, "missing_required_answer")
        return
    job_queue.heartbeat(user_id, str(item["queue_id"]), WORKER_ID,
                        str(item["lease_token"]), db_path=db_path, lease_seconds=180)
    plan = application_forms.build_plan(
        profile, result, resume_extension=str(resume.get("file_format") or ""),
        job_context={"role": str(item.get("role") or ""),
                     "company": str(item.get("company") or ""),
                     "location": str(item.get("location") or "")},
    )
    if not plan.get("ready"):
        reason = str(plan.get("reason") or "manual_application_required")
        if reason == "model_error":
            _fail(user_id, item, db_path, "model_error")
        else:
            _review(user_id, item, db_path, reason)
        return

    job_queue.heartbeat(user_id, str(item["queue_id"]), WORKER_ID,
                        str(item["lease_token"]), db_path=db_path, lease_seconds=180)

    try:
        temporary_resume = _write_temporary_resume(user, resume, loaded_resume)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        _review(user_id, item, db_path, "missing_required_answer")
        return

    gate_state = {"start_attempted": False}

    def before_submit() -> object:
        """Recheck every mutable authorization immediately before the click."""
        try:
            fresh = _fresh_user(user_id)
            if fresh is None or not _ready(fresh):
                return "settings_changed"
            current_settings = control_store.get_settings(fresh)
            if current_settings["autonomy_mode"] != "submit_eligible":
                return "settings_changed"
            current_profile = control_store.get_profile(fresh)
            if (current_profile["version"] != profile["version"] or
                    current_profile["approved_version"] != current_profile["version"]):
                return "settings_changed"
            current_resume = _primary_resume(fresh)
            if (not current_resume or int(current_resume["document_id"]) != int(resume["document_id"]) or
                    current_resume.get("sha256") != resume.get("sha256")):
                return "settings_changed"
            if _daily_limit_reached(current_settings, db_path):
                return "daily_limit_reached"
            gate_state["start_attempted"] = True
            job_queue.start_application(user_id, str(item["queue_id"]), WORKER_ID,
                                        str(item["lease_token"]), db_path=db_path)
            return True
        except Exception:
            # If the ledger start had an uncertain outcome, require a human
            # check before any retry; the form click has not happened yet.
            return "uncertain_submission" if gate_state["start_attempted"] else "settings_changed"

    try:
        applied = browser_cdp.apply_public_page(
            application_url, control_store.STATE_DIR, user_id, plan,
            resume_path=str(temporary_resume), before_submit=before_submit,
            timeout_seconds=60, handoff_queue_id=str(item["queue_id"]),
        )
    finally:
        try:
            temporary_resume.unlink()
        except OSError:
            pass

    application_result = applied.get("application_result") if isinstance(applied, dict) else None
    if not isinstance(applied, dict) or not applied.get("ok") or not isinstance(application_result, dict):
        code = str(applied.get("error_code") or "") if isinstance(applied, dict) else ""
        if code in {"site_block", "http_error", "challenge", "captcha"}:
            _review(user_id, item, db_path, "site_block")
        elif code in {"invalid_url", "unsafe_redirect", "unsafe_destination", "blocked_url",
                      "non_public_host", "navigation_policy_blocked"}:
            _review(user_id, item, db_path, "source_access_unavailable")
        else:
            _review(user_id, item, db_path,
                    "uncertain_submission" if gate_state["start_attempted"] else "manual_application_required")
        return

    status = application_result.get("status")
    reason = str(application_result.get("reason") or "")
    if status == "submitted":
        job_queue.mark_confirmed_applied(
            user_id, str(item["queue_id"]), WORKER_ID, str(item["lease_token"]),
            confirmed=True, db_path=db_path,
        )
    elif status == "not_submitted" and reason in {"daily_limit_reached", "settings_changed"} and not gate_state["start_attempted"]:
        job_queue.release_unstarted(
            user_id, str(item["queue_id"]), WORKER_ID, str(item["lease_token"]),
            db_path=db_path,
            reason="daily_limit_reached" if reason == "daily_limit_reached" else "settings_changed",
        )
    else:
        allowed_reasons = {
            "captcha", "verification_challenge", "site_block", "missing_required_answer",
            "legal_attestation", "uncertain_submission", "possible_duplicate",
            "manual_application_required",
        }
        _review(user_id, item, db_path, reason if reason in allowed_reasons else "uncertain_submission")


def run_cycle() -> int:
    processed = 0
    browser_cdp.process_handoff_commands(control_store.STATE_DIR)
    if browser_cdp.refresh_live_handoff():
        _status("handoff")
        return 0
    for user in control_store.list_worker_users():
        if not _ready(user):
            continue
        user_id = str(user["user_id"])
        db_path = control_store.tenant_db_path(user)
        job_queue.recover_interrupted(user_id, db_path=db_path)
        try:
            settings = control_store.get_settings(user)
            if (settings["autonomy_mode"] == "submit_eligible" and
                    _daily_limit_reached(settings, db_path)):
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        item = job_queue.claim_next(user_id, WORKER_ID, db_path=db_path, lease_seconds=180)
        if item is None:
            continue
        _status("inspecting")
        try:
            _process(user, item, db_path)
        except InfrastructureBlocked as exc:
            LOG.error("Application browser unavailable: %s", exc.code)
            _status("error", exc.code)
            return processed
        except Exception as exc:
            # Avoid printing URLs, profile values, page content, or credentials.
            LOG.error("Application preparation failed: %s", type(exc).__name__)
            try:
                _fail(user_id, item, db_path, "adapter_error")
            except Exception:
                LOG.error("Could not release application lease; recovery will inspect it")
        processed += 1
        if browser_cdp.refresh_live_handoff():
            _status("handoff")
            return processed
    _status("running")
    return processed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one job per user")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    def shutdown(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    control_store.initialize()
    while True:
        try:
            count = run_cycle()
        except Exception as exc:
            LOG.error("Application worker cycle failed: %s", type(exc).__name__)
            _status("error", "cycle_failed")
            count = 0
        if args.once:
            return 0
        time.sleep(1 if count else 10)


if __name__ == "__main__":
    raise SystemExit(main())
