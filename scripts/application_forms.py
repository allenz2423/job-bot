"""Validate local-model field plans against approved candidate facts and DOM gates."""

from __future__ import annotations

import re
from typing import Any

try:
    from . import application_llm
except ImportError:
    import application_llm


_LEGAL_OR_HIGH_RISK = re.compile(
    r"\b(attest|certif(?:y|ication)|consent|agreement|terms and conditions|terms of service|"
    r"waiver|release|acknowledg(?:e)?ment|declaration|affirm|"
    r"electronic signature|e-sign|sign(?:ature)?|background check|criminal history|"
    r"social security|ssn|tax id|passport|bank account|routing number|date of birth|"
    r"medical|health diagnosis)\b",
    re.IGNORECASE,
)
_OPTIONAL_SELF_ID = re.compile(
    r"\b(race|ethnicity|gender|sex|disability|veteran status|sexual orientation)\b",
    re.IGNORECASE,
)
_SUBMIT_TEXTS = {
    "submit application", "submit your application", "send application",
    "send your application", "apply for this job",
}
_ALREADY_APPLIED_TEXTS = (
    "already applied", "application already received", "you have already applied",
    "thank you for applying", "thank you for your application",
    "your application has been submitted", "application submitted",
    "application received", "we have received your application", "successfully applied",
)
_EXCLUDED_FACT_PREFIXES = ("preferences.", "references.", "disclosures.",
                           "materials.", "demographics.")


