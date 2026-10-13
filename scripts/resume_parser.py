"""Local, bounded extraction of resume text and conservative profile suggestions.

The parser never writes uploaded content to disk or logs it. Suggestions contain
only explicitly formatted contact details, profile links, a clearly headed
summary, and entries from a clearly headed skills section. They remain profile
suggestions for the caller to present for review.
"""

from __future__ import annotations

import io
import ntpath
import os
import re
import selectors
import shutil
import subprocess
import threading
import time
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:  # Support both ``import scripts.resume_parser`` and direct script imports.
    from . import profile_schema
except ImportError:  # pragma: no cover - used when scripts/ is on sys.path
    import profile_schema  # type: ignore[no-redef]


MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_PDF_STDOUT_BYTES = 1 * 1024 * 1024
PDF_TIMEOUT_SECONDS = 20
MAX_DOCX_XML_BYTES = 10 * 1024 * 1024

_EMAIL_RE = re.compile(
    r"(?<![A-Z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(
    r"(?<![\w])(?:\+\d{1,3}[\s.-]?)?"
    r"(?:\(\d{2,4}\)|\d{2,4})[\s.-]\d{3,4}[\s.-]\d{3,4}"
    r"(?:\s*(?:ext\.?|x)\s*\d{1,6})?(?![\w])",
    re.IGNORECASE,
)
_URL_RE = re.compile(
    r"(?i)(?:\bhttps?://|\bwww\.)[^\s<>{}\[\]\"']+"
    r"|\b(?:linkedin\.com|github\.com)(?:/[^\s<>{}\[\]\"']*)?"
)
_PORTFOLIO_LABEL_URL_RE = re.compile(
    r"(?i)\bportfolio(?:\s+(?:url|link))?\s*[:|—–-]\s*"
    r"(?P<url>(?:(?:https?://)?(?:www\.)?)"
    r"[A-Z0-9](?:[A-Z0-9.-]*[A-Z0-9])?\.[A-Z]{2,}"
    r"(?:/[^\s<>{}\[\]\"']*)?)"
)
_HIGH_RISK_SUMMARY_RE = re.compile(
    r"\b(?:ssn|social security|taxpayer (?:id|identification)|tax id|"
    r"passport(?: number| no\.?| #)?|national id(?:entity)?|government id|"
    r"bank account|routing number|date of birth|birth date|dob|medical (?:history|record|condition)|"
    r"health record|health diagnosis|diagnos(?:is|ed)|visa number)\b",
    re.IGNORECASE,
)

_SUMMARY_HEADINGS = {"summary", "professional summary", "career summary"}
_SKILLS_HEADINGS = {
    "skills",
    "technical skills",
    "core skills",
    "key skills",
    "skills and tools",
    "skills & tools",
    "technical skills and tools",
    "technical skills & tools",
    "core competencies",
}
_SECTION_HEADINGS = _SUMMARY_HEADINGS | _SKILLS_HEADINGS | {
    "objective",
    "career objective",
    "profile",
    "professional profile",
    "experience",
    "work experience",
    "work history",
    "professional experience",
    "professional background",
    "employment history",
    "employment",
    "education",
    "education and training",
    "education & training",
    "certifications",
    "licenses and certifications",
    "projects",
    "selected projects",
    "awards",
    "publications",
    "languages",
    "volunteer experience",
    "leadership",
    "references",
    "interests",
    "additional information",
}
_SKILL_CATEGORIES = {
    "language",
    "languages",
    "programming language",
    "programming languages",
    "frameworks",
    "libraries",
    "tools",
    "platforms",
    "cloud",
    "cloud platforms",
    "databases",
    "methodologies",
    "certifications",
    "operating systems",
    "software",
    "technical skills",
    "other",
}
_NON_SKILL_WORDS = {
    "a", "an", "and", "at", "by", "for", "from", "in", "into", "of",
    "on", "or", "the", "to", "with", "using", "use", "experienced",
    "experience", "proficient", "proficiency", "skilled", "knowledgeable",
    "managed", "built", "led", "developed", "created", "designed",
    "supported", "implemented", "improved", "responsible", "responsibilities",
}


