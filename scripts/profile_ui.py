"""Plain-language candidate profile form and its narrow form parser."""

from __future__ import annotations

import html
import re

try:
    from . import control_store, profile_schema
except ImportError:
    import control_store
    import profile_schema


_ASK_SECTIONS = {"references", "sensitive_disclosures"}
_ASK_FIELDS = {
    "answers.salary_history", "answers.background_drug_screening",
    "answers.conflicts_noncompete", "eligibility.clearance_eligibility",
    "eligibility.export_control_eligibility",
}


def _e(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _policy(section_id: str, field_id: str, *, sensitive_child: bool = False) -> str:
    if section_id in _ASK_SECTIONS or field_id in _ASK_FIELDS or sensitive_child:
        return "ask"
    return "auto_use"


def _display_value(definition: dict[str, object], value: object) -> str:
    if value is None:
        return ""
    if definition["type"] == "text_list" and isinstance(value, list):
        return "\n".join(str(item) for item in value)
    if definition["type"] == "boolean":
        return "true" if value is True else "false" if value is False else ""
    return str(value)


def _input(definition: dict[str, object], name: str, value: object) -> str:
    kind = str(definition["type"])
    shown = _display_value(definition, value)
    control_id = _e(name)
    input_name = _e(name)
    if kind in {"multiline", "text_list"}:
        rows = 6 if str(definition["id"]) == "materials.master_resume" else 3
        max_length = f' maxlength="{int(definition["max_length"])}"' if "max_length" in definition else ""
        placeholder = ' placeholder="One item per line"' if kind == "text_list" else ""
        return (f'<textarea id="{control_id}" name="{input_name}" rows="{rows}"{max_length}{placeholder}>'
                f'{_e(shown)}</textarea>')
    if kind == "boolean":
        return (f'<select id="{control_id}" name="{input_name}">'
                f'<option value="" {"selected" if shown == "" else ""}>Choose an answer</option>'
                f'<option value="true" {"selected" if shown == "true" else ""}>Yes</option>'
                f'<option value="false" {"selected" if shown == "false" else ""}>No</option></select>')
    if kind == "choice":
        result = f'<select id="{control_id}" name="{input_name}"><option value="">Choose an answer</option>'
        for choice in definition.get("choices", ()):
            result += f'<option value="{_e(choice)}" {"selected" if shown == choice else ""}>{_e(choice)}</option>'
        return result + '</select>'
    input_type = {"url": "url", "date": "date", "integer": "number", "number": "number"}.get(kind, "text")
    step = ' step="any"' if kind == "number" else ' step="1"' if kind == "integer" else ""
    max_length = f' maxlength="{int(definition["max_length"])}"' if "max_length" in definition else ""
    return (f'<input id="{control_id}" name="{input_name}" type="{input_type}"'
            f' value="{_e(shown)}"{step}{max_length}>')


def _blank_choice(field_id: str, current_status: str) -> str:
    status = current_status if current_status in {"not_applicable", "withheld"} else "unknown"
    opened = " open" if status != "unknown" else ""
    result = f'<details class="blank-choice"{opened}><summary>Leave blank as…</summary>'
    result += f'<select name="blank_{_e(field_id)}" aria-label="Blank answer status">'
    for key, label in (("unknown", "Unknown (default)"), ("not_applicable", "Not applicable"),
                       ("withheld", "Prefer not to answer")):
        result += f'<option value="{key}" {"selected" if status == key else ""}>{label}</option>'
    return result + '</select></details>'


def _record_card(definition: dict[str, object], index: int | str,
                 record: dict[str, object] | None = None) -> str:
    field_id = str(definition["id"])
    record = record or {}
    title = f'Entry {index + 1}' if isinstance(index, int) else 'New entry'
    body = '<div class="record-card"><div class="record-top">'
    body += f'<strong>{_e(title)}</strong><button type="button" class="secondary" data-remove-record>Remove</button></div>'
    body += '<div class="record-grid">'
    for child in definition["item_fields"]:
        child_id = str(child["id"])
        payload = record.get(child_id, {})
        value = payload.get("value") if isinstance(payload, dict) and payload.get("status") == "known" else None
        name = f'record_{field_id}_{index}_{child_id}'
        body += f'<div class="record-field"><label for="{_e(name)}">{_e(child["label"])}</label>'
        body += _input(child, name, value)
        if child.get("description"):
            body += f'<p class="help">{_e(child["description"])}</p>'
        if child["type"] == "text_list":
            body += '<p class="help">One item per line.</p>'
        body += '</div>'
    return body + '</div></div>'


def _record_field(definition: dict[str, object], current: dict[str, object]) -> str:
    field_id = str(definition["id"])
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", field_id)
    list_id = "records_" + safe_id
    template_id = "template_" + safe_id
    values = current.get("value", []) if current.get("status") == "known" else []
    records = values if isinstance(values, list) else []
    visible = records if records else [{}]
    body = f'<div id="{list_id}" class="record-list" data-next-index="{len(visible)}">'
    for index, item in enumerate(visible):
        body += _record_card(definition, index, item if isinstance(item, dict) else {})
    body += '</div>'
    body += f'<template id="{template_id}">{_record_card(definition, "__INDEX__")}</template>'
    body += f'<button type="button" class="secondary add-record" data-add-record="{list_id}" data-template-id="{template_id}">Add another</button>'
    return body


def _known_record_value(record: object, field_id: str) -> str:
    if not isinstance(record, dict):
        return ""
    payload = record.get(field_id)
    if isinstance(payload, dict) and payload.get("status") == "known":
        value = payload.get("value")
        return str(value) if value is not None else ""
    return ""


def _legacy_resume_versions(current: object) -> str:
    """Render saved role-specific resume text without offering the old editor."""
    if not isinstance(current, dict) or current.get("status") != "known":
        return ""
    records = current.get("value")
    if not isinstance(records, list) or not records:
        return ""
    body = '<div class="legacy-resumes"><h3>Previously saved resume versions</h3>'
    body += '<p class="help">These older entries are read-only. Upload a file above to manage resume versions.</p>'
    for index, record in enumerate(records, start=1):
        name = _known_record_value(record, "name")
        role_region = _known_record_value(record, "role_or_region")
        content = _known_record_value(record, "content")
        title = name or role_region or f"Resume version {index}"
        body += f'<details class="legacy-resume"><summary>{_e(title)}</summary>'
        if role_region:
            body += f'<p><strong>Role or country/region:</strong> {_e(role_region)}</p>'
        if content:
            body += f'<pre>{_e(content)}</pre>'
        body += '</details>'
    return body + '</div>'


def _resume_document_card(document: dict[str, object], csrf: str) -> str:
    document_id = int(document["document_id"])
    purpose = str(document.get("purpose", "unassigned"))
    if purpose not in {"primary", "targeted", "unassigned"}:
        purpose = "unassigned"
    purpose_label = {
        "primary": "Primary resume",
        "targeted": "Targeted resume",
        "unassigned": "Unassigned",
    }[purpose]
    filename = str(document.get("filename", "Resume file"))
    display_name = str(document.get("display_name") or "")
    target = str(document.get("target_role_region") or "")
    uploaded_at = str(document.get("uploaded_at", ""))
    approved_at = str(document.get("approved_at") or "")
    metadata_version = document.get("metadata_version", 0)
    try:
        metadata_version = max(0, int(metadata_version))
    except (TypeError, ValueError):
        metadata_version = 0

    purpose_id = f"resume_purpose_{document_id}"
    target_id = f"resume_target_{document_id}"
    name_id = f"resume_name_{document_id}"
    target_group_id = f"resume_target_group_{document_id}"
    body = '<article class="record-card uploaded-resume">'
    body += f'<h4>{_e(display_name or filename)}</h4>'
    body += (f'<p>{_e(purpose_label)} · Uploaded {_e(uploaded_at)} · '
             f'<a href="/profile/resume/{document_id}/download">Download file</a></p>')
    if target:
        body += f'<p><strong>Target:</strong> {_e(target)}</p>'
    if approved_at:
        body += f'<p><strong>Status:</strong> Approved on {_e(approved_at)}</p>'
    else:
        body += '<p><strong>Status:</strong> Not approved</p>'

    body += '<details><summary>Edit resume details</summary>'
    body += (f'<form method="post" action="/profile/resume/{document_id}/metadata">'
             f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
             f'<input type="hidden" name="expected_metadata_version" value="{metadata_version}">'
             f'<label for="{purpose_id}">Resume type</label>'
             f'<select id="{purpose_id}" name="purpose" data-resume-purpose data-target-input="{target_id}" '
             'required>')
    for value, label in (("unassigned", "Unassigned"), ("primary", "Primary resume"),
                         ("targeted", "Targeted resume")):
        body += f'<option value="{value}" {"selected" if purpose == value else ""}>{label}</option>'
    body += '</select>'
    body += (f'<label for="{name_id}">Display name (optional)</label>'
             f'<input id="{name_id}" name="display_name" maxlength="200" value="{_e(display_name)}">'
             f'<div id="{target_group_id}" data-resume-target-group>'
             f'<label for="{target_id}">Target role or country/region</label>'
             f'<input id="{target_id}" name="target_role_region" maxlength="300" value="{_e(target)}">'
             '<p class="help">Required for a targeted resume.</p></div>'
             '<p><button type="submit" class="secondary">Save resume details</button></p></form></details>')
    if not approved_at:
        body += (f'<form method="post" action="/profile/resume/{document_id}/approve">'
                 f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                 f'<input type="hidden" name="expected_metadata_version" value="{metadata_version}">'
                 '<button type="submit">Approve this resume</button></form>')
    else:
        body += (f'<form method="post" action="/profile/resume/{document_id}/revoke">'
                 f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                 f'<input type="hidden" name="expected_metadata_version" value="{metadata_version}">'
                 '<button type="submit" class="secondary">Revoke approval</button></form>')
    return body + '</article>'


def render_profile(user: dict[str, object], csrf: str) -> str:
    """Return the body of the profile page, ready for the shared layout."""
    profile = control_store.get_profile(user)
    data = profile["data"] if isinstance(profile["data"], dict) else {}
    fields = data.get("fields", {}) if isinstance(data.get("fields", {}), dict) else {}
    approved = bool(profile["version"] and profile["version"] == profile["approved_version"])
    resume_entry = fields.get("materials.master_resume", {})
    resume_text = (str(resume_entry.get("value", ""))
                   if isinstance(resume_entry, dict) and resume_entry.get("status") == "known" else "")
    documents = control_store.list_resume_documents(user)
    body = '<h1>Candidate profile</h1>'
    body += '<p class="muted">Fill in what you know. Leave the rest blank; it will stay unknown. You can save and return later.</p>'
    body += '<p class="muted">Approved facts can be used by a future application worker. Voluntary disclosures and reference details will still need your review.</p>'
    body += '<p class="help">Government IDs, bank details, and medical documents are not collected here.</p>'
    body += f'<p><span class="pill">Version {int(profile["version"])}</span> <span class="pill">{"Approved" if approved else "Draft"}</span></p>'
    body += '<section class="card"><h2>Resume</h2><p class="muted">Upload a PDF, DOCX, or text file (up to 5 MB), or paste the primary resume text. We extract readable text and suggest explicit contact details, a labeled summary, and listed skills for empty fields. Review the draft before approving it.</p>'
    body += '<p class="help">Uploaded files are stored for your review. No application worker currently uses or attaches them to applications.</p>'
    body += '<p class="help">Choosing a new primary resume moves the previous primary to Unassigned and clears its approval.</p>'
    body += (f'<form method="post" action="/profile/resume/upload" enctype="multipart/form-data">'
             f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
             f'<input type="hidden" name="expected_version" value="{int(profile["version"])}">'
             '<label for="resume_upload_purpose">Resume type</label>'
             '<select id="resume_upload_purpose" name="purpose" data-resume-purpose data-target-input="resume_upload_target" required>'
             '<option value="primary" selected>Primary resume</option>'
             '<option value="targeted">Targeted resume</option></select>'
             '<label for="resume_upload_name">Display name (optional)</label>'
             '<input id="resume_upload_name" name="display_name" maxlength="200">'
             '<div data-resume-target-group>'
             '<label for="resume_upload_target">Target role or country/region</label>'
             '<input id="resume_upload_target" name="target_role_region" maxlength="300">'
             '<p class="help">Required for a targeted resume.</p></div>'
             '<label for="resume_file">Upload a resume</label>'
             '<input id="resume_file" name="resume_file" type="file" accept=".pdf,.docx,.txt,application/pdf,text/plain,application/vnd.openxmlformats-officedocument.wordprocessingml.document" required>')
    if resume_text:
        body += '<label data-primary-only><input type="checkbox" name="replace_text" value="yes">Replace the saved primary resume text with text extracted from this file</label>'
    body += '<p><button type="submit">Upload and read resume</button></p></form>'
    body += (f'<form method="post" action="/profile/resume/text">'
             f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
             f'<input type="hidden" name="expected_version" value="{int(profile["version"])}">'
             '<label for="resume_text">Paste or edit resume text</label>'
             f'<textarea id="resume_text" name="resume_text" rows="9" maxlength="100000">{_e(resume_text)}</textarea>'
             '<p><button type="submit" class="secondary">Save resume text</button></p></form>')
    if documents:
        body += '<h3>Uploaded resume files</h3>'
        for document in documents:
            body += _resume_document_card(document, csrf)
    legacy_versions = _legacy_resume_versions(fields.get("materials.resume_versions"))
    if legacy_versions:
        body += legacy_versions
    body += '</section>'
    body += (f'<form method="post" action="/profile/save">'
             f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
             f'<input type="hidden" name="expected_version" value="{int(profile["version"])}">'
             '<div class="profile-actions"><button type="submit">Save changes</button><span>Blank fields stay unknown.</span></div>')
    for section_index, section in enumerate(profile_schema.PROFILE_SECTIONS):
        known = sum(fields.get(field["id"], {}).get("status") == "known"
                    for field in section["fields"]
                    if field["id"] not in {"materials.master_resume", "materials.resume_versions"})
        open_now = section_index == 0 or known > 0
        body += f'<section class="card profile-section"><details {"open" if open_now else ""}>'
        body += f'<summary><span>{_e(section["label"])}</span><small>{known} answered</small></summary>'
        if section.get("description"):
            body += f'<p class="muted">{_e(section["description"])}</p>'
        if section["id"] == "application_materials":
            body += '<p class="muted">Manage primary and targeted resumes in the Resume section above.</p>'
        for field in section["fields"]:
            field_id = str(field["id"])
            if field_id in {"materials.master_resume", "materials.resume_versions"}:
                continue
            current = fields.get(field_id, {})
            if not isinstance(current, dict):
                current = {}
            body += f'<div class="profile-field"><label for="value_{_e(field_id)}">{_e(field["label"])}</label>'
            if field.get("description"):
                body += f'<p class="help">{_e(field["description"])}</p>'
            if field["type"] == "records":
                body += _record_field(field, current)
            else:
                value = current.get("value") if current.get("status") == "known" else None
                body += _input(field, "value_" + field_id, value)
                if field["type"] == "text_list":
                    body += '<p class="help">One item per line.</p>'
            body += _blank_choice(field_id, str(current.get("status", "unknown")))
            body += '</div>'
        body += '</details></section>'
    body += '<p><button type="submit">Save changes</button></p></form>'
    if profile["version"] and not approved:
        body += (f'<form method="post" action="/profile/approve">'
                 f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                 f'<input type="hidden" name="expected_version" value="{int(profile["version"])}">'
                 '<button class="secondary" type="submit">Approve saved profile</button></form>')
    body += '<script src="/static/profile.js" defer></script>'
    return body


def _parse_value(definition: dict[str, object], raw: str) -> object:
    kind = str(definition["type"])
    if kind == "text_list":
        return [line.strip() for line in raw.splitlines() if line.strip()]
    if kind == "boolean":
        if raw not in {"true", "false"}:
            raise ValueError(f"{definition['label']} needs Yes or No")
        return raw == "true"
    if kind == "integer":
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{definition['label']} needs a whole number") from exc
    if kind == "number":
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"{definition['label']} needs a number") from exc
    return raw


