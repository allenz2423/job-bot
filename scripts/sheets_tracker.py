#!/usr/bin/env python3
"""Create and synchronize the editable Google Sheets application tracker."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from application_store import list_applications, update_user_fields
from google_auth import ROOT, access_token, config_value, read_env, resolve_path


SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
HEADERS = [
    "Job URL (Identifier)", "Company", "Role", "Location", "Source", "Exact Job URL",
    "Employer Job ID", "Match Score", "Status", "Discovered", "Submitted",
    "Last Email Update", "Next Action", "Priority (You)", "Your Notes", "Review Decision",
]
STATUS_LABELS = {
    "discovered": "Discovered",
    "in_progress": "In progress",
    "applied": "Applied",
    "needs_review": "Needs review",
    "rejected": "Rejected",
    "interview": "Interview",
    "offer": "Offer",
    "closed": "Closed",
}
STATUS_CHOICES = list(STATUS_LABELS.values())
NAVY = {"red": 0.09, "green": 0.16, "blue": 0.29}
BLUE = {"red": 0.13, "green": 0.38, "blue": 0.68}
TEAL = {"red": 0.04, "green": 0.52, "blue": 0.53}
PALE = {"red": 0.94, "green": 0.97, "blue": 0.99}
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}


class GoogleSheetsClient:
    def __init__(self, bearer: str):
        self.bearer = bearer

    def request(self, method: str, path: str, body: dict[str, object] | None = None,
                query: dict[str, str] | None = None) -> dict[str, object]:
        url = SHEETS_API + path
        if query:
            url += "?" + urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.bearer}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=40) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as error:
            if error.code in (401, 403):
                raise RuntimeError(
                    f"Google Sheets denied the request (HTTP {error.code}); check API enablement and OAuth consent."
                ) from None
            raise RuntimeError(f"Google Sheets API request failed (HTTP {error.code}).") from None
        except (URLError, TimeoutError) as error:
            reason = error.reason if isinstance(error, URLError) else "timeout"
            raise RuntimeError(f"Could not reach Google Sheets: {reason}.") from None

    def create(self, body: dict[str, object]) -> dict[str, object]:
        return self.request("POST", "", body)

    def batch_update(self, spreadsheet_id: str, requests: list[dict[str, object]]) -> None:
        encoded_id = quote(spreadsheet_id, safe="")
        self.request("POST", f"/{encoded_id}:batchUpdate", {"requests": requests})

    def values_batch_update(self, spreadsheet_id: str, data: list[dict[str, object]],
                            value_input_option: str = "USER_ENTERED") -> None:
        encoded_id = quote(spreadsheet_id, safe="")
        self.request(
            "POST", f"/{encoded_id}/values:batchUpdate",
            {"valueInputOption": value_input_option, "data": data},
        )

    def values_get(self, spreadsheet_id: str, value_range: str) -> list[list[object]]:
        encoded_id = quote(spreadsheet_id, safe="")
        encoded_range = quote(value_range, safe="!:'")
        response = self.request(
            "GET", f"/{encoded_id}/values/{encoded_range}", query={"valueRenderOption": "UNFORMATTED_VALUE"}
        )
        values = response.get("values", [])
        return values if isinstance(values, list) else []


def _config() -> tuple[dict[str, str], dict[str, str], Path]:
    gmail_config = read_env(ROOT / "config" / "gmail.env")
    sheets_path = ROOT / "config" / "sheets.env"
    sheets_config = read_env(sheets_path)
    token_path = resolve_path(config_value(gmail_config, "GMAIL_TOKEN_FILE"), "secrets/gmail/token.json")
    return gmail_config, sheets_config, token_path


def _save_sheets_config(spreadsheet_id: str, url: str) -> None:
    path = ROOT / "config" / "sheets.env"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("# Google Sheets application tracker. Keep this file out of Git.\n")
        stream.write(f"GOOGLE_SHEETS_SPREADSHEET_ID={spreadsheet_id}\n")
        stream.write(f"GOOGLE_SHEETS_SPREADSHEET_URL={url}\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _sheet_properties(sheet_id: int, title: str, columns: int, rows: int = 5000,
                      frozen_rows: int = 1) -> dict[str, object]:
    return {
        "properties": {
            "sheetId": sheet_id,
            "title": title,
            "gridProperties": {
                "rowCount": rows,
                "columnCount": columns,
                "frozenRowCount": frozen_rows,
                "hideGridlines": True,
            },
            "tabColor": TEAL if title != "Dashboard" else BLUE,
        }
    }


def create_workbook(client: GoogleSheetsClient) -> tuple[str, str]:
    workbook = client.create({
        "properties": {"title": "Job Application Tracker"},
        "sheets": [
            _sheet_properties(0, "Dashboard", 12, rows=100, frozen_rows=0),
            _sheet_properties(1, "Applications", len(HEADERS)),
            _sheet_properties(2, "Review Queue", len(HEADERS)),
        ],
    })
    spreadsheet_id = str(workbook.get("spreadsheetId", ""))
    url = str(workbook.get("spreadsheetUrl", ""))
    if not spreadsheet_id or not url:
        raise RuntimeError("Google created a workbook but returned no spreadsheet link.")
    initialize_workbook(client, spreadsheet_id)
    _save_sheets_config(spreadsheet_id, url)
    return spreadsheet_id, url


def _repeat_cell(sheet_id: int, start_row: int, end_row: int, start_col: int, end_col: int,
                 fmt: dict[str, object], fields: str = "userEnteredFormat") -> dict[str, object]:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": end_row,
                "startColumnIndex": start_col,
                "endColumnIndex": end_col,
            },
            "cell": {"userEnteredFormat": fmt},
            "fields": fields,
        }
    }


def initialize_workbook(client: GoogleSheetsClient, spreadsheet_id: str) -> None:
    value_data: list[dict[str, object]] = [
        {"range": "Applications!A1:P1", "values": [HEADERS]},
        {"range": "Review Queue!A1:P1", "values": [HEADERS]},
        {"range": "Review Queue!A2", "values": [[
            '=IFERROR(FILTER(Applications!A2:P,Applications!P2:P="Needs review",Applications!A2:A<>""),"")'
        ]]},
        {"range": "Dashboard!A1", "values": [["JOB APPLICATION TRACKER"]]},
        {"range": "Dashboard!A2", "values": [["A durable record of roles, applications, and employer updates"]]},
        {"range": "Dashboard!A4", "values": [["TOTAL TRACKED"]]},
        {"range": "Dashboard!A5", "values": [["=COUNTA(Applications!A2:A)"]]},
        {"range": "Dashboard!D4", "values": [["APPLIED"]]},
        {"range": "Dashboard!D5", "values": [['=COUNTIF(Applications!I2:I,"Applied")']]},
        {"range": "Dashboard!G4", "values": [["IN PROGRESS"]]},
        {"range": "Dashboard!G5", "values": [['=COUNTIF(Applications!I2:I,"In progress")']]},
        {"range": "Dashboard!J4", "values": [["NEEDS REVIEW"]]},
        {"range": "Dashboard!J5", "values": [['=COUNTIF(Applications!P2:P,"Needs review")']]},
        {"range": "Dashboard!A9:B17", "values": [
            ["STATUS", "COUNT"],
            ["Discovered", '=COUNTIF(Applications!I2:I,"Discovered")'],
            ["In progress", '=COUNTIF(Applications!I2:I,"In progress")'],
            ["Applied", '=COUNTIF(Applications!I2:I,"Applied")'],
            ["Needs review", '=COUNTIF(Applications!I2:I,"Needs review")'],
            ["Rejected", '=COUNTIF(Applications!I2:I,"Rejected")'],
            ["Interview", '=COUNTIF(Applications!I2:I,"Interview")'],
            ["Offer", '=COUNTIF(Applications!I2:I,"Offer")'],
            ["Closed", '=COUNTIF(Applications!I2:I,"Closed")'],
        ]},
        {"range": "Dashboard!A20", "values": [["Tip: edit Priority, Your Notes, and Review Decision on Applications. The bot leaves those columns untouched."]]},
        {"range": "Review Queue!A20", "values": [["To approve, skip, or mark a duplicate, edit Review Decision on the matching row in Applications."]]},
    ]
    client.values_batch_update(spreadsheet_id, value_data)

    requests: list[dict[str, object]] = [
        {"mergeCells": {"range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 12}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": 12}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": 0, "startRowIndex": 3, "endRowIndex": 4, "startColumnIndex": 0, "endColumnIndex": 2}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": 0, "startRowIndex": 4, "endRowIndex": 6, "startColumnIndex": 0, "endColumnIndex": 2}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": 0, "startRowIndex": 3, "endRowIndex": 4, "startColumnIndex": 3, "endColumnIndex": 5}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": 0, "startRowIndex": 4, "endRowIndex": 6, "startColumnIndex": 3, "endColumnIndex": 5}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": 0, "startRowIndex": 3, "endRowIndex": 4, "startColumnIndex": 6, "endColumnIndex": 8}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": 0, "startRowIndex": 4, "endRowIndex": 6, "startColumnIndex": 6, "endColumnIndex": 8}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": 0, "startRowIndex": 3, "endRowIndex": 4, "startColumnIndex": 9, "endColumnIndex": 11}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {"sheetId": 0, "startRowIndex": 4, "endRowIndex": 6, "startColumnIndex": 9, "endColumnIndex": 11}, "mergeType": "MERGE_ALL"}},
        _repeat_cell(0, 0, 1, 0, 12, {
            "backgroundColor": NAVY,
            "textFormat": {"foregroundColor": WHITE, "fontSize": 22, "bold": True},
            "verticalAlignment": "MIDDLE",
        }, "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)"),
        _repeat_cell(0, 1, 2, 0, 12, {
            "backgroundColor": NAVY,
            "textFormat": {"foregroundColor": {"red": 0.82, "green": 0.89, "blue": 0.96}, "fontSize": 10},
            "verticalAlignment": "MIDDLE",
        }, "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)"),
        _repeat_cell(0, 3, 4, 0, 11, {
            "backgroundColor": BLUE,
            "textFormat": {"foregroundColor": WHITE, "bold": True, "fontSize": 10},
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
        }, "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"),
        _repeat_cell(0, 4, 6, 0, 11, {
            "backgroundColor": PALE,
            "textFormat": {"foregroundColor": NAVY, "bold": True, "fontSize": 26},
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
        }, "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"),
        _repeat_cell(0, 8, 9, 0, 2, {
            "backgroundColor": NAVY,
            "textFormat": {"foregroundColor": WHITE, "bold": True},
        }, "userEnteredFormat(backgroundColor,textFormat)"),
        _repeat_cell(0, 19, 20, 0, 12, {
            "textFormat": {"foregroundColor": {"red": 0.30, "green": 0.37, "blue": 0.45}, "italic": True, "fontSize": 9},
            "wrapStrategy": "WRAP",
        }, "userEnteredFormat(textFormat,wrapStrategy)"),
        _repeat_cell(1, 0, 1, 0, len(HEADERS), {
            "backgroundColor": NAVY,
            "textFormat": {"foregroundColor": WHITE, "bold": True},
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        }, "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,wrapStrategy)"),
        _repeat_cell(2, 0, 1, 0, len(HEADERS), {
            "backgroundColor": NAVY,
            "textFormat": {"foregroundColor": WHITE, "bold": True},
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        }, "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,wrapStrategy)"),
        {"addBanding": {"bandedRange": {
            "range": {"sheetId": 1, "startRowIndex": 0, "endRowIndex": 5000, "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
            "rowProperties": {"headerColor": NAVY, "firstBandColor": WHITE, "secondBandColor": PALE},
        }}},
        {"setBasicFilter": {"filter": {"range": {"sheetId": 1, "startRowIndex": 0, "endRowIndex": 5000, "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}}}},
    ]

    widths = [360, 190, 260, 150, 130, 360, 180, 110, 140, 155, 155, 220, 240, 130, 320, 160]
    for column, pixels in enumerate(widths):
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": 1, "dimension": "COLUMNS", "startIndex": column, "endIndex": column + 1},
            "properties": {"pixelSize": pixels}, "fields": "pixelSize",
        }})
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": 2, "dimension": "COLUMNS", "startIndex": column, "endIndex": column + 1},
            "properties": {"pixelSize": pixels}, "fields": "pixelSize",
        }})
    requests.extend([
        {"updateDimensionProperties": {"range": {"sheetId": 1, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 44}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": 2, "dimension": "ROWS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 44}, "fields": "pixelSize"}},
    ])

    validations = [
        (8, STATUS_CHOICES),
        (13, ["Low", "Normal", "High"]),
        (15, ["Needs review", "Approved", "Skip", "Duplicate"]),
    ]
    for column, choices in validations:
        requests.append({"setDataValidation": {
            "range": {"sheetId": 1, "startRowIndex": 1, "endRowIndex": 5000, "startColumnIndex": column, "endColumnIndex": column + 1},
            "rule": {
                "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": choice} for choice in choices]},
                "strict": True,
                "showCustomUi": True,
            },
        }})

    status_colors = {
        "Applied": {"red": 0.84, "green": 0.94, "blue": 0.87},
        "In progress": {"red": 1.0, "green": 0.94, "blue": 0.80},
        "Needs review": {"red": 1.0, "green": 0.88, "blue": 0.78},
        "Interview": {"red": 0.82, "green": 0.90, "blue": 0.98},
        "Offer": {"red": 0.83, "green": 0.93, "blue": 0.95},
    }
    for label, color in status_colors.items():
        requests.append({"addConditionalFormatRule": {
            "rule": {
                "ranges": [{"sheetId": 1, "startRowIndex": 1, "endRowIndex": 5000, "startColumnIndex": 8, "endColumnIndex": 9}],
                "booleanRule": {
                    "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": label}]},
                    "format": {"backgroundColor": color, "textFormat": {"bold": True, "foregroundColor": NAVY}},
                },
            },
            "index": 0,
        }})

    requests.append({"addChart": {"chart": {
        "spec": {
            "title": "Applications by status",
            "pieChart": {
                "legendPosition": "RIGHT_LEGEND",
                "domain": {"sourceRange": {"sources": [{"sheetId": 0, "startRowIndex": 9, "endRowIndex": 17, "startColumnIndex": 0, "endColumnIndex": 1}]}},
                "series": {"sourceRange": {"sources": [{"sheetId": 0, "startRowIndex": 9, "endRowIndex": 17, "startColumnIndex": 1, "endColumnIndex": 2}]}},
            },
            "backgroundColor": WHITE,
        },
        "position": {"overlayPosition": {"anchorCell": {"sheetId": 0, "rowIndex": 8, "columnIndex": 3}, "widthPixels": 620, "heightPixels": 360}},
    }}})
    client.batch_update(spreadsheet_id, requests)


def _cell(row: list[object], column: int) -> object:
    return row[column] if column < len(row) else ""


def _hyperlink_formula(url: str) -> str:
    escaped = url.replace('"', '""')
    return f'=HYPERLINK("{escaped}","{escaped}")'


def sync_workbook(client: GoogleSheetsClient, spreadsheet_id: str, db_path: str | Path | None = None) -> dict[str, int]:
    existing_rows = client.values_get(spreadsheet_id, "Applications!A1:P")
    apps = list_applications(db_path)
    app_by_key = {str(app["application_key"]): app for app in apps}
    key_by_url = {
        str(item["job_url"]): str(app["application_key"])
        for app in apps for item in app.get("job_urls", []) if item.get("job_url")
    }
    row_by_key: dict[str, int] = {}
    feedback: list[tuple[str, str, str, str]] = []
    if existing_rows:
        for index, row in enumerate(existing_rows[1:], start=2):
            first_column = str(_cell(row, 0))
            saved_url = str(_cell(row, 5))
            key = (first_column if first_column in app_by_key else
                   key_by_url.get(first_column) or key_by_url.get(saved_url, ""))
            if key:
                row_by_key[key] = index
                feedback.append((key, str(_cell(row, 13)), str(_cell(row, 14)), str(_cell(row, 15))))

    for key, priority, notes, decision in feedback:
        update_user_fields(key, priority=priority, user_notes=notes, review_decision=decision, db_path=db_path)

    apps = list_applications(db_path)
    client.values_batch_update(spreadsheet_id, [{"range": "Applications!A1:P1", "values": [HEADERS]},
                                                 {"range": "Review Queue!A1:P1", "values": [HEADERS]}])
    client.batch_update(spreadsheet_id, [{"updateDimensionProperties": {
        "range": {"sheetId": 1, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
        "properties": {"hiddenByUser": False, "pixelSize": 360},
        "fields": "hiddenByUser,pixelSize",
    }}])
    updates: list[dict[str, object]] = []
    url_updates: list[dict[str, object]] = []
    source_updates: list[dict[str, object]] = []
    inserted = updated = 0
    next_row = max(2, len(existing_rows) + 1)
    for app in apps:
        key = str(app["application_key"])
        urls = app.get("job_urls", [])
        primary_url = (next((item.get("job_url", "") for item in urls if item.get("is_primary")), "")
                       or next((item.get("job_url", "") for item in urls if item.get("job_url")), ""))
        if not primary_url:
            continue
        status = STATUS_LABELS.get(str(app["status"]), "Discovered")
        score = "" if app.get("match_score") is None else float(app["match_score"])
        bot_values = [
            primary_url,
            str(app.get("company", "")),
            str(app.get("role", "")),
            str(app.get("location", "")),
            str(app.get("job_source", "")),
            primary_url,
            str(app.get("employer_job_id", "")),
            score,
            status,
            str(app.get("discovered_at", "")),
            str(app.get("submitted_at", "")),
            str(app.get("last_email_update", "")),
            str(app.get("next_action", "")),
        ]
        if key in row_by_key:
            sheet_row = row_by_key[key]
            target = f"Applications!A{sheet_row}:M{sheet_row}"
            updates.append({"range": target, "values": [bot_values]})
            updated += 1
        else:
            sheet_row = next_row
            target = f"Applications!A{sheet_row}:P{sheet_row}"
            updates.append({"range": target, "values": [bot_values + [str(app.get("priority", "")), str(app.get("user_notes", "")), str(app.get("review_decision", ""))]]})
            row_by_key[key] = next_row
            next_row += 1
            inserted += 1
        if primary_url:
            url_updates.append({
                "range": f"Applications!F{sheet_row}:F{sheet_row}",
                "values": [[_hyperlink_formula(primary_url)]],
            })
        if app.get("job_source") == "Adzuna":
            source_updates.append({
                "range": f"Applications!E{sheet_row}:E{sheet_row}",
                "values": [['=HYPERLINK("https://www.adzuna.co.uk/","Adzuna")']],
            })

    if updates:
        client.values_batch_update(spreadsheet_id, updates, value_input_option="RAW")
    if url_updates:
        client.values_batch_update(spreadsheet_id, url_updates, value_input_option="USER_ENTERED")
    if source_updates:
        client.values_batch_update(spreadsheet_id, source_updates, value_input_option="USER_ENTERED")
        client.batch_update(spreadsheet_id, [{"updateDimensionProperties": {
            "range": {"sheetId": 1, "dimension": "ROWS", "startIndex": 1, "endIndex": 5000},
            "properties": {"pixelSize": 25}, "fields": "pixelSize",
        }}])

    return {"applications": len(apps), "inserted": inserted, "updated": updated}


def sync_configured_workbook(db_path: str | Path | None = None) -> dict[str, object]:
    gmail_config, sheets_config, token_path = _config()
    spreadsheet_id = sheets_config.get("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
    if not spreadsheet_id:
        raise RuntimeError("No spreadsheet is configured; run python3 scripts/sheets_tracker.py create first.")
    bearer = access_token(token_path)
    client = GoogleSheetsClient(bearer)
    selected_db = db_path or resolve_path(config_value(gmail_config, "GMAIL_DB_PATH"), "state/jobbot.sqlite3")
    result = sync_workbook(client, spreadsheet_id, selected_db)
    return {
        "spreadsheet_url": sheets_config.get("GOOGLE_SHEETS_SPREADSHEET_URL", ""),
        **result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "sync"))
    args = parser.parse_args()
    gmail_config, sheets_config, token_path = _config()
    spreadsheet_id = sheets_config.get("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()

    if args.command == "create":
        bearer = access_token(token_path)
        client = GoogleSheetsClient(bearer)
        if spreadsheet_id:
            url = sheets_config.get("GOOGLE_SHEETS_SPREADSHEET_URL", "")
            print(json.dumps({"created": False, "spreadsheet_url": url}, ensure_ascii=False))
            return 0
        spreadsheet_id, url = create_workbook(client)
        print(json.dumps({"created": True, "spreadsheet_url": url}, ensure_ascii=False))
        return 0

    if not spreadsheet_id:
        raise RuntimeError("No spreadsheet is configured; run python3 scripts/sheets_tracker.py create first.")
    db_path = resolve_path(config_value(gmail_config, "GMAIL_DB_PATH"), "state/jobbot.sqlite3")
    print(json.dumps(sync_configured_workbook(db_path), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Sheets tracker failed: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1)
