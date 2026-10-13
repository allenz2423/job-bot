#!/usr/bin/env python3
"""Local, containerized control UI for accounts, profiles, and the durable queue."""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    from . import application_store, browser_cdp, control_store, global_job_pool, job_queue, profile_ui, resume_parser, source_adapters
except ImportError:
    import application_store
    import browser_cdp
    import control_store
    import global_job_pool
    import job_queue
    import profile_ui
    import resume_parser
    import source_adapters


NETWORK_ACK = "I understand this exposes the job bot to my network"
COOKIE_NAME = "jobbot_session"
CSRF_COOKIE = "jobbot_csrf"
MAX_BODY = 4 * 1024 * 1024
MAX_UPLOAD_BODY = 6 * 1024 * 1024
PROFILE_SCRIPT = Path(__file__).resolve().parent / "static" / "profile.js"


def _application_worker_state() -> str:
    path = control_store.STATE_DIR / "application_worker_status.json"
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(str(status["updated_at"]).replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - updated).total_seconds() >= 45:
            return "offline"
        return str(status.get("state") or "error")
    except (OSError, ValueError, KeyError, TypeError):
        return "offline"


def _expected_profile(user: dict[str, object], form: dict[str, str]) -> tuple[dict[str, object], dict[str, object]]:
    try:
        expected = int(form.get("expected_version", ""))
    except ValueError as exc:
        raise ValueError("Reload the profile and try again") from exc
    profile = control_store.get_profile(user)
    if expected != profile["version"]:
        raise ValueError("The profile changed. Reload it before saving.")
    data = profile["data"]
    fields = data.get("fields", {}) if isinstance(data, dict) else {}
    return profile, dict(fields)


def _merge_resume_suggestions(fields: dict[str, object], text: str) -> None:
    for field_id, suggestion in resume_parser.suggest_profile_fields(text).items():
        current = fields.get(field_id)
        if current is None or (isinstance(current, dict) and current.get("status") == "unknown"):
            fields[field_id] = suggestion


def e(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _source_label(source: object) -> str:
    if source == "Adzuna":
        return '<a class="source-credit" href="https://www.adzuna.co.uk/" target="_blank" rel="noopener noreferrer">Adzuna</a>'
    return e(source)


def _review_reason_label(reason: object) -> str:
    labels = {
        "manual_application_required": "Complete on the employer site, then confirm here",
        "source_access_unavailable": "Automated page access unavailable; open the saved URL",
        "captcha": "CAPTCHA or human verification needed",
        "verification_challenge": "Sign-in or verification needed",
        "site_block": "Site blocked automated access",
        "collection_page": "Search or collection page; no individual job confirmed",
        "auto_apply_not_authorized": "Legacy host authorization pause; requeue this item to resume",
        "missing_required_answer": "A required answer is missing or ambiguous",
        "legal_attestation": "Legal attestation or sensitive authorization needs a person",
        "uncertain_submission": "Submission outcome uncertain; inspect the site before retrying",
        "possible_duplicate": "The page may show an existing application; inspect before retrying",
    }
    return labels.get(str(reason), str(reason).replace("_", " "))


def _cookie(name: str, value: str, *, clear: bool = False) -> str:
    item = cookies.SimpleCookie()
    item[name] = value
    item[name]["path"] = "/"
    item[name]["httponly"] = True
    item[name]["samesite"] = "Strict"
    if clear:
        item[name]["max-age"] = 0
    return item.output(header="").strip()


def _csrf_form(token: str) -> str:
    return f'<input type="hidden" name="csrf" value="{e(token)}">'


def _layout(title: str, body: str, user: dict[str, object] | None = None,
            csrf: str = "") -> str:
    if user:
        nav = (
            '<nav><a href="/">Dashboard</a><a href="/queue">Apply queue</a><a href="/pool">Global pool</a>'
            '<a href="/profile">Profile</a><a href="/settings">Settings</a>'
            '<a href="/password">Password</a>'
            + ('<a href="/users">Users</a>' if user["role"] == "owner" else "")
            + f'<form method="post" action="/logout">{_csrf_form(csrf)}<button type="submit">Sign out</button></form></nav>'
        )
        badge = f'<span class="account">{e(user["email"])}</span>'
    else:
        nav, badge = "", ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)} · Job Bot</title>
