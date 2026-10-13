"""Candidate profile field catalog and strict JSON validation.

This module defines the profile payload used by the web control UI. It
does not store profile data or decide whether an application may proceed; the
caller must require an approved profile version before reuse. High-risk
onboarding data (government/tax identifiers, banking details, identity
documents, and health diagnoses) is intentionally absent from the catalog.
"""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Any


SCHEMA_VERSION = 1
MAX_PROFILE_BYTES = 1_000_000
MAX_JSON_DEPTH = 12
PROFILE_STATUSES = ("known", "unknown", "not_applicable", "withheld")
REUSE_POLICIES = ("auto_use", "ask", "never")
APPROVAL_STATES = ("draft", "approved")


def _field(
    field_id: str,
    label: str,
    value_type: str = "text",
    *,
    max_length: int = 1000,
    sensitive: bool = False,
    description: str = "",
    choices: tuple[str, ...] = (),
    item_fields: tuple[dict[str, Any], ...] = (),
    max_items: int = 50,
    item_max_length: int = 500,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": field_id,
        "label": label,
        "type": value_type,
        "sensitive": sensitive,
        "description": description,
    }
    if value_type in {"text", "multiline", "url"}:
        result["max_length"] = max_length
    if value_type == "choice":
        result["choices"] = choices
    if value_type == "text_list":
        result["max_items"] = max_items
        result["item_max_length"] = item_max_length
    if value_type == "records":
        result["item_fields"] = item_fields
        result["max_items"] = max_items
    return result


def _record_field(
    field_id: str,
    label: str,
    value_type: str = "text",
    **options: Any,
) -> dict[str, Any]:
    """Define a leaf field inside a repeatable record."""
    return _field(field_id, label, value_type, **options)


def _section(
    section_id: str,
    label: str,
    fields: tuple[dict[str, Any], ...],
    *,
    sensitive: bool = False,
    description: str = "",
) -> dict[str, Any]:
    return {
        "id": section_id,
        "label": label,
        "sensitive": sensitive,
        "description": description,
        "fields": fields,
    }


_CONTACT_METHODS = ("email", "phone", "text", "other")
_PROFICIENCY = ("beginner", "intermediate", "advanced", "fluent", "native")

