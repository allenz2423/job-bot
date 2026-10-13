#!/usr/bin/env python3
"""JSON-in/JSON-out application tools for a local LLM tool adapter.

Examples:
  printf '%s' '{"job_url":"https://jobs.example/123"}' | python3 scripts/application_tools.py find_existing_application
  printf '%s' '{"job_url":"https://jobs.example/123","company":"Example","role":"Analyst"}' | python3 scripts/application_tools.py upsert_application
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from application_store import (
    DEFAULT_DB,
    find_existing_application,
    mark_applied,
    mark_in_progress,
    recover_in_progress,
    upsert_application,
)
from sheets_tracker import sync_configured_workbook


TOOL_SCHEMAS = [
    {
        "name": "find_existing_application",
        "description": "Check durable application history by exact full URL or employer job ID; company/title matches are only possible duplicates.",
        "parameters": {
            "type": "object",
            "properties": {
                "job_url": {"type": "string", "description": "Exact full job URL, including query parameters."},
                "employer_job_id": {"type": "string"},
                "job_source": {"type": "string"},
                "company": {"type": "string"},
                "role": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "upsert_application",
        "description": "Record or refresh a listing in the durable application history. Possible company/title duplicates are placed in review.",
        "parameters": {
            "type": "object",
            "properties": {
                "job_url": {"type": "string"},
                "job_source": {"type": "string"},
                "employer_job_id": {"type": "string"},
                "company": {"type": "string"},
                "role": {"type": "string"},
                "location": {"type": "string"},
                "match_score": {"type": "number", "minimum": 0, "maximum": 100},
                "next_action": {"type": "string"},
            },
            "required": ["job_url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "mark_application_in_progress",
        "description": "Record the exact URL and timestamp before beginning an application so a restart can resume it.",
        "parameters": {
            "type": "object",
            "properties": {
                "application_key": {"type": "string"},
                "job_url": {"type": "string"},
            },
            "required": ["application_key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "mark_application_applied",
        "description": "Mark applied only after the site confirms a completed submission; confirmed must be true.",
        "parameters": {
            "type": "object",
            "properties": {
                "application_key": {"type": "string"},
                "job_url": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
            "required": ["application_key", "confirmed"],
            "additionalProperties": False,
        },
    },
    {
        "name": "recover_in_progress_applications",
        "description": "Return exact full URLs for applications interrupted before submission confirmation.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sync_application_tracker",
        "description": "Sync the durable application ledger to the user's Google Sheet and import edits to Priority, Your Notes, and Review Decision.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def load_payload() -> dict[str, object]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        raise ValueError("stdin must contain one JSON object") from None
    if not isinstance(payload, dict):
        raise ValueError("tool input must be a JSON object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=[schema["name"] for schema in TOOL_SCHEMAS])
    args = parser.parse_args()
    payload = load_payload()
    db_path = os.environ.get("GMAIL_DB_PATH", str(DEFAULT_DB))

    if args.tool == "find_existing_application":
        result = find_existing_application(
            job_url=str(payload.get("job_url", "")),
            employer_job_id=str(payload.get("employer_job_id", "")),
            job_source=str(payload.get("job_source", "")),
            company=str(payload.get("company", "")),
            role=str(payload.get("role", "")),
            db_path=db_path,
        )
    elif args.tool == "upsert_application":
        result = upsert_application(payload, db_path=db_path)
    elif args.tool == "mark_application_in_progress":
        result = mark_in_progress(
            str(payload.get("application_key", "")), str(payload.get("job_url", "")), db_path=db_path
        )
    elif args.tool == "mark_application_applied":
        result = mark_applied(
            str(payload.get("application_key", "")), str(payload.get("job_url", "")),
            confirmed=payload.get("confirmed") is True, db_path=db_path,
        )
    elif args.tool == "recover_in_progress_applications":
        result = recover_in_progress(db_path=db_path)
    else:
        result = sync_configured_workbook(db_path=db_path)

    json.dump({"ok": True, "result": result}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