def _safe_basename(filename: str) -> str:
    if type(filename) is not str:
        raise ValueError("Resume filename must be text")
    leaf = ntpath.basename(filename.replace("/", "\\"))
    leaf = "".join(
        ch for ch in leaf
        if unicodedata.category(ch) not in {"Cc", "Cf"} and ch not in {"/", "\\", ":"}
    ).strip().strip(".")
    if not leaf:
        leaf = "resume"
    if len(leaf) > 200:
        suffix = Path(leaf).suffix
        leaf = leaf[: max(1, 200 - len(suffix))] + suffix
    return leaf


def _clip_text(text: str) -> tuple[str, bool]:
    # Keep printable content and normal text whitespace. This also makes the
    # returned value safe for the profile schema's JSON string validation.
    clean = "".join(
        ch if ch in "\t\n\r" or unicodedata.category(ch) not in {"Cc", "Cs"} else " "
        for ch in text
    )
    clean = clean.replace("\r\n", "\n").replace("\r", "\n")
    return clean[:MAX_TEXT_CHARS], len(clean) > MAX_TEXT_CHARS


def _extract_pdf(content: bytes) -> tuple[str, str]:
    executable = shutil.which("pdftotext")
    if not executable:
        return "", "PDF text extraction is unavailable on this system."

    try:
        process = subprocess.Popen(
            [executable, "-layout", "-", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
        )
    except OSError:
        return "", "PDF text extraction could not be started."

    assert process.stdin is not None
    assert process.stdout is not None
    writer_error: list[bool] = []

    def write_input() -> None:
        try:
            process.stdin.write(content)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            writer_error.append(True)
            try:
                process.stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=write_input, daemon=True)
    writer.start()
    output = bytearray()
    timed_out = False
    too_much_output = False
    deadline = time.monotonic() + PDF_TIMEOUT_SECONDS
    selector = selectors.DefaultSelector()
    try:
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(min(remaining, 0.25))
            if not events:
                if process.poll() is not None:
                    # A closed pipe is normally reported as readable; continue
                    # once so the EOF can be consumed.
                    continue
                continue
            try:
                chunk = os.read(process.stdout.fileno(), 64 * 1024)
            except BlockingIOError:
                continue
            if not chunk:
                break
            if len(output) + len(chunk) > MAX_PDF_STDOUT_BYTES:
                too_much_output = True
                break
            output.extend(chunk)
    finally:
        selector.close()
        if timed_out or too_much_output:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        writer.join(timeout=2)
        process.stdout.close()
        if process.stdin and not process.stdin.closed:
            process.stdin.close()

    if timed_out:
        return "", "PDF text extraction timed out."
    if too_much_output:
        return "", "PDF text output exceeded the extraction limit."
    if process.returncode != 0:
        return "", "PDF text extraction could not read this file."
    if writer_error:
        return "", "PDF text extraction could not read this file."
    try:
        return output.decode("utf-8-sig", errors="replace"), ""
    except Exception:  # Defensive: decoding with replacement should not fail.
        return "", "PDF text extraction could not decode this file."


def _docx_xml_names(archive: zipfile.ZipFile) -> list[str]:
    if len(archive.infolist()) > 4096:
        raise ValueError("DOCX file contains too many entries")
    names = set(archive.namelist())
    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
        raise ValueError("Resume file is not a supported DOCX document")
    return sorted(
        name for name in names
        if re.fullmatch(r"word/(?:document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml", name)
    )


def _append_docx_text(node: ET.Element, parts: list[str], remaining: int) -> int:
    """Append readable Word runs while bounding extracted characters."""
    for child in node:
        if remaining <= 0:
            return 0
        local = child.tag.rsplit("}", 1)[-1] if isinstance(child.tag, str) else ""
        if local == "t":
            value = child.text or ""
            value = value[:remaining]
            parts.append(value)
            remaining -= len(value)
        elif local == "tab":
            parts.append("\t")
            remaining -= 1
        elif local in {"br", "cr"}:
            parts.append("\n")
            remaining -= 1
        else:
            remaining = _append_docx_text(child, parts, remaining)
            if local in {"p", "tr"} and remaining:
                parts.append("\n")
                remaining -= 1
    return remaining