_LINK_FIELDS = (
    _record_field("kind", "Link type", "choice", choices=("LinkedIn", "GitHub", "Portfolio", "Personal site", "Work sample", "Other")),
    _record_field("url", "URL", "url", max_length=2048),
    _record_field("label", "Label", max_length=200),
)
_EMPLOYMENT_FIELDS = (
    _record_field("employer", "Employer", max_length=300),
    _record_field("department", "Department", max_length=300),
    _record_field("title", "Title", max_length=300),
    _record_field("location", "Location", max_length=300),
    _record_field("work_arrangement", "Work arrangement", max_length=100),
    _record_field("employment_type", "Employment type", max_length=100, description="Examples include employee, contract, internship, self-employment, military service, or volunteering."),
    _record_field("start_date", "Start date", "date"),
    _record_field("end_date", "End date", "date"),
    _record_field("current_role", "Current role", "boolean"),
    _record_field("responsibilities", "Responsibilities", "multiline", max_length=12000),
    _record_field("accomplishments", "Accomplishments and outcomes", "multiline", max_length=16000),
    _record_field("tools", "Technologies and tools", "text_list", max_items=100, item_max_length=200),
    _record_field("manager_contact", "Manager or contact details", "multiline", max_length=2000, sensitive=True),
    _record_field("permission_to_contact", "Permission to contact employer", "boolean", sensitive=True),
    _record_field("rehire_status", "Rehire status", max_length=500, sensitive=True),
    _record_field("reason_for_leaving", "Reason for leaving", "multiline", max_length=2000, sensitive=True),
)
_EDUCATION_FIELDS = (
    _record_field("school", "School or provider", max_length=300),
    _record_field("location", "Location", max_length=300),
    _record_field("start_date", "Start date", "date"),
    _record_field("end_date", "End date", "date"),
    _record_field("completion_status", "Completion status", max_length=100),
    _record_field("credential", "Degree or credential", max_length=300),
    _record_field("field_of_study", "Field of study", max_length=300),
    _record_field("gpa", "GPA", "number"),
    _record_field("grading_scale", "Grading scale", max_length=100),
    _record_field("honors", "Honors", "text_list", max_items=50, item_max_length=300),
    _record_field("coursework", "Relevant coursework", "text_list", max_items=100, item_max_length=300),
    _record_field("training_type", "Training type", max_length=100, description="Examples include bootcamp, apprenticeship, or other training."),
)
_SKILL_FIELDS = (
    _record_field("skill", "Skill", max_length=200),
    _record_field("proficiency", "Proficiency", "choice", choices=_PROFICIENCY),
    _record_field("years_experience", "Years of experience", "number"),
    _record_field("last_used", "Last used", "date"),
    _record_field("supporting_role_or_project", "Supporting role or project", max_length=500),
    _record_field("tools_and_methods", "Tools and methods", "text_list", max_items=50, item_max_length=200),
)
_LANGUAGE_FIELDS = (
    _record_field("language", "Language", max_length=100),
    _record_field("spoken_proficiency", "Spoken proficiency", "choice", choices=_PROFICIENCY),
    _record_field("written_proficiency", "Written proficiency", "choice", choices=_PROFICIENCY),
)
_EXPERIENCE_FIELDS = (
    _record_field("title", "Title", max_length=300),
    _record_field("organization", "Organization", max_length=300),
    _record_field("description", "Description", "multiline", max_length=10000),
    _record_field("date", "Date", "date"),
    _record_field("url", "URL", "url", max_length=2048),
)
_LICENSE_FIELDS = (
    _record_field("name", "License or certification", max_length=300),
    _record_field("issuer", "Issuer", max_length=300),
    _record_field("jurisdiction", "Jurisdiction", max_length=200),
    _record_field("issue_date", "Issue date", "date"),
    _record_field("expiry_date", "Expiry date", "date"),
    _record_field("status", "Status", max_length=100),
    _record_field("credential_number", "Credential number", max_length=300, sensitive=True),
)
_Q_AND_A_FIELDS = (
    _record_field("question", "Original question", "multiline", max_length=4000),
    _record_field("approved_answer", "Approved answer", "multiline", max_length=10000),
    _record_field("source_facts", "Supporting profile facts", "text_list", max_items=30, item_max_length=500),
    _record_field("last_confirmed", "Date last confirmed", "date"),
)
_REFERENCE_FIELDS = (
    _record_field("name", "Name", max_length=300, sensitive=True),
    _record_field("role", "Role", max_length=300, sensitive=True),
    _record_field("organization", "Organization", max_length=300, sensitive=True),
    _record_field("relationship", "Relationship", max_length=300, sensitive=True),
    _record_field("email", "Email", max_length=320, sensitive=True),
    _record_field("phone", "Phone", max_length=100, sensitive=True),
    _record_field("preferred_contact_method", "Preferred contact method", "choice", choices=_CONTACT_METHODS, sensitive=True),
    _record_field("agreed_to_be_contacted", "Agreed to be contacted", "boolean", sensitive=True),
)
_ANSWER_BANK_FIELDS = (
    _record_field("question", "Original question", "multiline", max_length=4000),
    _record_field("approved_answer", "User-approved answer", "multiline", max_length=10000, sensitive=True),
    _record_field("source_facts", "Supporting profile facts", "text_list", max_items=30, item_max_length=500),
    _record_field("last_confirmed", "Date last confirmed", "date"),
)


