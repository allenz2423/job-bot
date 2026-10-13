"""Keyword job searches through explicitly configured official APIs.

Provider credentials stay in the caller's tenant adapter and never enter a
model prompt, queue row, diagnostic, or notification.
"""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from . import source_adapters
except ImportError:
    import source_adapters


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESULTS = 50
SEARXNG_RESULTS_PER_PAGE = 20
SEARXNG_MAX_PAGES_PER_QUERY = 50
ADZUNA_COUNTRIES = {"us", "gb", "ca", "au"}


class SearchError(Exception):
    """A bounded error code safe to log without exposing API credentials."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _text(value: object, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def is_known_collection_page(job_url: str) -> bool:
    """Hard stop for known search/category URL shapes; the model handles the rest."""
    parsed = urlsplit(job_url)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    path = parsed.path.casefold()
    if host == "ziprecruiter.com":
        return path.startswith("/jobs/") or path == "/university"
    if host == "indeed.com":
        return (path.startswith("/q-") and path.endswith("-jobs.html")) or path == "/jobs"
    if host == "simplyhired.com":
        return path == "/search"
    if host == "jooble.org":
        return path.startswith("/jobs-")
    if host == "prosple.com":
        return path.endswith("-jobs")
    if host == "higheredjobs.com":
        return path == "/admin/search.cfm"
    if host == "builtin.com":
        return path.startswith("/jobs/")
    if host == "roberthalf.com":
        return path.startswith("/us/en/jobs/all/")
    if host == "github.com":
        return path.startswith("/speedyapply/2027-swe-college-jobs")
    if host == "careers.leidos.com":
        return path == "/pages/intern-and-new-graduate-jobs"
    if host == "careers.snowflake.com":
        return path == "/us/en/university"
    if host == "accenture.com":
        return path.startswith("/us-en/careers/explore-careers/area-of-interest/")
    if host == "jpmorganchase.com":
        return path.startswith("/careers/explore-opportunities/programs/")
    return False


def search_adzuna(phrase: str, location: str, credentials: dict[str, str]) -> list[source_adapters.JobListing]:
    """Fetch one bounded page of Adzuna ads for a saved search phrase."""
    country = credentials.get("country", "")
    app_id = credentials.get("app_id", "")
    app_key = credentials.get("app_key", "")
    if (country not in ADZUNA_COUNTRIES or not app_id or not app_key or
            len(app_id) > 200 or len(app_key) > 200):
        raise SearchError("invalid_credentials")
    if not isinstance(phrase, str) or not phrase or len(phrase) > 120:
        raise SearchError("invalid_query")
    if not isinstance(location, str) or len(location) > 120:
        raise SearchError("invalid_query")
    query = {"app_id": app_id, "app_key": app_key, "results_per_page": str(MAX_RESULTS),
             "what": phrase, "content-type": "application/json"}
    if location:
        query["where"] = location
    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1?{urlencode(query)}"
    request = Request(url, headers={"Accept": "application/json",
                                    "User-Agent": source_adapters.USER_AGENT}, method="GET")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=25) as response:
            if response.headers.get("Content-Length", "").isdigit() and int(response.headers["Content-Length"]) > MAX_RESPONSE_BYTES:
                raise SearchError("response_too_large")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise SearchError("access_denied") from None
        if exc.code == 429:
            raise SearchError("rate_limited") from None
        raise SearchError("http_error") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise SearchError("network_error") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SearchError("response_too_large")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise SearchError("invalid_response") from None
    if type(payload) is not dict or type(payload.get("results")) is not list:
        raise SearchError("invalid_response")
    result: list[source_adapters.JobListing] = []
    for item in payload["results"][:MAX_RESULTS]:
        if type(item) is not dict:
            continue
        role = _text(item.get("title"), 500)
        url_value = item.get("redirect_url")
        if not role:
            continue
        try:
            job_url = source_adapters.validate_public_job_url(url_value)
        except ValueError:
            continue
        company_data = item.get("company")
        location_data = item.get("location")
        company = _text(company_data.get("display_name"), 300) if type(company_data) is dict else ""
        place = _text(location_data.get("display_name"), 300) if type(location_data) is dict else ""
        description = source_adapters._clean_html(_text(item.get("description"), 12000))
        # An Adzuna advert ID is not a confirmed employer job ID.
        result.append(source_adapters.JobListing(job_url, role, company, place, description))
    return result


def search_searxng(phrase: str, location: str, *, pageno: int = 1) -> list[source_adapters.JobListing]:
    """Fetch one bounded page through the private SearXNG instance."""
    if not isinstance(phrase, str) or not phrase.strip() or len(phrase) > 120:
        raise SearchError("invalid_query")
    if not isinstance(location, str) or len(location) > 120:
        raise SearchError("invalid_query")
    if type(pageno) is not int or not 1 <= pageno <= SEARXNG_MAX_PAGES_PER_QUERY:
        raise SearchError("invalid_page")
    query = f'{phrase.strip()} jobs careers hiring'
    if location.strip():
        query += f' {location.strip()}'
    base = os.environ.get("SEARXNG_URL", "http://searxng:8080").rstrip("/")
    url = base + "/search?" + urlencode({
        "q": query, "format": "json", "language": "en-US", "pageno": str(pageno),
    })
    request = Request(url, headers={
        "Accept": "application/json", "User-Agent": source_adapters.USER_AGENT,
    }, method="GET")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=25) as response:
            if response.headers.get("Content-Length", "").isdigit() and int(response.headers["Content-Length"]) > MAX_RESPONSE_BYTES:
                raise SearchError("response_too_large")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise SearchError("access_denied") from None
        if exc.code == 429:
            raise SearchError("rate_limited") from None
        raise SearchError("http_error") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise SearchError("network_error") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SearchError("response_too_large")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise SearchError("invalid_response") from None
    results = payload.get("results") if type(payload) is dict else None
    if type(results) is not list:
        raise SearchError("invalid_response")
    listings = []
    for item in results[:SEARXNG_RESULTS_PER_PAGE]:
        if type(item) is not dict:
            continue
        role = _text(item.get("title"), 500)
        try:
            job_url = source_adapters.validate_public_job_url(item.get("url"))
        except ValueError:
            continue
        if not role:
            continue
        description = source_adapters._clean_html(_text(item.get("content"), 12000))
        posted = _text(item.get("publishedDate"), 100)
        listings.append(source_adapters.JobListing(job_url, role, "", "", description,
                                                   date_posted=posted))
    return listings