def _extract_docx(content: bytes) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = _docx_xml_names(archive)
            total_size = 0
            chunks: list[str] = []
            remaining = MAX_TEXT_CHARS
            for name in names:
                info = archive.getinfo(name)
                if info.flag_bits & 0x1:
                    return "", "DOCX file uses unsupported encryption."
                total_size += info.file_size
                if total_size > MAX_DOCX_XML_BYTES:
                    return "", "DOCX XML content exceeded the extraction limit."
                raw = archive.read(info)
                if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                    return "", "DOCX file contains unsupported XML declarations."
                root = ET.fromstring(raw)
                remaining = _append_docx_text(root, chunks, remaining)
                if remaining:
                    chunks.append("\n")
                    remaining -= 1
            text = "".join(chunks)
            return text, "" if remaining else "DOCX text was truncated to the extraction limit."
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError, RuntimeError, ValueError):
        return "", "DOCX text extraction could not read this file."


def extract_resume(filename: str, content: bytes) -> dict[str, object]:
    """Extract bounded text from a PDF, DOCX, or UTF-8/Windows-1252 TXT file.

    Invalid filenames, unsupported formats, mismatched file signatures, and
    oversized uploads raise ``ValueError`` with a generic message that does
    not include uploaded content or a potentially sensitive path.
    """
    safe_name = _safe_basename(filename)
    if type(content) is not bytes:
        raise ValueError("Resume content must be bytes")
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("Resume file exceeds the 5 MiB limit")
    if not content:
        raise ValueError("Resume file is empty")

    file_format = Path(safe_name).suffix.lower().lstrip(".")
    if file_format not in {"pdf", "docx", "txt"}:
        raise ValueError("Resume must use a .pdf, .docx, or .txt extension")

    if file_format == "pdf":
        if b"%PDF-" not in content[:1024]:
            raise ValueError("PDF extension does not match the file signature")
        raw_text, warning = _extract_pdf(content)
    elif file_format == "docx":
        if not content.startswith(b"PK\x03\x04"):
            raise ValueError("DOCX extension does not match the file signature")
        raw_text, warning = _extract_docx(content)
    else:
        # Plain text has no portable magic signature. Decode it strictly using
        # the requested preference and reject obvious binary payloads.
        if b"\x00" in content:
            raise ValueError("TXT file contains unsupported binary data")
        try:
            raw_text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                raw_text = content.decode("cp1252")
            except UnicodeDecodeError:
                raise ValueError("TXT file encoding is not supported") from None
        warning = ""

    text, truncated = _clip_text(raw_text)
    if truncated and not warning:
        warning = "Extracted text was truncated to 100,000 characters."
    return {"filename": safe_name, "format": file_format, "text": text, "warning": warning}


def _known(value: object) -> dict[str, object]:
    return {"status": "known", "reuse_policy": "auto_use", "value": value}


def _normalize_heading(line: str) -> tuple[str, str]:
    candidate = line.strip().strip("\ufeff")
    candidate = re.sub(r"^\s*(?:\d{1,2}[.)]|[•▪◦‣⁃*+-])\s*", "", candidate)
    match = re.match(r"^([^:]{1,70}):\s*(.*)$", candidate)
    remainder = ""
    if match:
        candidate = match.group(1)
        remainder = match.group(2).strip()
    candidate = candidate.strip(" \t|—–-•▪◦‣⁃*#")
    normalized = re.sub(r"\s+", " ", candidate).casefold()
    return normalized, remainder