PROFILE_SECTIONS: tuple[dict[str, Any], ...] = (
    _section("identity_contact", "Identity and contact", (
        _field("identity.legal_first_name", "Legal first name", max_length=200, sensitive=True),
        _field("identity.middle_name", "Middle name", max_length=200, sensitive=True),
        _field("identity.legal_last_name", "Legal last name", max_length=200, sensitive=True),
        _field("identity.suffix", "Suffix", max_length=40, sensitive=True),
        _field("identity.preferred_name", "Preferred name", max_length=200),
        _field("identity.pronouns", "Pronouns", max_length=100),
        _field("contact.email_addresses", "Email addresses", "text_list", sensitive=True, max_items=20, item_max_length=320),
        _field("contact.phone_numbers", "Phone numbers", "text_list", sensitive=True, max_items=20, item_max_length=100),
        _field("contact.current_address", "Current address", "multiline", max_length=1200, sensitive=True),
        _field("contact.mailing_address", "Mailing address", "multiline", max_length=1200, sensitive=True),
        _field("contact.country", "Country", max_length=100, sensitive=True),
        _field("contact.time_zone", "Time zone", max_length=100),
        _field("contact.preferred_method", "Preferred contact method", "choice", choices=_CONTACT_METHODS, sensitive=True),
        _field("contact.profile_links", "Professional profile links", "records", item_fields=_LINK_FIELDS, max_items=30),
    ), sensitive=True, description="Personal contact details and approved professional links."),
    _section("application_materials", "Application materials", (
        _field("materials.master_resume", "Master resume", "multiline", max_length=100000, sensitive=True),
        _field("materials.resume_versions", "Role- or country-specific resumes", "records", item_fields=(
            _record_field("name", "Version name", max_length=200),
            _record_field("role_or_region", "Role or country/region", max_length=300),
            _record_field("content", "Resume content", "multiline", max_length=100000, sensitive=True),
        ), max_items=20, sensitive=True),
        _field("materials.professional_summary", "Professional summary", "multiline", max_length=12000),
        _field("materials.headline", "Headline", max_length=1000),
        _field("materials.objectives", "Career objectives", "multiline", max_length=8000),
        _field("materials.cover_letter_templates", "Cover-letter templates", "records", item_fields=(
            _record_field("name", "Template name", max_length=200),
            _record_field("role_or_region", "Role or country/region", max_length=300),
            _record_field("content", "Template content", "multiline", max_length=40000),
        ), max_items=30),
        _field("materials.writing_samples", "Writing samples", "records", item_fields=_EXPERIENCE_FIELDS, max_items=50),
        _field("materials.publications", "Publications", "records", item_fields=_EXPERIENCE_FIELDS, max_items=100),
        _field("materials.patents", "Patents", "records", item_fields=_EXPERIENCE_FIELDS, max_items=100),
        _field("materials.presentations", "Presentations", "records", item_fields=_EXPERIENCE_FIELDS, max_items=100),
        _field("materials.portfolio_urls", "Portfolio and work-sample URLs", "text_list", max_items=100, item_max_length=2048),
    ), sensitive=True),
    _section("employment_history", "Employment history", (
        _field("employment.positions", "Positions and other experience", "records", item_fields=_EMPLOYMENT_FIELDS, max_items=100, sensitive=True,
               description="Include employment, internships, contract work, self-employment, military service, volunteering, or career breaks when chosen."),
    ), sensitive=True),
    _section("education_training", "Education and training", (
        _field("education.records", "Education and training", "records", item_fields=_EDUCATION_FIELDS, max_items=100, sensitive=True),
    ), sensitive=True),
    _section("skills_evidence", "Skills and evidence", (
        _field("skills.records", "Skills", "records", item_fields=_SKILL_FIELDS, max_items=300),
        _field("skills.languages", "Languages", "records", item_fields=_LANGUAGE_FIELDS, max_items=100),
        _field("skills.awards", "Awards", "records", item_fields=_EXPERIENCE_FIELDS, max_items=100),
        _field("skills.projects", "Projects", "records", item_fields=_EXPERIENCE_FIELDS, max_items=100),
        _field("skills.leadership", "Leadership", "records", item_fields=_EXPERIENCE_FIELDS, max_items=100),
        _field("skills.community_work", "Community work", "records", item_fields=_EXPERIENCE_FIELDS, max_items=100),
        _field("skills.memberships", "Professional memberships", "records", item_fields=_EXPERIENCE_FIELDS, max_items=100),
    )),
    _section("licenses_certifications", "Licenses and certifications", (
        _field("licenses.records", "Licenses and certifications", "records", item_fields=_LICENSE_FIELDS, max_items=100, sensitive=True),
        _field("licenses.driver_class", "Driver's-license class", max_length=100, sensitive=True),
        _field("licenses.travel_availability", "Travel or vehicle availability", max_length=500),
    ), sensitive=True),
    _section("work_authorization", "Work authorization and eligibility", (
        _field("eligibility.authorized_countries", "Countries authorized to work in", "text_list", sensitive=True, max_items=100, item_max_length=100),
        _field("eligibility.status_wording", "User-selected authorization status wording", "multiline", max_length=2000, sensitive=True,
               description="Use only wording explicitly confirmed by the user; never infer citizenship or immigration status."),
        _field("eligibility.sponsorship_now", "Sponsorship needed now", "boolean", sensitive=True),
        _field("eligibility.sponsorship_later", "Sponsorship needed later", "boolean", sensitive=True),
        _field("eligibility.authorization_expiry", "Authorization expiry date", "date", sensitive=True),
        _field("eligibility.clearance_eligibility", "User-confirmed clearance eligibility", "multiline", max_length=1000, sensitive=True),
        _field("eligibility.export_control_eligibility", "User-confirmed export-control eligibility", "multiline", max_length=1000, sensitive=True),
    ), sensitive=True, description="Treat as sensitive; only save wording and facts the user explicitly confirms."),
    _section("job_preferences", "Job preferences", (
        _field("preferences.target_titles", "Preferred titles", "text_list", max_items=100, item_max_length=200),
        _field("preferences.excluded_titles", "Titles to avoid (soft preference)", "text_list", max_items=100, item_max_length=200,
               description="These lower a match's rank. Put a title in Hard dealbreakers only if it must be excluded."),
        _field("preferences.seniority", "Preferred seniority", "text_list", max_items=30, item_max_length=100),
        _field("preferences.industries", "Industries", "text_list", max_items=100, item_max_length=200),
        _field("preferences.employers", "Preferred employers", "text_list", max_items=100, item_max_length=300),
        _field("preferences.locations", "Preferred locations", "text_list", max_items=100, item_max_length=300),
        _field("preferences.work_arrangement", "Remote, hybrid, or onsite preference", "text_list", max_items=10, item_max_length=100),
        _field("preferences.commute_radius", "Preferred commute radius", max_length=200),
        _field("preferences.relocation", "Relocation willingness", max_length=500),
        _field("preferences.employment_types", "Preferred employment types", "text_list", max_items=30, item_max_length=100),
        _field("preferences.schedule", "Preferred schedule, shifts, weekends, and overtime", "multiline", max_length=3000),
        _field("preferences.travel_limits", "Preferred travel limit", max_length=1000),
        _field("preferences.earliest_start_date", "Earliest start date", "date"),
        _field("preferences.notice_period", "Notice period", max_length=500),
        _field("preferences.compensation_floor", "Preferred minimum compensation", "multiline", max_length=1500, sensitive=True,
               description="A ranking preference unless you also state it as a hard dealbreaker below."),
        _field("preferences.compensation_target", "Compensation target", "multiline", max_length=1500, sensitive=True),
        _field("preferences.compensation_terms", "Base, bonus, equity, and benefits terms", "multiline", max_length=3000, sensitive=True),
        _field("preferences.compensation_currency", "Compensation currency", max_length=20, sensitive=True),
        _field("preferences.compensation_pay_period", "Compensation pay period", max_length=100, sensitive=True),
        _field("preferences.company_size", "Company-size preferences", max_length=500),
        _field("preferences.additional_preferences", "Other preferences", "text_list", max_items=100, item_max_length=500,
               description="Add more preferences, one per line. These guide ranking but do not block search results."),
        _field("preferences.dealbreakers", "Hard dealbreakers (explicit)", "multiline", max_length=5000,
               description="Only list conditions that must block an application. Write each condition clearly."),
    ), description="Preferences broaden and rank job discovery; they are not strict search filters. Only explicit hard dealbreakers block a match."),
    _section("screening_answers", "Reusable screening answers", (
        _field("answers.compensation_wording", "Preferred compensation wording", "multiline", max_length=3000, sensitive=True),
        _field("answers.salary_history", "Salary-history response", "multiline", max_length=3000, sensitive=True),
        _field("answers.start_date_availability", "Start date and availability", "multiline", max_length=2000),
        _field("answers.travel_shift_commute", "Travel, shift, commute, and overtime answers", "multiline", max_length=3000),
        _field("answers.relocation", "Relocation answer", "multiline", max_length=2000),
        _field("answers.work_authorization", "Work-authorization and sponsorship answer", "multiline", max_length=3000, sensitive=True),
        _field("answers.referral_source", "How the user heard about the employer or referral source", "multiline", max_length=2000),
        _field("answers.prior_employment_application", "Prior employment or application at employer", "multiline", max_length=2000, sensitive=True),
        _field("answers.relatives_at_company", "Relatives at the company", "multiline", max_length=2000, sensitive=True),
        _field("answers.outside_employment", "Outside employment", "multiline", max_length=2000, sensitive=True),
        _field("answers.conflicts_noncompete", "Conflicts of interest or non-compete disclosures", "multiline", max_length=3000, sensitive=True),
        _field("answers.years_by_skill", "Years of experience by skill", "records", item_fields=(
            _record_field("skill", "Skill", max_length=200),
            _record_field("years", "Years", "number"),
        ), max_items=200),
        _field("answers.role_company_interest", "Reasons for interest in a role or company", "multiline", max_length=5000),
        _field("answers.strengths_goals", "Strengths and career goals", "multiline", max_length=5000),
        _field("answers.accomplishment_stories", "User-approved accomplishment stories", "records", item_fields=_EXPERIENCE_FIELDS, max_items=100),
        _field("answers.background_drug_screening", "Background or drug-screening answers", "multiline", max_length=3000, sensitive=True,
               description="Store only an explicitly approved answer; pause for authorizations or other legal attestations."),
        _field("answers.custom_bank", "Other user-approved question and answer pairs", "records", item_fields=_ANSWER_BANK_FIELDS, max_items=200, sensitive=True,
               description="Each answer must be explicitly approved. Preserve its source facts and last-confirmed date."),
    ), sensitive=True, description="Screening answers are reusable only when explicitly approved; pause for missing required answers."),
    _section("references", "References", (
        _field("references.records", "References", "records", item_fields=_REFERENCE_FIELDS, max_items=100, sensitive=True,
               description="Never offer or contact a reference without user approval."),
    ), sensitive=True),
    _section("sensitive_disclosures", "Optional sensitive disclosures", (
        _field("disclosures.race_ethnicity", "Race or ethnicity", "multiline", max_length=1000, sensitive=True),
        _field("disclosures.sex_gender", "Sex or gender self-identification", "multiline", max_length=1000, sensitive=True),
        _field("disclosures.disability", "Disability self-identification", "multiline", max_length=1000, sensitive=True),
        _field("disclosures.veteran_status", "Veteran status", "multiline", max_length=1000, sensitive=True),
        _field("disclosures.accommodation_request", "Accommodation request", "multiline", max_length=2000, sensitive=True,
               description="Optional request only. Do not include health diagnoses or medical records."),
        _field("disclosures.age_or_birth_date", "Age or date of birth", "multiline", max_length=300, sensitive=True),
        _field("disclosures.criminal_history", "Criminal-history answer", "multiline", max_length=2000, sensitive=True),
        _field("disclosures.security_clearance", "Security-clearance details", "multiline", max_length=1500, sensitive=True),
    ), sensitive=True, description="Optional self-identification and other sensitive answers. Never infer these values."),
)