def _norm(value: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def _approved_facts(fields: dict[str, Any]) -> list[dict[str, str]]:
    """Bound model context to explicit, approved values; never include resume text."""
    facts: list[dict[str, str]] = []
    remaining = 20000

    def add(fact_id: str, payload: object, label: str = "") -> None:
        nonlocal remaining
        if not isinstance(payload, dict) or payload.get("status") != "known" or payload.get("reuse_policy") != "auto_use":
            return
        raw = payload.get("value")
        values = raw if isinstance(raw, list) else [raw]
        if len(values) > 10:
            return
        for index, value in enumerate(values):
            if isinstance(value, bool):
                text = "Yes" if value else "No"
            elif isinstance(value, (str, int, float)):
                text = str(value).strip()
            else:
                continue
            if not text or len(text) > 1000 or len(text) > remaining:
                continue
            ident = f"{fact_id}[{index}]" if isinstance(raw, list) else fact_id
            facts.append({"id": ident, "label": (label or fact_id)[:160], "value": text})
            remaining -= len(text)

    for field_id, payload in fields.items():
        if not isinstance(field_id, str) or field_id.startswith(_EXCLUDED_FACT_PREFIXES):
            continue
        if not isinstance(payload, dict) or payload.get("status") != "known":
            continue
        value = payload.get("value")
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            if len(value) > 10:
                continue
            for index, record in enumerate(value):
                for child_id, child in record.items():
                    if child_id in {"manager_contact", "credential_number", "source_facts"}:
                        continue
                    if field_id == "answers.custom_bank" and child_id != "approved_answer":
                        continue
                    if field_id == "answers.custom_bank" and child_id == "approved_answer":
                        question = record.get("question", {})
                        label = str(question.get("value") or "") if isinstance(question, dict) else ""
                    else:
                        label = f"{field_id} {child_id} record {index + 1}"
                    add(f"{field_id}[{index}].{child_id}", child, label)
        else:
            add(field_id, payload)
    first = fields.get("identity.legal_first_name")
    last = fields.get("identity.legal_last_name")
    if all(isinstance(item, dict) and item.get("status") == "known" and
           item.get("reuse_policy") == "auto_use" and
           isinstance(item.get("value"), str) and item["value"].strip()
           for item in (first, last)):
        full_name = f"{first['value'].strip()} {last['value'].strip()}"
        if len(full_name) <= 300:
            facts.append({"id": "identity.full_name", "label": "Legal full name",
                          "value": full_name})
    return facts


def _model_field(field: dict[str, Any]) -> dict[str, Any]:
    return {key: str(field.get(key) or "")[:300] for key in ("label", "name", "id", "placeholder", "autocomplete", "type")} | {
        "dom_index": field.get("dom_index"), "required": field.get("required") is True,
        "options": [{"text": str(option.get("text") or "")[:200], "value": ""}
                    for option in field.get("options", []) if isinstance(option, dict)][:40],
    }


def _select_value(field: dict[str, Any], answer: str) -> str | None:
    options = field.get("options")
    if not isinstance(options, list):
        return None
    matches = [
        option for option in options if isinstance(option, dict) and
        _norm(option.get("text")) == _norm(answer) and
        isinstance(option.get("value"), str) and option.get("value") != ""
    ]
    return str(matches[0]["value"]) if len(matches) == 1 else None


def build_plan(profile: dict[str, Any], page: dict[str, Any], *,
               resume_extension: str = "", job_context: dict[str, str] | None = None) -> dict[str, Any]:
    """Return a safe DOM action plan or a stable handoff reason."""
    fields_payload = profile.get("data", {}).get("fields", {})
    if not isinstance(fields_payload, dict):
        return {"ready": False, "reason": "missing_required_answer"}
    if page.get("challenge_detected") or page.get("human_review_required"):
        signals = page.get("challenge_signals") or []
        if any("captcha" in str(value).lower() for value in signals):
            return {"ready": False, "reason": "captcha"}
        if any("block" in str(value).lower() for value in signals):
            return {"ready": False, "reason": "site_block"}
        return {"ready": False, "reason": "verification_challenge"}

    visible_text = _norm(page.get("visible_text"))
    if any(_norm(phrase) in visible_text for phrase in _ALREADY_APPLIED_TEXTS):
        return {"ready": False, "reason": "possible_duplicate"}

    forms = page.get("forms")
    buttons = page.get("buttons")
    if not isinstance(forms, list):
        return {"ready": False, "reason": "manual_application_required"}
    # Search and cookie preference forms often share a page with exactly one
    # application POST form. Ignore unrelated forms, but never choose between
    # multiple POST targets.
    post_forms = [form for form in forms if isinstance(form, dict) and
                  str(form.get("method") or "").lower() == "post" and
                  isinstance(form.get("fields"), list) and form["fields"]]
    if len(post_forms) != 1:
        return {"ready": False, "reason": "manual_application_required"}
    form = post_forms[0]
    if form.get("index", -1) < 0 or str(form.get("method", "")).lower() != "post":
        return {"ready": False, "reason": "manual_application_required"}
    raw_fields = form.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields or len(raw_fields) > 80:
        return {"ready": False, "reason": "manual_application_required"}

    labels = " ".join(
        " ".join(str(field.get(key) or "") for key in ("label", "name", "id", "placeholder"))
        for field in raw_fields if isinstance(field, dict)
    )
    if _LEGAL_OR_HIGH_RISK.search(labels):
        return {"ready": False, "reason": "legal_attestation"}

    sendable_fields: list[dict[str, Any]] = []
    file_fields: list[dict[str, Any]] = []
    for field in raw_fields:
        if not isinstance(field, dict) or field.get("disabled"):
            if isinstance(field, dict) and field.get("required"):
                return {"ready": False, "reason": "missing_required_answer"}
            continue
        label = " ".join(str(field.get(key) or "") for key in ("label", "name", "id", "placeholder"))
        if _OPTIONAL_SELF_ID.search(label):
            if field.get("required"):
                return {"ready": False, "reason": "missing_required_answer"}
            continue
        if field.get("read_only") or not field.get("visible"):
            if field.get("required"):
                return {"ready": False, "reason": "missing_required_answer"}
            continue
        if field.get("value_present"):
            return {"ready": False, "reason": "manual_application_required"}
        field_type = str(field.get("type", "")).lower()
        if field_type == "file":
            file_fields.append(field)
            continue
        if field_type in {"hidden", "button", "submit", "reset"}:
            continue
        if field_type in {"checkbox", "radio", "password", "image"}:
            if field.get("required") or field_type == "password":
                return {"ready": False, "reason": "missing_required_answer"}
            continue
        if field_type not in {"text", "email", "tel", "url", "search", "textarea", "number", "date", "select", "select-one"}:
            if field.get("required"):
                return {"ready": False, "reason": "missing_required_answer"}
            continue
        if not isinstance(field.get("dom_index"), int) or field["dom_index"] < 0:
            return {"ready": False, "reason": "manual_application_required"}
        sendable_fields.append(field)

    resume_extension = resume_extension.lower().lstrip(".")
    resume_dom_index: int | None = None
    if file_fields:
        if (len(file_fields) != 1 or resume_extension not in {"pdf", "docx", "txt"}):
            return {"ready": False, "reason": "missing_required_answer"}
        file_label = _norm(" ".join(
            str(file_fields[0].get(key) or "")
            for key in ("label", "name", "id", "placeholder", "autocomplete")
        ))
        if not re.search(r"\b(resume|cv|curriculum vitae)\b", file_label):
            return {"ready": False, "reason": "manual_application_required"}
        accepted = str(file_fields[0].get("accept") or "").lower()
        if accepted and not any(token in accepted for token in ("*/*", f".{resume_extension}",
                                                                  {"pdf": "application/pdf", "docx": "wordprocessingml", "txt": "text/plain"}[resume_extension])):
            return {"ready": False, "reason": "missing_required_answer"}
        else:
            resume_dom_index = file_fields[0].get("dom_index")
    elif any(field.get("required") and "resume" in _norm(field.get("label"))
             for field in raw_fields if isinstance(field, dict)):
        return {"ready": False, "reason": "missing_required_answer"}

    if not isinstance(buttons, list):
        return {"ready": False, "reason": "manual_application_required"}
    matching_buttons = [
        button for button in buttons if isinstance(button, dict) and
        _norm(button.get("text")) in _SUBMIT_TEXTS and not button.get("disabled")
    ]
    if len(matching_buttons) != 1:
        return {"ready": False, "reason": "manual_application_required"}
    signature = page.get("form_signature")
    if not isinstance(signature, str) or not re.fullmatch(r"[a-f0-9]{64}", signature):
        return {"ready": False, "reason": "manual_application_required"}

    facts = _approved_facts(fields_payload)
    fact_map = {fact["id"]: fact["value"] for fact in facts}
    fact_labels = {fact["id"]: fact["label"] for fact in facts}
    context = {key: str((job_context or {}).get(key) or "")[:200] for key in ("role", "company", "location")}
    try:
        model_plan = application_llm.plan_fields([_model_field(field) for field in sendable_fields], facts, context)
    except application_llm.ApplicationModelError as exc:
        return {"ready": False, "reason": "model_error", "model_code": exc.code}
    decisions = model_plan.get("fields") if isinstance(model_plan, dict) else None
    if not isinstance(decisions, list) or len(decisions) != len(sendable_fields):
        return {"ready": False, "reason": "model_error"}
    by_index = {field["dom_index"]: field for field in sendable_fields}
    if len(by_index) != len(sendable_fields):
        return {"ready": False, "reason": "manual_application_required"}
    planned_fields: list[dict[str, Any]] = []
    seen: set[int] = set()
    for decision in decisions:
        if not isinstance(decision, dict) or type(decision.get("dom_index")) is not int:
            return {"ready": False, "reason": "model_error"}
        index = decision["dom_index"]
        if index in seen or index not in by_index:
            return {"ready": False, "reason": "model_error"}
        seen.add(index)
        field = by_index[index]
        action = decision.get("action")
        if action in {"review", "skip_optional"}:
            if field.get("required"):
                return {"ready": False, "reason": "missing_required_answer"}
            continue
        answer = decision.get("answer")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 5000:
            return {"ready": False, "reason": "model_error"}
        if action == "use_fact":
            fact_id = decision.get("fact_id")
            if not isinstance(fact_id, str) or answer != fact_map.get(fact_id):
                return {"ready": False, "reason": "model_error"}
            if fact_id.startswith("answers.custom_bank[") and _norm(
                    field.get("label") or field.get("name")) != _norm(fact_labels.get(fact_id)):
                return {"ready": False, "reason": "missing_required_answer"}
            field_name = _norm(" ".join(str(field.get(key) or "") for key in
                                        ("label", "name", "id", "placeholder", "autocomplete")))
            if (answer in {"Yes", "No"} and fact_id.endswith(".current_role") and
                    not any(term in field_name for term in ("current role", "currently employed", "current position"))):
                return {"ready": False, "reason": "missing_required_answer"}
            if ("email" in field_name and any(token in field_name for token in
                                               ("school", "university", "academic", "student")) and
                    not any(token in fact_id for token in ("school_email", "university_email", "academic_email"))):
                return {"ready": False, "reason": "missing_required_answer"}
            topic_facts = (
                (r"\b(work authori[sz]ation|authori[sz]ed to work|visa|sponsor\w*|citizenship)\b", "answers.work_authorization"),
                (r"\b(relocat\w*|willing to move)\b", "answers.relocation"),
                (r"\b(salary|compensation|pay expectation)\b", "answers.compensation_wording"),
                (r"\b(prior employment|previously employed|previously applied)\b", "answers.prior_employment_application"),
                (r"\b(travel|shift|commute|overtime)\b", "answers.travel_shift_commute"),
            )
            for pattern, required_fact in topic_facts:
                if re.search(pattern, field_name) and fact_id != required_fact and not fact_id.startswith("answers.custom_bank["):
                    return {"ready": False, "reason": "missing_required_answer"}
        elif action == "draft":
            # A model-generated narrative is a suggestion, not a user-approved
            # reusable answer. Keep this job for review even when grounded.
            return {"ready": False, "reason": "missing_required_answer"}
        else:
            return {"ready": False, "reason": "model_error"}
        field_type = str(field.get("type") or "").lower()
        if field_type == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", answer):
            return {"ready": False, "reason": "missing_required_answer"}
        if field_type in {"select", "select-one"}:
            selected = _select_value(field, answer)
            if selected is None:
                return {"ready": False, "reason": "missing_required_answer"} if field.get("required") else {"ready": False, "reason": "model_error"}
            answer = selected
        elif field_type == "number":
            try:
                float(answer)
            except ValueError:
                return {"ready": False, "reason": "missing_required_answer"}
        elif field_type == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", answer):
            return {"ready": False, "reason": "missing_required_answer"}
        planned_fields.append({"dom_index": index, "value": answer})
    by_value = {item["dom_index"]: item["value"] for item in planned_fields}
    main_emails = [by_value[field["dom_index"]] for field in sendable_fields
                   if field["dom_index"] in by_value and
                   _norm(field.get("label") or field.get("name")) in {"email", "email address", "primary email"}]
    confirmations = [by_value[field["dom_index"]] for field in sendable_fields
                     if field["dom_index"] in by_value and
                     "email confirmation" in _norm(field.get("label") or field.get("name"))]
    if confirmations and (len(main_emails) != 1 or any(value != main_emails[0] for value in confirmations)):
        return {"ready": False, "reason": "missing_required_answer"}
    return {
        "ready": True,
        "form_signature": signature,
        "fields": planned_fields,
        "resume_dom_index": resume_dom_index,
        "submit_button_index": matching_buttons[0].get("dom_index"),
    }