def _section_content(
    text: str,
    target_headings: set[str],
    *,
    stop_at_blank: bool = False,
) -> str | None:
    lines = text.splitlines()
    start: int | None = None
    first_remainder = ""
    for index, line in enumerate(lines):
        heading, remainder = _normalize_heading(line)
        if heading in target_headings:
            start = index + 1
            first_remainder = remainder
            break
    if start is None:
        return None

    content: list[str] = [first_remainder] if first_remainder else []
    started = bool(first_remainder)
    for line in lines[start:]:
        heading, _ = _normalize_heading(line)
        if heading in _SECTION_HEADINGS:
            break
        if stop_at_blank and not line.strip():
            if started:
                break
            continue
        content.append(line.rstrip())
        if line.strip():
            started = True
    value = "\n".join(content).strip()
    return value or None


def _summary_suggestion(text: str) -> str | None:
    value = _section_content(text, _SUMMARY_HEADINGS, stop_at_blank=True)
    if not value or _HIGH_RISK_SUMMARY_RE.search(value):
        return None
    value = "\n".join(line.strip() for line in value.splitlines()).strip()
    value = value[: int(profile_schema.PROFILE_FIELDS["materials.professional_summary"]["max_length"])]
    return value or None


def _emails(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _EMAIL_RE.finditer(text):
        value = match.group(0).strip(".,;:!?)]}>")
        if len(value) > 320 or value.count("@") != 1:
            continue
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            found.append(value)
        if len(found) >= 20:
            break
    return found


def _phones(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _PHONE_RE.finditer(text):
        value = re.sub(r"\s+", " ", match.group(0)).strip(" .,-;")
        digit_count = sum(ch.isdigit() for ch in value)
        if not 10 <= digit_count <= 15 or len(value) > 100:
            continue
        key = re.sub(r"\D", "", value)
        if key not in seen:
            seen.add(key)
            found.append(value)
        if len(found) >= 20:
            break
    return found


def _header_text(text: str) -> str:
    """Limit contact extraction to the short document header, before sections."""
    header: list[str] = []
    for line in text.splitlines()[:20]:
        heading, _ = _normalize_heading(line)
        if heading in _SECTION_HEADINGS:
            break
        header.append(line)
    return "\n".join(header)


def _trim_url(value: str) -> str:
    return value.strip().rstrip(".,;:!?)]}>")


def _absolute_url(value: str) -> str | None:
    value = _trim_url(value)
    if not value or "@" in value or any(ord(ch) <= 32 or ord(ch) == 127 for ch in value):
        return None
    if value.casefold().startswith("www.") or re.match(r"(?i)^(?:linkedin\.com|github\.com)(?:/|$)", value):
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if any(ch in parsed.hostname for ch in "\\%"):
            return None
    except ValueError:
        return None
    return value


def _profile_links(text: str) -> list[dict[str, object]]:
    candidates: list[tuple[str, str]] = []
    for line in text.splitlines():
        line_matches = list(_URL_RE.finditer(line))
        for match in line_matches:
            absolute = _absolute_url(match.group(0))
            if not absolute:
                continue
            host = (urlsplit(absolute).hostname or "").lower().removeprefix("www.")
            if host == "linkedin.com" or host.endswith(".linkedin.com"):
                candidates.append(("LinkedIn", absolute))
            elif host == "github.com" or host.endswith(".github.com"):
                candidates.append(("GitHub", absolute))
            else:
                before = line[max(0, match.start() - 50):match.start()]
                if re.search(r"\bportfolio(?:\s+(?:url|link))?\s*[:|—–-]?\s*$", before, re.IGNORECASE):
                    candidates.append(("Portfolio", absolute))
                elif re.search(r"(?:^|/)portfolio(?:/|$)", urlsplit(absolute).path, re.IGNORECASE):
                    candidates.append(("Portfolio", absolute))

        # A portfolio label may be followed by an unprefixed hostname.
        labelled = _PORTFOLIO_LABEL_URL_RE.search(line)
        if labelled:
            labelled_url = labelled.group("url")
            if not re.match(r"(?i)^(?:https?://|www\.)", labelled_url):
                labelled_url = "https://" + labelled_url
            absolute = _absolute_url(labelled_url)
            if absolute:
                host = (urlsplit(absolute).hostname or "").lower().removeprefix("www.")
                if host == "linkedin.com" or host.endswith(".linkedin.com"):
                    candidates.append(("LinkedIn", absolute))
                elif host == "github.com" or host.endswith(".github.com"):
                    candidates.append(("GitHub", absolute))
                else:
                    candidates.append(("Portfolio", absolute))

    records: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for kind, url in candidates:
        key = (kind, url.casefold())
        if key in seen:
            continue
        seen.add(key)
        records.append({"kind": _known(kind), "url": _known(url)})
        if len(records) >= 30:
            break
    return records


def _skill_items(text: str) -> list[str]:
    section = _section_content(text, _SKILLS_HEADINGS)
    if not section:
        return []
    items: list[str] = []
    seen: set[str] = set()
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        was_bulleted = bool(re.match(r"^(?:[•▪◦‣⁃*+-]|\d{1,2}[.)])\s+", line))
        line = re.sub(r"^(?:[•▪◦‣⁃*+-]|\d{1,2}[.)])\s+", "", line).strip()
        category_match = re.match(r"^([^:]{1,50}):\s*(.*)$", line)
        if category_match:
            category = re.sub(r"\s+", " ", category_match.group(1)).casefold()
            if category not in _SKILL_CATEGORIES:
                continue
            line = category_match.group(2).strip()
        has_list_delimiter = bool(re.search(r"[,;|]", line))
        if has_list_delimiter:
            entries = re.split(r"[,;|]", line)
        elif was_bulleted or _is_atomic_skill(line):
            entries = [line]
        else:
            continue

        for entry in entries:
            value = re.sub(r"\s+", " ", entry).strip(" \t•▪◦‣⁃*+-")
            if not value or len(value) > 200 or len(value.split()) > 12:
                continue
            if any(ch in value for ch in "!?{}<>\n\r"):
                continue
            # Periods are valid in names such as Node.js and .NET, but a
            # sentence-ending full stop is not a skill delimiter.
            value = value.rstrip(".").strip()
            if not value or not _is_atomic_skill(value):
                continue
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            items.append(value)
            if len(items) >= 300:
                return items
    return items


def _is_atomic_skill(value: str) -> bool:
    words = re.findall(r"[\w+#./-]+", value, flags=re.UNICODE)
    if not words or len(words) > 5:
        return False
    if any(word.casefold() in _NON_SKILL_WORDS for word in words):
        return False
    # Reject a sentence or prose fragment while allowing explicit technology
    # and discipline labels such as "C++", "Project Management", or "Azure".
    if re.search(r"[.!?]\s", value):
        return False
    return bool(re.fullmatch(r"[\w+#./ -]+", value, flags=re.UNICODE))


def suggest_profile_fields(text: str) -> dict[str, object]:
    """Return profile-schema field envelopes for facts explicitly in resume text.

    Missing facts are omitted (therefore remain unknown). Employment, education,
    qualifications, eligibility, and screening answers are never synthesized.
    """
    if type(text) is not str:
        return {}
    text, _ = _clip_text(text)
    header = _header_text(text)
    fields: dict[str, object] = {}

    emails = _emails(header)
    if emails:
        fields["contact.email_addresses"] = _known(emails)

    phones = _phones(header)
    if phones:
        fields["contact.phone_numbers"] = _known(phones)

    links = _profile_links(header)
    if links:
        fields["contact.profile_links"] = _known(links)

    summary = _summary_suggestion(text)
    if summary:
        fields["materials.professional_summary"] = _known(summary)

    skills = _skill_items(text)
    if skills:
        fields["skills.records"] = _known([{"skill": _known(skill)} for skill in skills])

    try:
        validated = profile_schema.validate_profile({
            "schema_version": profile_schema.SCHEMA_VERSION,
            "version": 1,
            "approval_state": "draft",
            "approved_version": None,
            "fields": fields,
        })
    except (TypeError, ValueError):
        # Keep this a suggestion API: malformed or overly large extracted text
        # yields no reusable fields instead of an exception or partial output.
        return {}
    return validated["fields"]  # type: ignore[return-value]