def parse_profile_form(form: dict[str, str], existing_fields: dict[str, object] | None = None) -> dict[str, object]:
    """Infer known/unknown from values; keep reuse policy internal."""
    parsed: dict[str, object] = {}
    for section in profile_schema.PROFILE_SECTIONS:
        section_id = str(section["id"])
        for definition in section["fields"]:
            field_id = str(definition["id"])
            if field_id in {"materials.master_resume", "materials.resume_versions"}:
                continue
            policy = _policy(section_id, field_id)
            value: object | None = None
            if definition["type"] == "records":
                prefix = f"record_{field_id}_"
                indices: set[int] = set()
                for key in form:
                    if key.startswith(prefix):
                        remainder = key[len(prefix):]
                        index_text = remainder.split("_", 1)[0]
                        if index_text.isdecimal():
                            indices.add(int(index_text))
                if len(indices) > int(definition["max_items"]):
                    raise ValueError(f"{definition['label']} has too many entries")
                records: list[dict[str, object]] = []
                for index in sorted(indices):
                    record: dict[str, object] = {}
                    for child in definition["item_fields"]:
                        child_id = str(child["id"])
                        raw = form.get(f"{prefix}{index}_{child_id}", "").strip()
                        if raw:
                            record[child_id] = {
                                "status": "known",
                                "reuse_policy": _policy(section_id, field_id, sensitive_child=bool(child["sensitive"])),
                                "value": _parse_value(child, raw),
                            }
                    if record:
                        records.append(record)
                if records:
                    value = records
            else:
                raw = form.get("value_" + field_id, "").strip()
                if raw:
                    value = _parse_value(definition, raw)
            if value is not None:
                parsed[field_id] = {"status": "known", "reuse_policy": policy, "value": value}
                continue
            blank = form.get("blank_" + field_id, "unknown")
            if blank not in {"unknown", "not_applicable", "withheld"}:
                raise ValueError("Invalid blank answer status")
            if blank != "unknown":
                parsed[field_id] = {"status": blank, "reuse_policy": "ask"}
    if existing_fields:
        for field_id in ("materials.master_resume", "materials.resume_versions"):
            if field_id in existing_fields:
                parsed[field_id] = existing_fields[field_id]
    return parsed