<style>
:root {{ color-scheme: light; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background:#f4f6fb; color:#15233b; }}
* {{ box-sizing:border-box; }} body {{ margin:0; }} header {{ background:#12233e; color:white; padding:18px max(20px,calc((100vw - 1100px)/2)); display:flex; justify-content:space-between; align-items:center; gap:16px; }}
header strong {{ font-size:20px; letter-spacing:.02em; }} .account {{ color:#c9d7f1; font-size:14px; }}
nav {{ background:#dfe8f7; padding:10px max(20px,calc((100vw - 1100px)/2)); display:flex; gap:18px; align-items:center; flex-wrap:wrap; }}
nav a {{ color:#17395e; font-weight:650; text-decoration:none; }} nav form {{ margin-left:auto; }}
main {{ max-width:1100px; margin:30px auto; padding:0 20px 70px; }} h1 {{ margin:0 0 14px; font-size:28px; }} h2 {{ margin:0 0 14px; font-size:20px; }} h3 {{ margin:0 0 10px; font-size:16px; }}
.muted {{ color:#526178; }} .card {{ background:white; border:1px solid #dce3ef; border-radius:14px; padding:22px; margin:18px 0; box-shadow:0 2px 8px #1e35540a; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px; }} .metric {{ background:#eef3fb; border-radius:10px; padding:18px; }} .metric strong {{ display:block; font-size:26px; }}
label {{ display:block; font-weight:650; margin:12px 0 5px; }} input, select, textarea {{ width:100%; border:1px solid #aebdd3; border-radius:7px; padding:10px; font:inherit; background:#fff; color:#15233b; }}
input[type=checkbox] {{ width:auto; margin-right:8px; }} button,.button {{ display:inline-block; border:0; border-radius:8px; padding:10px 16px; background:#2557a7; color:white; font:inherit; font-weight:650; cursor:pointer; text-decoration:none; }}
button.secondary {{ background:#dfe8f7; color:#17395e; }} button.danger {{ background:#984c31; }} form.inline {{ display:inline-block; margin:2px; }}
table {{ border-collapse:collapse; width:100%; background:white; }} th,td {{ border-bottom:1px solid #e2e8f1; text-align:left; padding:10px; vertical-align:top; }} th {{ color:#4a5b74; font-size:13px; }}
.scroll {{ overflow:auto; }} .pill {{ display:inline-block; border-radius:99px; background:#e4ecf9; padding:4px 9px; font-size:12px; font-weight:700; }}
.notice {{ background:#fff4df; border:1px solid #dfc47e; padding:14px; border-radius:8px; }} .error {{ background:#ffebe7; border:1px solid #d88f80; padding:14px; border-radius:8px; }}
.profile-section {{ padding:0; }} .profile-section > details > summary {{ cursor:pointer; list-style:none; display:flex; justify-content:space-between; align-items:center; padding:20px 22px; font-size:19px; font-weight:700; }}
.profile-section > details > summary::-webkit-details-marker {{ display:none; }} .profile-section > details > summary small {{ color:#526178; font-size:13px; }} .profile-section > details > p {{ margin:0 22px 16px; }}
.profile-field {{ padding:15px 0; border-top:1px solid #e2e8f1; margin:0 22px; }} .profile-field > label {{ margin-top:0; }}
.profile-actions {{ position:sticky; top:0; z-index:5; display:flex; align-items:center; gap:14px; background:#f4f6fb; padding:10px 0; border-bottom:1px solid #dce3ef; }} .profile-actions span {{ color:#526178; font-size:13px; }}
.blank-choice {{ margin:8px 0 0; font-size:13px; color:#526178; }} .blank-choice summary {{ cursor:pointer; width:max-content; }} .blank-choice select {{ max-width:230px; margin-top:7px; }}
.record-list {{ display:grid; gap:12px; }} .record-card {{ background:#f7f9fd; border:1px solid #d6e0ee; border-radius:9px; padding:14px; }} .record-top {{ display:flex; justify-content:space-between; align-items:center; }} .record-top button {{ padding:6px 10px; font-size:12px; }}
.record-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:8px 14px; }} .record-field:has(textarea) {{ grid-column:1/-1; }} .record-field label {{ font-size:14px; }} .add-record {{ margin-top:10px; }}
.legacy-resume pre {{ white-space:pre-wrap; overflow-wrap:anywhere; max-height:320px; overflow:auto; background:#f7f9fd; padding:12px; border-radius:7px; }}
.uploaded-resume details > summary {{ cursor:pointer; color:#2557a7; font-weight:650; }} .uploaded-resume h4 {{ margin:0 0 8px; }}
.help {{ font-size:13px; color:#526178; margin:4px 0 10px; }} code {{ background:#eef3fb; padding:2px 4px; border-radius:3px; }}
.source-credit {{ display:inline-flex; min-width:116px; min-height:23px; align-items:center; justify-content:center; border-radius:5px; background:#eef3fb; color:#17395e; font-weight:700; text-decoration:underline; }}
</style></head><body><header><strong>Job Bot</strong>{badge}</header>{nav}<main>{body}</main></body></html>"""


def _notice(message: str, *, error: bool = False) -> str:
    return f'<p class="{"error" if error else "notice"}">{e(message)}</p>'


def _login_page(message: str = "") -> str:
    body = '<h1>Sign in</h1><div class="card"><form method="post" action="/login">'
    if message:
        body += _notice(message, error=True)
    body += ('<label for="email">Email</label><input id="email" name="email" type="email" required autocomplete="username">'
             '<label for="password">Password</label><input id="password" name="password" type="password" required autocomplete="current-password">'
             '<p><button type="submit">Sign in</button></p></form></div>')
    return _layout("Sign in", body)


def _setup_page(message: str = "") -> str:
    body = '<h1>Create the owner account</h1><p class="muted">The setup token is stored in the container at <code>/app/state/bootstrap_token</code>.</p>'
    body += '<div class="card"><form method="post" action="/setup">'
    if message:
        body += _notice(message, error=True)
    body += ('<label for="token">Setup token</label><input id="token" name="token" type="password" required autocomplete="off">'
             '<label for="email">Email</label><input id="email" name="email" type="email" required autocomplete="username">'
             '<label for="password">Password (at least 12 characters)</label><input id="password" name="password" type="password" required minlength="12" autocomplete="new-password">'
             '<p><button type="submit">Create account</button></p></form></div>')
    return _layout("Setup", body)


def _dashboard(user: dict[str, object], csrf: str) -> str:
    db_path = control_store.initialize_tenant(user)
    queue = job_queue.list_queue(str(user["user_id"]), db_path=db_path)
    counts = Counter(str(item["state"]) for item in queue)
    applications = application_store.list_applications(db_path)
    profile = control_store.get_profile(user)
    settings = control_store.get_settings(user)
    approved = bool(profile["version"] and profile["version"] == profile["approved_version"])
    today_utc = datetime.now(timezone.utc).date()
    submitted_today = 0
    for application in applications:
        if application.get("status") == "in_progress":
            submitted_today += 1
            continue
        stamp = str(application.get("submitted_at") or "")
        if stamp:
            try:
                submitted = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if submitted.tzinfo is None:
                    submitted = submitted.replace(tzinfo=timezone.utc)
                submitted_today += submitted.astimezone(timezone.utc).date() == today_utc
            except ValueError:
                pass
    body = '<h1>Dashboard</h1><p class="muted">Your private job workspace on this computer.</p>'
    body += '<div class="grid">'
    for label, count in (("Jobs queued", counts["pending"] + counts["retry_wait"]), ("Need review", counts["needs_review"]),
                         ("Confirmed applications", sum(a["status"] == "applied" for a in applications))):
        body += f'<div class="metric"><strong>{count}</strong>{e(label)}</div>'
    body += '</div>'
    body += '<section class="card"><h2>Current setup</h2><p>'
    body += f'Profile: <span class="pill">{"Approved" if approved else "Needs approval"}</span> &nbsp; '
    body += f'Discovery: <span class="pill">{"Paused" if settings["discovery_paused"] else "Enabled in settings"}</span> &nbsp; '
    worker_state = _application_worker_state()
    if worker_state == "offline":
        application_state = "Worker offline"
    elif worker_state == "error":
        application_state = "Worker error; check container logs"
    elif worker_state == "handoff":
        application_state = "Human verification needed in the application browser"
    elif settings["application_paused"]:
        application_state = "Paused"
    elif settings["autonomy_mode"] == "discovery_only":
        application_state = "Waiting: discovery only mode"
    elif not approved:
        application_state = "Waiting: approve profile"
    elif not any(doc["purpose"] == "primary" and doc["approved_at"] and int(doc["text_chars"]) > 0
                 for doc in control_store.list_resume_documents(user)):
        application_state = "Waiting: approve a readable primary resume"
    elif (settings["autonomy_mode"] == "submit_eligible" and
          int(settings["daily_application_limit"]) != -1 and
          submitted_today >= int(settings["daily_application_limit"])):
        application_state = "Daily application limit reached (UTC)"
    elif counts["leased"]:
        application_state = "Inspecting a queued job"
    elif not counts["pending"] and not counts["retry_wait"]:
        application_state = "No runnable jobs; review queue ready"
    elif not counts["pending"] and counts["retry_wait"]:
        application_state = "Waiting for scheduled retry"
    else:
        application_state = "Preparing queued jobs"
    body += f'Applications: <span class="pill">{e(application_state)}</span></p>'
    body += '<p class="notice">Submit eligible mode can send simple applications for queued jobs using approved profile facts and the approved primary resume. Missing answers, attestations, verification challenges, site blocks, and uncertain outcomes go to review. Only an employer confirmation is counted as applied.</p>'
    body += '<p><a class="button" href="/queue">Open apply queue</a> <a class="button" href="/profile">Edit profile</a></p></section>'
    if queue:
        body += '<section class="card"><h2>Recent jobs</h2><div class="scroll"><table><thead><tr><th>Role</th><th>Company</th><th>State</th><th>Job URL (identifier)</th></tr></thead><tbody>'
        for item in queue[:10]:
            body += f'<tr><td>{e(item["role"] or "Untitled role")}</td><td>{e(item["company"] or "Unknown company")}<br><small>Source: {_source_label(item["job_source"])}</small></td><td>{e(item["state"])}</td><td><a href="{e(item["job_url"])}" target="_blank" rel="noopener noreferrer">{e(item["job_url"])}</a></td></tr>'
        body += '</tbody></table></div></section>'
    return _layout("Dashboard", body, user, csrf)


def _queue_page(user: dict[str, object], csrf: str, message: str = "") -> str:
    db_path = control_store.initialize_tenant(user)
    queue = job_queue.list_queue(str(user["user_id"]), db_path=db_path)
    body = '<h1>Apply queue</h1><p class="muted">Add a job URL here or move one from the global pool. Full URLs are stored exactly as entered, including query parameters.</p>'
    active_handoff = None
    active_item = None
    for item in queue:
        handoff = browser_cdp.live_handoff_info(
            control_store.STATE_DIR, str(user["user_id"]), str(item["queue_id"]),
        )
        if handoff:
            active_handoff, active_item = handoff, item
            break
    if active_handoff and active_item:
        history_url = f'/queue/item/{e(active_item["queue_id"])}'
        body += '<section class="card"><h2>Application browser is waiting</h2>'
        body += f'<p>{e(active_item["role"] or "Untitled role")} · {e(active_item["company"] or "Unknown company")}</p>'
        body += f'<p><a class="button" href="{e(active_handoff["novnc_url"])}" target="_blank" rel="noopener noreferrer">Open browser view</a></p>'
        body += f'<p class="muted">VNC uses a temporary password only; find it on <a href="{history_url}">this job’s history page</a>.</p></section>'
    if message:
        body += _notice(message)
    body += f'''<section class="card"><h2>Add a job to the apply queue</h2><p class="muted">Discovery adds eligible listings automatically. You can also add one directly by its full job URL; manual entries stay private unless you choose to share them.</p><form method="post" action="/queue/add">{_csrf_form(csrf)}
<label>Full job URL</label><input type="url" name="job_url" required maxlength="4000">
<p class="muted">Company and role are optional for queueing, and are required before unattended submission can proceed.</p><div class="grid"><div><label>Company</label><input name="company" maxlength="300"></div><div><label>Role</label><input name="role" maxlength="500"></div>
<div><label>Source</label><input name="job_source" maxlength="200"></div><div><label>Employer job ID</label><input name="employer_job_id" maxlength="300"></div>
<div><label>Location</label><input name="location" maxlength="300"></div></div>
<label><input name="share_public" type="checkbox" value="yes">Share this public listing in the global pool for other accounts</label>
<p><button type="submit">Add to apply queue</button></p></form></section>'''
    body += '<section class="card"><h2>Jobs waiting for application work</h2>'
    if not queue:
        body += '<p class="muted">No jobs have been added yet.</p>'
    else:
        body += '<div class="scroll"><table><thead><tr><th>Job</th><th>Match</th><th>State</th><th>History</th><th>Action</th></tr></thead><tbody>'
        for item in queue:
            url = e(item["job_url"])
            role = e(item["role"] or "Untitled role")
            company = e(item["company"] or "Unknown company")
            body += f'<tr><td><strong>{role}</strong><br>{company}<br><small>Source: {_source_label(item["job_source"])}</small><br>Job URL (identifier): <a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a></td>'
            if item.get("match_score") is None:
                body += f'<td>Not ranked<br><small>{e(item.get("next_action", ""))}</small></td>'
            else:
                body += f'<td>{float(item["match_score"]):g}/100 advisory<br><small>{e(item.get("next_action", ""))}</small></td>'
            body += f'<td><span class="pill">{e(item["state"])}</span><br><small>{e(_review_reason_label(item["review_reason"]))}</small></td>'
            queue_item_url = f'/queue/item/{e(item["queue_id"])}'
            body += f'<td>{len(item["job_urls"])} URL(s)<br>{int(item["attempt_count"])} attempt(s)<br><a href="{queue_item_url}">View history</a>'
            body += '</td><td>'
            if item["state"] == "needs_review":
                body += f'<form method="post" action="/queue/review">{_csrf_form(csrf)}<input type="hidden" name="queue_id" value="{e(item["queue_id"])}">'
                body += '<label>Review decision</label><select name="decision">'
                if item["application_key"]:
                    body += '<option value="approve">Approve for queue</option>'
                body += '<option value="skip">Skip</option><option value="duplicate">Duplicate</option>'
                if item["application_key"] and (item["application_status"] == "in_progress" or
                                                item["review_reason"] in job_queue.MANUAL_SUBMISSION_REASONS):
                    body += '<option value="submitted">Submission confirmed on site</option>'
                if item["application_key"] and item["application_status"] == "in_progress":
                    body += '<option value="not_submitted">No submission on site</option>'
                body += '</select>'
                if not item["application_key"]:
                    body += '<small>Job identity conflicts with a saved record. You can skip it here; approving requires identity resolution.</small>'
                body += '<label><input type="checkbox" name="confirmed" value="yes">I completed or inspected this job on the employer site before confirming an outcome</label><button class="secondary" type="submit">Save decision</button></form>'
            elif item["state"] == "paused":
                if item["last_error"] == "invalid_url_rescrape_requested":
                    body += 'Waiting for URL check'
                else:
                    body += f'<form method="post" action="/queue/resume">{_csrf_form(csrf)}<input type="hidden" name="queue_id" value="{e(item["queue_id"])}"><button class="secondary" type="submit">Resume queue item</button></form>'
            else:
                body += '—'
            body += '</td></tr>'
        body += '</tbody></table></div>'
    body += '</section>'
    return _layout("Jobs", body, user, csrf)


def _queue_item_page(user: dict[str, object], csrf: str, queue_id: str) -> str:
    db_path = control_store.tenant_db_path(user)
    item = job_queue.get_queue_item(str(user["user_id"]), queue_id, db_path=db_path)
    events = job_queue.queue_events(str(user["user_id"]), queue_id, db_path=db_path, limit=100)
    body = f'<p><a href="/queue">← Jobs</a></p><h1>{e(item["role"] or "Untitled role")}</h1>'
    body += f'<p>{e(item["company"] or "Unknown company")} · {e(item["state"])} · {int(item["attempt_count"])} attempt(s) · Source: {_source_label(item["job_source"])}</p>'
    if item["review_reason"]:
        body += f'<p class="notice">Review reason: {e(_review_reason_label(item["review_reason"]))}</p>'
    handoff = browser_cdp.live_handoff_info(
        control_store.STATE_DIR, str(user["user_id"]), queue_id,
    )
    if handoff:
        body += '<section class="card"><h2>Live application browser</h2>'
        body += '<p class="notice">The application browser is paused for a human verification step. Solve the CAPTCHA or verification in the browser yourself; the bot will not attempt to bypass it.</p>'
        body += f'<p><a class="button" href="{e(handoff["novnc_url"])}" target="_blank" rel="noopener noreferrer">Open browser view</a></p>'
        body += f'<p>VNC viewer address: <code>{e(handoff["vnc_address"])}</code><br>Temporary password: <code>{e(handoff["password"])}</code></p>'
        body += '<p>After solving verification, return here. Approve the job to retry from its saved URL. If the application was already started, inspect its outcome and choose the matching submission decision.</p>'
        body += f'<form method="post" action="/queue/handoff/close">{_csrf_form(csrf)}<input type="hidden" name="queue_id" value="{e(queue_id)}"><button class="secondary" type="submit">Close browser and keep this job in review</button></form>'
        body += '</section>'
    if item["pool_id"]:
        pooled = global_job_pool.get_job(str(item["pool_id"]))
        state = pooled["rescrape_status"] or "available" if pooled else "unavailable"
        body += f'<p>Global pool: <span class="pill">{e(state)}</span></p>'
        if item["state"] == "paused" and item["last_error"] == "invalid_url_rescrape_requested":
            body += f'<form method="post" action="/queue/rescrape">{_csrf_form(csrf)}<input type="hidden" name="queue_id" value="{e(queue_id)}"><button class="secondary" type="submit">Retry URL check</button></form>'
    elif item["job_source"] != "Adzuna":
        body += f'<form method="post" action="/queue/share">{_csrf_form(csrf)}<input type="hidden" name="queue_id" value="{e(queue_id)}"><button class="secondary" type="submit">Share this public listing in the global pool</button></form>'
    body += '<section class="card"><h2>Saved job URLs (listing identifiers)</h2><p class="muted">A retry must inspect the site and this history before submitting again.</p><ul>'
    for url in item["job_urls"]:
        full_url = e(url["job_url"])
        body += f'<li><a href="{full_url}" target="_blank" rel="noopener noreferrer">{full_url}</a> {"(current)" if url["is_current"] else ""}</li>'
    body += '</ul></section><section class="card"><h2>Recent state changes</h2><p class="muted">Showing up to 100 most recent events.</p>'
    if events:
        body += '<div class="scroll"><table><thead><tr><th>Time</th><th>Event</th><th>Details</th></tr></thead><tbody>'
        for event in events:
            details = json.loads(event["detail_json"])
            body += f'<tr><td>{e(event["event_at"])}</td><td>{e(event["event_type"])}</td><td>{e(json.dumps(details, ensure_ascii=False))}</td></tr>'
        body += '</tbody></table></div>'
    else:
        body += '<p>No events recorded.</p>'
    body += '</section>'
    return _layout("Job history", body, user, csrf)


def _pool_page(user: dict[str, object], csrf: str) -> str:
    jobs = global_job_pool.list_jobs(limit=100)
    private_urls = {item["job_url"] for item in job_queue.list_queue(
        str(user["user_id"]), db_path=control_store.tenant_db_path(user))}
    body = '<h1>Global pool</h1><p class="muted">Public listings discovered by the workers are shared across accounts. Each account keeps its own application history, queue state, and profile. Search result links may still need job-page review.</p>'
    if not jobs:
        body += '<section class="card"><p>No public jobs have been discovered yet.</p></section>'
    else:
        body += '<section class="card"><div class="scroll"><table><thead><tr><th>Job</th><th>Source</th><th>URL check</th><th>Action</th></tr></thead><tbody>'
        for job in jobs:
            status = str(job["rescrape_status"] or "available")
            body += f'<tr><td><strong>{e(job["role"] or "Untitled role")}</strong><br>{e(job["company"] or "Unknown company")}<br>{e(job["location"])}</td>'
            source = _source_label("Adzuna") if job["source_kind"] == "adzuna" else e(job["source_kind"])
            body += f'<td>{source}</td><td><span class="pill">{e(status)}</span><br><a href="{e(job["job_url"])}" target="_blank" rel="noopener noreferrer">Open listing</a></td><td>'
            if job["job_url"] in private_urls:
                body += 'Already in your queue'
            elif status in {"pending", "claimed", "unresolved"}:
                body += 'Waiting for URL review'
            else:
                body += f'<form method="post" action="/pool/add">{_csrf_form(csrf)}<input type="hidden" name="pool_id" value="{e(job["pool_id"])}"><button type="submit">Add to my queue</button></form>'
            body += '</td></tr>'
        body += '</tbody></table></div></section>'
    return _layout("Global pool", body, user, csrf)


def _settings_page(user: dict[str, object], csrf: str, message: str = "") -> str:
    values = control_store.get_settings(user)
    sources = control_store.list_discovery_sources(user)
    searches = control_store.list_discovery_searches(user)
    adzuna = control_store.adzuna_status(user)
    interval_labels = {
        1: "1 minute", 5: "5 minutes", 10: "10 minutes", 15: "15 minutes",
        30: "30 minutes", 60: "1 hour", 120: "2 hours", 240: "4 hours",
        360: "6 hours", 720: "12 hours", 1440: "24 hours",
    }
    body = '<h1>Settings</h1><p class="notice">Discovery scores new listings against your saved searches and approved soft preferences. Automatic submission runs for queued jobs in Submit eligible mode with an approved profile and readable primary resume. Unknown required answers, attestations, verification, site blocks, and uncertain submissions go to review.</p>'
    if message:
        body += _notice(message)
    body += f'<section class="card"><form method="post" action="/settings">{_csrf_form(csrf)}'
    for key, label in (("discovery_paused", "Pause discovery"), ("application_paused", "Pause applications")):
        body += f'<label><input type="checkbox" name="{key}" value="1" {"checked" if values[key] else ""}>{label}</label>'
    body += '<label>Autonomy mode</label><select name="autonomy_mode">'
    for key, label in (("discovery_only", "Discovery only"), ("prepare_for_review", "Prepare for review"), ("submit_eligible", "Submit eligible automatically")):
        body += f'<option value="{key}" {"selected" if values["autonomy_mode"] == key else ""}>{label}</option>'
    body += '</select>'
    body += f'<label>Daily application limit</label><input name="daily_application_limit" type="number" min="-1" max="100" value="{int(values["daily_application_limit"])}"><p class="muted">Enter -1 for unlimited, or 1–100. A finite limit counts confirmed submissions and reserves capacity for unresolved submissions; it resets at 00:00 UTC.</p>'
    body += f'<label>Browser sessions per user</label><input name="browser_concurrency" type="number" min="1" max="8" value="{int(values["browser_concurrency"])}">'
    body += '<p><button type="submit">Save settings</button></p></form></section>'
    body += '<section class="card"><h2>Job searches</h2><p class="muted">Add each field or role you want the scraper to search for. These are search queries, not strict rules for ranking or applying. Location is optional.</p>'
    body += f'<p>Web search: <span class="pill">SearXNG enabled</span>; Adzuna: <span class="pill">{"enabled" if adzuna["enabled"] else "off"}</span>. Saved search phrases run up to every six hours, subject to provider limits.</p>'
    if searches:
        body += '<div class="scroll"><table><thead><tr><th>Search phrase</th><th>Location</th><th>Status</th><th>Controls</th></tr></thead><tbody>'
        for search in searches:
            search_id = int(search["search_id"])
            enabled = bool(search["enabled"])
            body += f'<tr><td><strong>{e(search["phrase"])}</strong></td><td>{e(search["location"] or "Anywhere")}</td><td>{"Enabled" if enabled else "Off"}</td><td>'
            body += f'<form class="inline" method="post" action="/settings/search/toggle">{_csrf_form(csrf)}<input type="hidden" name="search_id" value="{search_id}"><input type="hidden" name="enabled" value="{"0" if enabled else "1"}"><button class="secondary" type="submit">{"Disable" if enabled else "Enable"}</button></form>'
            body += f'<form class="inline" method="post" action="/settings/search/remove">{_csrf_form(csrf)}<input type="hidden" name="search_id" value="{search_id}"><button class="danger" type="submit">Remove</button></form></td></tr>'
        body += '</tbody></table></div>'
    else:
        body += '<p>No job searches added yet.</p>'
    body += f'<form method="post" action="/settings/search/add">{_csrf_form(csrf)}'
    body += '<label>Job field or search phrase</label><input name="phrase" maxlength="120" required placeholder="nursing, HPC engineering intern, SWE intern">'
    body += '<label>Location (optional)</label><input name="location" maxlength="120" placeholder="New York, remote, or blank for anywhere">'
    body += '<p><button type="submit">Add search</button></p></form></section>'
    body += '<section class="card"><h2>Web search</h2><p class="muted">Search phrases use the private SearXNG service included in Docker. It queries the enabled engines in your local SearXNG settings and does not need a search API key. Saved search phrases run up to every six hours, subject to shared request limits.</p></section>'
    body += '<section class="card"><h2>Adzuna search access</h2><p class="muted">Get an app ID and key from <a href="https://developer.adzuna.com/" target="_blank" rel="noopener noreferrer">Adzuna Developer</a>. The credentials are encrypted for your account and never shown again. Check its API terms for your use; a paid multi-user service may need a separate license.</p>'
    if adzuna["configured"]:
        body += f'<p>Saved for {e(str(adzuna["country"]).upper())}: <strong>{"Enabled" if adzuna["enabled"] else "Disabled"}</strong>. Enter both credentials below to replace them.</p>'
        body += f'<form class="inline" method="post" action="/settings/adzuna/toggle">{_csrf_form(csrf)}<input type="hidden" name="enabled" value="{"0" if adzuna["enabled"] else "1"}"><button class="secondary" type="submit">{"Disable" if adzuna["enabled"] else "Enable"}</button></form>'
        body += f'<form class="inline" method="post" action="/settings/adzuna/remove">{_csrf_form(csrf)}<button class="danger" type="submit">Remove credentials</button></form>'
    body += f'<form method="post" action="/settings/adzuna/save">{_csrf_form(csrf)}'
    body += '<label>Country</label><select name="country">'
    for code, label in (("us", "United States"), ("gb", "United Kingdom"), ("ca", "Canada"), ("au", "Australia")):
        body += f'<option value="{code}" {"selected" if adzuna["country"] == code else ""}>{label}</option>'
    body += '</select><label>App ID</label><input name="app_id" type="password" maxlength="200" required autocomplete="off">'
    body += '<label>App key</label><input name="app_key" type="password" maxlength="200" required autocomplete="off">'
    body += '<label><input name="rights_confirmed" type="checkbox" value="yes" required>I confirmed my Adzuna API access permits this use</label>'
    body += '<p><button type="submit">Save search access</button></p></form></section>'
    body += '<section class="card"><h2>Scrape jobs</h2><p class="muted">Each entry has its own target for new, unique jobs to add to your queue and its own wakeup interval. Use <strong>Run now</strong> to request an immediate scan. Web searches keep checking additional result pages until they reach the target or exhaust the configured titles, available results, or provider request budget. Duplicate URLs do not count toward the target. A source can finish below target when it does not return enough valid jobs or reaches a provider limit; the worker records the target and actual count in its logs. The scheduler checks once per minute. Search the web for these job titles, or add a permitted employer careers URL to read its public job pages. The application browser has a separate human-only VNC handoff for CAPTCHA or sign-in verification; discovery has no VNC session. The date limit applies when a listing supplies a posting date; undated listings can still appear.</p>'
    if sources:
        body += '<div class="scroll"><table><thead><tr><th>Allowed job titles</th><th>Target / URL</th><th>Target &amp; wakeup</th><th>Posting window</th><th>Status</th><th>Controls</th></tr></thead><tbody>'
        for source in sources:
            source_id = int(source["source_id"])
            title = e(str(source.get("job_title") or source.get("name") or ""))
            url = str(source.get("url") or "")
            url_display = f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">{e(url)}</a>' if url else '<span class="pill">Web search</span>'
            max_jobs = int(source.get("max_jobs", 25))
            source_interval = int(source.get("interval_minutes", 360))
            timespan = str(source.get("timespan", "any"))
            timespan_label = {
                "any": "Any time",
                "24h": "Past 24 hours",
                "3d": "Past 3 days",
                "7d": "Past 7 days",
                "14d": "Past 14 days",
                "30d": "Past 30 days",
            }.get(timespan, timespan)
            enabled = bool(source["enabled"])
            body += f'<tr><td><strong>{title}</strong></td><td>{url_display}</td><td><form method="post" action="/settings/source/update">{_csrf_form(csrf)}<input type="hidden" name="source_id" value="{source_id}">'
            body += f'<label>Target new jobs per cycle</label><input name="max_jobs" type="number" min="1" max="200" value="{max_jobs}" required>'
            body += '<label>Run every</label><select name="interval_minutes">'
            for minutes, label in interval_labels.items():
                body += f'<option value="{minutes}" {"selected" if source_interval == minutes else ""}>{label}</option>'
            body += f'</select><button class="secondary" type="submit">Save</button></form></td><td>{e(timespan_label)}</td><td>{"Enabled" if enabled else "Off"}</td><td>'
            body += f'<form class="inline" method="post" action="/settings/source/toggle">{_csrf_form(csrf)}<input type="hidden" name="source_id" value="{source_id}"><input type="hidden" name="enabled" value="{"0" if enabled else "1"}"><button class="secondary" type="submit">{"Disable" if enabled else "Enable"}</button></form>'
            body += f'<form class="inline" method="post" action="/settings/source/remove">{_csrf_form(csrf)}<input type="hidden" name="source_id" value="{source_id}"><button class="danger" type="submit">Remove</button></form>'
            if enabled:
                body += f'<form class="inline" method="post" action="/settings/source/run">{_csrf_form(csrf)}<input type="hidden" name="source_id" value="{source_id}"><button class="secondary" type="submit">Run now</button></form>'
            else:
                body += '<button class="secondary" type="button" disabled title="Enable this scrape job to run it">Run now</button>'
            body += '</td></tr>'
        body += '</tbody></table></div>'
    else:
        body += '<p>No scrape jobs added yet.</p>'
    body += f'<form method="post" action="/settings/source/add">{_csrf_form(csrf)}'
    body += '<label>Allowed job titles (these titles share one target and schedule)</label><textarea name="job_title" rows="2" maxlength="500" required placeholder="HPC Intern&#10;SWE Intern"></textarea>'
    body += '<label>Employer careers page or URL (optional)</label><input name="url" type="url" maxlength="2000" placeholder="https://example.com/careers (leave blank to search web)">'
    body += '<label>Target new jobs per cycle</label><input name="max_jobs" type="number" min="1" max="200" value="25" required>'
    body += '<label>Wakeup interval for this job</label><select name="interval_minutes">'
    for minutes, label in interval_labels.items():
        body += f'<option value="{minutes}" {"selected" if minutes == 360 else ""}>{label}</option>'
    body += '</select>'
    body += '<label>Posting date window (when known)</label><select name="timespan">'
    body += '<option value="7d" selected>Past 7 days</option>'
    body += '<option value="24h">Past 24 hours</option>'
    body += '<option value="3d">Past 3 days</option>'
    body += '<option value="14d">Past 14 days</option>'
    body += '<option value="30d">Past 30 days</option>'
    body += '<option value="any">Any time (no limit)</option>'
    body += '</select>'
    body += '<label><input name="rights_confirmed" type="checkbox" value="yes">If I entered an employer URL, I confirmed this site permits automated reading</label>'
    body += '<p><button type="submit">Add scrape job</button></p></form></section>'
    return _layout("Settings", body, user, csrf)


def _password_page(user: dict[str, object], csrf: str, message: str = "") -> str:
    body = '<h1>Change password</h1>'
    if user["must_change_password"]:
        body += _notice("Change the temporary password before using this account.")
    if message:
        body += _notice(message, error=True)
    body += f'''<section class="card"><form method="post" action="/password">{_csrf_form(csrf)}
<label>Current password</label><input name="old_password" type="password" required autocomplete="current-password">
<label>New password (at least 12 characters)</label><input name="new_password" type="password" minlength="12" required autocomplete="new-password">
<p><button type="submit">Change password</button></p></form></section>'''
    return _layout("Change password", body, user, csrf)


def _users_page(user: dict[str, object], csrf: str, temporary_password: str = "") -> str:
    body = '<h1>Users</h1><p class="muted">Each account has a separate profile, application ledger, and queue.</p>'
    if temporary_password:
        body += f'<div class="notice"><strong>One-time temporary password:</strong> <code>{e(temporary_password)}</code><br>Copy it now. It will not be shown again.</div>'
    body += f'''<section class="card"><h2>Add member</h2><form method="post" action="/users/add">{_csrf_form(csrf)}
<label>Email</label><input name="email" type="email" required maxlength="254"><p><button type="submit">Create member</button></p></form></section>'''
    body += '<section class="card"><h2>Accounts</h2><table><thead><tr><th>Email</th><th>Role</th><th>Created</th><th>Last sign in</th></tr></thead><tbody>'
    for item in control_store.list_users():
        body += f'<tr><td>{e(item["email"])}</td><td>{e(item["role"])}</td><td>{e(item["created_at"])}</td><td>{e(item["last_login_at"] or "—")}</td></tr>'
    body += '</tbody></table></section>'
    return _layout("Users", body, user, csrf)



class Handler(BaseHTTPRequestHandler):
    server_version = "JobBot/0.1"

    def log_message(self, format: str, *args: object) -> None:
        # Paths and query strings can contain user data; avoid access logs.
        return

    def _headers(self, status: int, content_type: str, length: int,
                 extra: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        for name, value in extra or []:
            self.send_header(name, value)
        self.end_headers()

    def _html(self, page: str, status: int = 200) -> None:
        body = page.encode("utf-8")
        self._headers(status, "text/html; charset=utf-8", len(body))
        self.wfile.write(body)

    def _redirect(self, path: str, cookies_out: list[str] | None = None) -> None:
        self._headers(303, "text/plain; charset=utf-8", 0,
                      [("Location", path)] + [("Set-Cookie", item) for item in cookies_out or []])

    def _request_cookies(self) -> dict[str, str]:
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return {}
        return {key: morsel.value for key, morsel in jar.items()}

    def _user(self) -> tuple[dict[str, object] | None, str, str]:
        jar = self._request_cookies()
        session_token = jar.get(COOKIE_NAME, "")
        user = control_store.session(session_token)
        csrf = jar.get(CSRF_COOKIE, "")
        if user and not control_store.valid_csrf(user, csrf):
            csrf = ""
        return user, session_token, csrf

    def _form(self) -> dict[str, str]:
        media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/x-www-form-urlencoded":
            raise ValueError("Form encoding is required")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid form length") from exc
        if not 0 <= length <= MAX_BODY:
            raise ValueError("Form is too large")
        body = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(body, keep_blank_values=True, max_num_fields=10000)
        return {key: values[-1] for key, values in parsed.items()}

    def _upload_form(self) -> tuple[dict[str, str], str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data;") or "\r" in content_type or "\n" in content_type:
            raise ValueError("File upload form encoding is required")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid upload length") from exc
        if not 0 < length <= MAX_UPLOAD_BODY:
            raise ValueError("Resume upload is too large")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("Incomplete resume upload")
        try:
            encoded_content_type = content_type.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("Invalid resume upload headers") from exc
        message = BytesParser(policy=policy.default).parsebytes(
            b"MIME-Version: 1.0\r\nContent-Type: " + encoded_content_type + b"\r\n\r\n" + body
        )
        if not message.is_multipart() or message.defects:
            raise ValueError("Invalid resume upload")
        form: dict[str, str] = {}
        filename = ""
        content = b""
        seen_file = False
        parts = list(message.iter_parts())
        if len(parts) > 8:
            raise ValueError("Too many upload fields")
        for part in parts:
            if part.is_multipart() or part.defects:
                raise ValueError("Invalid resume upload field")
            name = part.get_param("name", header="content-disposition")
            value = part.get_payload(decode=True)
            if not isinstance(value, bytes):
                raise ValueError("Invalid resume upload field")
            if name == "resume_file":
                if seen_file:
                    raise ValueError("Choose one resume file")
                seen_file = True
                filename = part.get_filename() or ""
                content = value
            elif name in {"csrf", "expected_version", "replace_text", "purpose", "display_name", "target_role_region"}:
                limit = 1500 if name in {"display_name", "target_role_region"} else 500
                if name in form or len(value) > limit:
                    raise ValueError("Invalid resume upload field")
                form[name] = value.decode("utf-8")
            else:
                raise ValueError("Unexpected resume upload field")
        if not filename or not content:
            raise ValueError("Choose a resume file")
        return form, filename, content

    def do_GET(self) -> None:
        try:
            self._do_GET()
        except Exception as exc:
            print(f"web request failed: {type(exc).__name__}", file=sys.stderr)
            self._html(_layout("Server error", '<h1>Server error</h1><p>Check the container logs for the error type.</p>'), 500)

    def _do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/static/profile.js":
            payload = PROFILE_SCRIPT.read_bytes()
            self._headers(200, "application/javascript; charset=utf-8", len(payload))
            self.wfile.write(payload)
            return
        if path == "/health":
            payload = b'{"status":"ok"}'
            self._headers(200, "application/json", len(payload))
            self.wfile.write(payload)
            return
        if control_store.needs_setup():
            if path != "/setup":
                self._redirect("/setup")
            else:
                self._html(_setup_page())
            return
        if path == "/setup":
            self._redirect("/login")
            return
        if path == "/login":
            self._html(_login_page())
            return
        user, _, csrf = self._user()
        if not user:
            self._redirect("/login")
            return
        if user["must_change_password"] and path != "/password":
            self._redirect("/password")
            return
        if path == "/":
            self._html(_dashboard(user, csrf))
        elif path == "/queue":
            self._html(_queue_page(user, csrf))
        elif path == "/pool":
            self._html(_pool_page(user, csrf))
        elif re.fullmatch(r"/queue/item/[0-9a-f]{32}", path):
            try:
                self._html(_queue_item_page(user, csrf, path.rsplit("/", 1)[-1]))
            except ValueError:
                self._html(_layout("Not found", '<h1>Job not found</h1>', user, csrf), 404)
        elif path == "/profile":
            notices = {
                "uploaded": "Resume uploaded. Review the extracted text and suggested fields before approving your profile.",
                "partial": "Resume uploaded, but some content may not have been read. Check the text below before approving your profile.",
                "no_text": "Resume uploaded, but no readable text was found. Paste the text below if this is a scanned file.",
                "saved": "Resume text saved. Review any suggested fields before approving your profile.",
                "cleared": "Saved resume text cleared. Uploaded files remain available below.",
                "metadata_saved": "Resume label and purpose saved. Review the file before approving it for future use.",
                "file_approved": "Resume file approved for future use. No application worker is connected yet.",
                "file_revoked": "Resume file approval revoked.",
            }
            code = parse_qs(urlsplit(self.path).query).get("resume", [""])[-1]
            notice = _notice(notices[code]) if code in notices else ""
            self._html(_layout("Candidate profile", notice + profile_ui.render_profile(user, csrf), user, csrf))
        elif re.fullmatch(r"/profile/resume/[1-9][0-9]{0,17}/download", path):
            document_id = int(path.split("/")[3])
            try:
                document = control_store.load_resume_document(user, document_id)
            except ValueError:
                self._html(_layout("Not found", '<h1>Resume not found</h1>', user, csrf), 404)
                return
            file_format = str(document["file_format"])
            content_type = {
                "pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "txt": "text/plain",
            }[file_format]
            content = document["content"]
            self._headers(200, content_type, len(content),
                          [("Content-Disposition", f'attachment; filename="resume.{file_format}"')])
            self.wfile.write(content)
        elif path == "/settings":
            run_code = parse_qs(urlsplit(self.path).query).get("run", [""])[-1]
            message = "Run requested. Discovery will pick it up on its next pass; provider limits still apply." if run_code == "queued" else ""
            self._html(_settings_page(user, csrf, message))
        elif path == "/password":
            self._html(_password_page(user, csrf))
        elif path == "/users" and user["role"] == "owner":
            self._html(_users_page(user, csrf))
        else:
            self._html(_layout("Not found", '<h1>Page not found</h1>', user, csrf), 404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/profile/resume/upload":
                form, upload_filename, upload_content = self._upload_form()
            else:
                form = self._form()
            if path == "/setup":
                if not control_store.needs_setup():
                    raise ValueError("Setup is closed")
                control_store.create_owner(form.get("token", ""), form.get("email", ""), form.get("password", ""))
                self._redirect("/login")
                return
            if path == "/login":
                result = control_store.login(form.get("email", ""), form.get("password", ""))
                if not result:
                    self._html(_login_page("Email or password is incorrect, or sign in is temporarily locked."), 401)
                    return
                token, csrf, user = result
                self._redirect("/password" if user["must_change_password"] else "/",
                               [_cookie(COOKIE_NAME, token), _cookie(CSRF_COOKIE, csrf)])
                return
            user, session_token, csrf = self._user()
            if not user:
                self._redirect("/login")
                return
            if not csrf or not form.get("csrf") or not control_store.valid_csrf(user, form["csrf"]):
                self._html(_layout("Invalid request", '<h1>Invalid request</h1><p>Reload the page and try again.</p>', user, csrf), 403)
                return
            if user["must_change_password"] and path not in {"/password", "/logout"}:
                self._redirect("/password")
                return
            db_path = control_store.tenant_db_path(user)
            user_id = str(user["user_id"])
            if path == "/logout":
                control_store.logout(session_token)
                self._redirect("/login", [_cookie(COOKIE_NAME, "", clear=True), _cookie(CSRF_COOKIE, "", clear=True)])
            elif path == "/password":
                control_store.change_password(int(user["user_id"]), form.get("old_password", ""), form.get("new_password", ""))
                self._redirect("/login", [_cookie(COOKIE_NAME, "", clear=True), _cookie(CSRF_COOKIE, "", clear=True)])
            elif path == "/queue/add":
                payload = {key: form.get(key, "") for key in ("job_url", "job_source", "employer_job_id", "company", "role", "location")}
                pool_id = ""
                if form.get("share_public") == "yes":
                    if str(payload["job_source"]) == "Adzuna":
                        raise ValueError("Adzuna results cannot be shared without redistribution rights")
                    listing = source_adapters.JobListing(
                        str(payload["job_url"]), str(payload["role"]), str(payload["company"]),
                        str(payload["location"]), "")
                    pool_id = str(global_job_pool.upsert_listing(listing, "manual")["pool_id"])
                job_queue.enqueue_job(user_id, payload, db_path=db_path, pool_id=pool_id)
                self._redirect("/queue")
            elif path == "/pool/add":
                job_queue.enqueue_from_global_pool(user_id, form.get("pool_id", ""), db_path=db_path)
                self._redirect("/queue")
            elif path == "/queue/review":
                decision = form.get("decision", "")
                queue_id = form.get("queue_id", "")
                job_queue.resolve_review(user_id, queue_id, decision, db_path=db_path,
                                         confirmed=(form.get("confirmed") == "yes"))
                browser_cdp.request_handoff_release(
                    control_store.STATE_DIR, user_id, queue_id,
                )
                self._redirect("/queue")
            elif path == "/queue/handoff/close":
                queue_id = form.get("queue_id", "")
                if not browser_cdp.request_handoff_release(
                        control_store.STATE_DIR, user_id, queue_id):
                    raise ValueError("No live application browser is open for this job")
                self._redirect("/queue/item/" + queue_id)
            elif path == "/queue/resume":
                job_queue.resume_paused(user_id, form.get("queue_id", ""), db_path=db_path)
                self._redirect("/queue")
            elif path == "/queue/rescrape":
                job_queue.retry_owned_rescrape(user_id, form.get("queue_id", ""), db_path=db_path)
                self._redirect("/queue/item/" + form.get("queue_id", ""))
            elif path == "/queue/share":
                job_queue.share_owned_job(user_id, form.get("queue_id", ""), db_path=db_path)
                self._redirect("/queue/item/" + form.get("queue_id", ""))
            elif path == "/settings":
                control_store.save_settings(user, {
                    "discovery_paused": "discovery_paused" in form,
                    "application_paused": "application_paused" in form,
                    "autonomy_mode": form.get("autonomy_mode", ""),
                    "daily_application_limit": form.get("daily_application_limit", ""),
                    "browser_concurrency": form.get("browser_concurrency", ""),
                })
                self._redirect("/settings")
            elif path == "/settings/source/add":
                job_title = (form.get("job_title") or form.get("name") or "").strip()
                if not job_title:
                    raise ValueError("Enter a job title to scrape")
                raw_max = form.get("max_jobs", "25").strip()
                try:
                    max_jobs = int(raw_max)
                except ValueError:
                    raise ValueError("Target job count must be a whole number") from None
                raw_interval = form.get("interval_minutes", "360").strip()
                try:
                    interval_minutes = int(raw_interval)
                except ValueError:
                    raise ValueError("Choose a supported scrape interval") from None
                timespan = form.get("timespan", "any").strip()
                if timespan not in {"24h", "3d", "7d", "14d", "30d", "any"}:
                    timespan = "any"
                url = form.get("url", "").strip()
                if url and form.get("rights_confirmed") != "yes":
                    raise ValueError("Confirm that the employer URL permits automated reading")
                source_type = "job_page" if url else "scrape_jobs"
                control_store.add_discovery_source(
                    user,
                    name=job_title,
                    url=url,
                    job_title=job_title,
                    max_jobs=max_jobs,
                    interval_minutes=interval_minutes,
                    timespan=timespan,
                    source_type=source_type,
                    permitted=not url or form.get("rights_confirmed") == "yes",
                    enabled=True,
                )
                self._redirect("/settings")
            elif path == "/settings/source/update":
                control_store.update_discovery_source_limits(
                    user, form.get("source_id", ""), form.get("max_jobs", ""),
                    form.get("interval_minutes", ""))
                self._redirect("/settings")
            elif path == "/settings/source/run":
                control_store.request_discovery_source_run(user, form.get("source_id", ""))
                self._redirect("/settings?run=queued")
            elif path == "/settings/source/toggle":
                enabled = form.get("enabled", "")
                if enabled not in {"0", "1"}:
                    raise ValueError("Invalid source setting")
                control_store.set_discovery_source_enabled(
                    user, form.get("source_id", ""), enabled == "1")
                self._redirect("/settings")
            elif path == "/settings/source/remove":
                control_store.remove_discovery_source(user, form.get("source_id", ""))
                self._redirect("/settings")
            elif path == "/settings/search/add":
                control_store.add_discovery_search(user, form.get("phrase", ""), form.get("location", ""))
                self._redirect("/settings")
            elif path == "/settings/search/toggle":
                enabled = form.get("enabled", "")
                if enabled not in {"0", "1"}:
                    raise ValueError("Invalid search setting")
                control_store.set_discovery_search_enabled(
                    user, form.get("search_id", ""), enabled == "1")
                self._redirect("/settings")
            elif path == "/settings/search/remove":
                control_store.remove_discovery_search(user, form.get("search_id", ""))
                self._redirect("/settings")
            elif path == "/settings/adzuna/save":
                control_store.save_adzuna_credentials(
                    user, form.get("app_id", ""), form.get("app_key", ""),
                    form.get("country", ""), rights_confirmed=form.get("rights_confirmed") == "yes")
                self._redirect("/settings")
            elif path == "/settings/adzuna/toggle":
                enabled = form.get("enabled", "")
                if enabled not in {"0", "1"}:
                    raise ValueError("Invalid Adzuna setting")
                control_store.set_adzuna_enabled(user, enabled == "1")
                self._redirect("/settings")
            elif path == "/settings/adzuna/remove":
                control_store.remove_adzuna_credentials(user)
                self._redirect("/settings")
            elif path == "/profile/save":
                profile, fields = _expected_profile(user, form)
                control_store.save_profile(user, profile_ui.parse_profile_form(form, fields),
                                           expected_version=int(profile["version"]))
                self._redirect("/profile")
            elif path == "/profile/resume/upload":
                profile, fields = _expected_profile(user, form)
                original_fields = dict(fields)
                purpose = form.get("purpose", "")
                if purpose not in {"primary", "targeted"}:
                    raise ValueError("Choose a primary or targeted resume")
                parsed = resume_parser.extract_resume(upload_filename, upload_content)
                extracted_text = str(parsed["text"])
                control_store.save_resume_document(user, str(parsed["filename"]), str(parsed["format"]),
                                                   upload_content, extracted_text, purpose=purpose,
                                                   display_name=form.get("display_name", ""),
                                                   target_role_region=form.get("target_role_region", ""))
                previous = fields.get("materials.master_resume")
                has_text = (isinstance(previous, dict) and previous.get("status") == "known" and
                            bool(previous.get("value")))
                if purpose == "primary" and extracted_text and (not has_text or form.get("replace_text") == "yes"):
                    fields["materials.master_resume"] = {
                        "status": "known", "reuse_policy": "auto_use", "value": extracted_text,
                    }
                if extracted_text:
                    _merge_resume_suggestions(fields, extracted_text)
                if fields != original_fields:
                    control_store.save_profile(user, fields, expected_version=int(profile["version"]))
                outcome = "partial" if extracted_text and parsed.get("warning") else "uploaded" if extracted_text else "no_text"
                self._redirect("/profile?resume=" + outcome)
            elif re.fullmatch(r"/profile/resume/[1-9][0-9]{0,17}/metadata", path):
                try:
                    expected = int(form.get("expected_metadata_version", ""))
                except ValueError as exc:
                    raise ValueError("Reload the profile and try again") from exc
                document_id = int(path.split("/")[3])
                control_store.update_resume_document_metadata(
                    user, document_id, purpose=form.get("purpose", ""),
                    display_name=form.get("display_name", ""),
                    target_role_region=form.get("target_role_region", ""),
                    expected_metadata_version=expected,
                )
                self._redirect("/profile?resume=metadata_saved")
            elif re.fullmatch(r"/profile/resume/[1-9][0-9]{0,17}/approve", path):
                try:
                    expected = int(form.get("expected_metadata_version", ""))
                except ValueError as exc:
                    raise ValueError("Reload the profile and try again") from exc
                document_id = int(path.split("/")[3])
                control_store.approve_resume_document(user, document_id, expected_metadata_version=expected)
                self._redirect("/profile?resume=file_approved")
            elif re.fullmatch(r"/profile/resume/[1-9][0-9]{0,17}/revoke", path):
                try:
                    expected = int(form.get("expected_metadata_version", ""))
                except ValueError as exc:
                    raise ValueError("Reload the profile and try again") from exc
                document_id = int(path.split("/")[3])
                control_store.revoke_resume_document_approval(user, document_id,
                                                              expected_metadata_version=expected)
                self._redirect("/profile?resume=file_revoked")
            elif path == "/profile/resume/text":
                profile, fields = _expected_profile(user, form)
                original_fields = dict(fields)
                resume_text = form.get("resume_text", "").strip()
                if len(resume_text) > 100000:
                    raise ValueError("Resume text is too long")
                if resume_text:
                    fields["materials.master_resume"] = {
                        "status": "known", "reuse_policy": "auto_use", "value": resume_text,
                    }
                    _merge_resume_suggestions(fields, resume_text)
                else:
                    fields.pop("materials.master_resume", None)
                if fields != original_fields:
                    control_store.save_profile(user, fields, expected_version=int(profile["version"]))
                self._redirect("/profile?resume=" + ("saved" if resume_text else "cleared"))
            elif path == "/profile/approve":
                try:
                    expected = int(form.get("expected_version", ""))
                except ValueError as exc:
                    raise ValueError("Reload the profile and try again") from exc
                control_store.approve_profile(user, expected_version=expected)
                self._redirect("/profile")
            elif path == "/users/add" and user["role"] == "owner":
                _, temporary_password = control_store.create_member(form.get("email", ""))
                self._html(_users_page(user, csrf, temporary_password))
            else:
                self._html(_layout("Not found", '<h1>Page not found</h1>', user, csrf), 404)
        except (ValueError, sqlite3.IntegrityError) as exc:
            user, _, csrf = self._user()
            if path == "/setup" and control_store.needs_setup():
                self._html(_setup_page(str(exc)), 400)
            elif path == "/login":
                self._html(_login_page("Sign in failed."), 400)
            elif user and (path == "/settings" or path.startswith("/settings/source/") or
                           path.startswith("/settings/search/") or path.startswith("/settings/adzuna/")):
                self._html(_settings_page(user, csrf, str(exc)), 400)
            elif user:
                body = f'<h1>Could not save</h1>{_notice(str(exc), error=True)}<p><a href="/">Return to dashboard</a></p>'
                self._html(_layout("Could not save", body, user, csrf), 400)
            else:
                self._html(_login_page("Sign in required."), 400)
        except Exception as exc:
            print(f"web request failed: {type(exc).__name__}", file=sys.stderr)
            self._html(_layout("Server error", '<h1>Server error</h1><p>Check the container logs for the error type.</p>'), 500)


def main() -> None:
    host_bind = os.environ.get("WEB_HOST_BIND", "127.0.0.1")
    if host_bind not in {"127.0.0.1", "localhost", "::1"}:
        if os.environ.get("WEB_REMOTE_BIND_ACK", "") != NETWORK_ACK:
            raise SystemExit("Network binding requires WEB_REMOTE_BIND_ACK with the exact warning phrase in DOCKER.md")
        print("WARNING: Job Bot is reachable from other devices on the published network interface.", file=sys.stderr)
    port = int(os.environ.get("WEB_PORT", "8080"))
    if not 1 <= port <= 65535:
        raise SystemExit("WEB_PORT must be between 1 and 65535")
    control_store.initialize()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Job Bot UI listening inside container on port {port}", file=sys.stderr)
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