def _index_fields(sections: tuple[dict[str, Any], ...]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for section in sections:
        for field in section["fields"]:
            if field["id"] in result:
                raise RuntimeError(f"duplicate profile field id: {field['id']}")
            result[field["id"]] = field
    return result


PROFILE_FIELDS = _index_fields(PROFILE_SECTIONS)


def _fail(path: str, reason: str) -> None:
    raise ValueError(f"{path}: {reason}")


def _check_json_tree(value: object, path: str = "$", depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        _fail(path, "maximum nesting depth exceeded")
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail(path, "non-finite numbers are not allowed")
        return
    if type(value) is str:
        if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in value):
            _fail(path, "unsupported control character")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            _fail(path, "invalid Unicode text")
        return
    if type(value) is list:
        for index, child in enumerate(value):
            _check_json_tree(child, f"{path}[{index}]", depth + 1)
        return
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                _fail(path, "object keys must be strings")
            _check_json_tree(key, f"{path}.<key>", depth + 1)
            _check_json_tree(child, f"{path}.{key}", depth + 1)
        return
    _fail(path, "value must be JSON-compatible")


def _check_value(value: object, definition: dict[str, Any], path: str) -> object:
    value_type = definition["type"]
    if value_type in {"text", "multiline"}:
        if type(value) is not str:
            _fail(path, "must be a string")
        if not value.strip():
            _fail(path, "must not be blank when status is known")
        if len(value) > definition["max_length"]:
            _fail(path, f"must be at most {definition['max_length']} characters")
        return value
    if value_type == "url":
        if type(value) is not str or len(value) > definition["max_length"]:
            _fail(path, "must be a URL string within the length limit")
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            _fail(path, "must be an absolute http or https URL")
        if any(ord(ch) <= 32 or ord(ch) == 127 for ch in value):
            _fail(path, "contains whitespace or a control character")
        return value
    if value_type == "choice":
        if type(value) is not str or value not in definition["choices"]:
            _fail(path, "must be one of the catalog choices")
        return value
    if value_type == "boolean":
        if type(value) is not bool:
            _fail(path, "must be a boolean")
        return value
    if value_type == "integer":
        if type(value) is not int:
            _fail(path, "must be an integer")
        return value
    if value_type == "number":
        if type(value) not in (int, float) or not math.isfinite(value):
            _fail(path, "must be a finite number")
        return value
    if value_type == "date":
        if type(value) is not str:
            _fail(path, "must be an ISO date (YYYY-MM-DD)")
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            _fail(path, "must be a valid ISO date (YYYY-MM-DD)")
        if parsed_date.isoformat() != value:
            _fail(path, "must use the YYYY-MM-DD format")
        return value
    if value_type == "text_list":
        if type(value) is not list or len(value) > definition["max_items"]:
            _fail(path, f"must be a list of at most {definition['max_items']} strings")
        clean: list[str] = []
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            if type(item) is not str or not item.strip():
                _fail(item_path, "must be a non-blank string")
            if len(item) > definition["item_max_length"]:
                _fail(item_path, f"must be at most {definition['item_max_length']} characters")
            clean.append(item)
        return clean
    if value_type == "records":
        if type(value) is not list or len(value) > definition["max_items"]:
            _fail(path, f"must be a list of at most {definition['max_items']} records")
        child_fields = {field["id"]: field for field in definition["item_fields"]}
        clean_records: list[dict[str, object]] = []
        for index, record in enumerate(value):
            record_path = f"{path}[{index}]"
            if type(record) is not dict:
                _fail(record_path, "must be an object")
            extras = set(record) - set(child_fields)
            if extras:
                _fail(record_path, "contains unsupported record fields")
            clean_record: dict[str, object] = {}
            for child_id, child_payload in record.items():
                clean_record[child_id] = _validate_field(child_payload, child_fields[child_id], f"{record_path}.{child_id}")
            if not clean_record:
                _fail(record_path, "must contain at least one field")
            clean_records.append(clean_record)
        return clean_records
    _fail(path, "catalog contains an unsupported value type")


def _validate_field(payload: object, definition: dict[str, Any], path: str) -> dict[str, object]:
    if type(payload) is not dict:
        _fail(path, "must be an object")
    allowed = {"status", "reuse_policy", "value"}
    if set(payload) - allowed:
        _fail(path, "contains unsupported keys")
    if "status" not in payload or "reuse_policy" not in payload:
        _fail(path, "requires status and reuse_policy")
    status = payload["status"]
    policy = payload["reuse_policy"]
    if type(status) is not str or status not in PROFILE_STATUSES:
        _fail(f"{path}.status", f"must be one of {', '.join(PROFILE_STATUSES)}")
    if type(policy) is not str or policy not in REUSE_POLICIES:
        _fail(f"{path}.reuse_policy", f"must be one of {', '.join(REUSE_POLICIES)}")

    result: dict[str, object] = {"status": status, "reuse_policy": policy}
    if status == "known":
        if "value" not in payload:
            _fail(path, "known fields require value")
        result["value"] = _check_value(payload["value"], definition, f"{path}.value")
    elif "value" in payload:
        _fail(path, "value is allowed only when status is known")
    return result


def validate_profile(payload: object) -> dict[str, object]:
    """Validate and return a detached profile payload.

    Shape::

        {
          "schema_version": 1,
          "version": 1,
          "approval_state": "draft" | "approved",
          "approved_version": null | positive integer <= version,
          "fields": {
            "identity.legal_first_name": {
              "status": "known" | "unknown" | "not_applicable" | "withheld",
              "reuse_policy": "auto_use" | "ask" | "never",
              "value": "..."  # required only for known
            }
          }
        }

    ``fields`` may be partial while the user completes the profile. Repeatable
    catalog fields use a list of records; each record maps child field IDs to
    the same status/policy/value envelope. Approved profiles must approve the
    exact current version. Errors name paths and rules but never echo values.
    """
    if type(payload) is not dict:
        raise ValueError("profile must be an object")
    _check_json_tree(payload)
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ValueError("profile must contain valid JSON data") from None
    if len(encoded) > MAX_PROFILE_BYTES:
        raise ValueError(f"profile exceeds the {MAX_PROFILE_BYTES}-byte limit")

    required = {"schema_version", "version", "approval_state", "approved_version", "fields"}
    if set(payload) != required:
        missing = required - set(payload)
        extra = set(payload) - required
        if extra:
            raise ValueError("profile contains unsupported top-level keys")
        raise ValueError("profile is missing required top-level keys")

    schema_version = payload["schema_version"]
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
    version = payload["version"]
    if type(version) is not int or version < 1:
        raise ValueError("version must be a positive integer")
    approval_state = payload["approval_state"]
    if type(approval_state) is not str or approval_state not in APPROVAL_STATES:
        raise ValueError(f"approval_state must be one of {', '.join(APPROVAL_STATES)}")
    approved_version = payload["approved_version"]
    if approved_version is not None and (type(approved_version) is not int or not 1 <= approved_version <= version):
        raise ValueError("approved_version must be null or an integer from 1 through version")
    if approval_state == "approved" and approved_version != version:
        raise ValueError("approved profiles must set approved_version equal to version")

    fields_payload = payload["fields"]
    if type(fields_payload) is not dict:
        raise ValueError("fields must be an object")
    unknown = set(fields_payload) - set(PROFILE_FIELDS)
    if unknown:
        raise ValueError("fields contains unsupported field IDs")

    fields: dict[str, object] = {}
    for field_id, field_payload in fields_payload.items():
        fields[field_id] = _validate_field(field_payload, PROFILE_FIELDS[field_id], f"fields.{field_id}")

    return {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "approval_state": approval_state,
        "approved_version": approved_version,
        "fields": fields,
    }
