"""Adapters for explicitly configured, policy-permitted public job sources."""

from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_LISTINGS = 200
MAX_JOB_PAGES_PER_SOURCE = 30
MAX_SEARCH_CRAWL_PAGES = 12
MAX_DESCRIPTION_CHARS = 12000
USER_AGENT = "JobBotDiscovery/0.1 (+https://github.com; configured job-source reader)"


class SourceError(Exception):
    """A bounded, safe-to-log source failure code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class JobListing:
    job_url: str
    role: str
    company: str
    location: str
    description: str
    employer_job_id: str = ""
    date_posted: str = ""


@dataclass(frozen=True)
class FeedResult:
    listings: list[JobListing]
    etag: str
    last_modified: str
    not_modified: bool = False


def validate_feed_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("feed URL must be a string no longer than 2000 characters")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("feed URL has an invalid port") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.fragment or port not in (None, 443) or
            any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        raise ValueError("feed URL must be an HTTPS URL without credentials or a fragment")
    if not _public_host(parsed.hostname):
        raise ValueError("feed host must resolve only to public addresses")
    return value


def _public_host(host: str) -> bool:
    try:
        addresses = {ipaddress.ip_address(host.split("%", 1)[0])}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0].split("%", 1)[0])
                for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError):
            return False
    return bool(addresses) and all(address.is_global for address in addresses)


def validate_public_job_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4000:
        raise ValueError("Invalid public job URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid public job URL") from None
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or
            parsed.username or parsed.password or port == 0 or
            any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or
            not _public_host(parsed.hostname)):
        raise ValueError("Invalid public job URL")
    return value


class _SameHostRedirects(HTTPRedirectHandler):
    def __init__(self, original_host: str, original_port: int | None):
        super().__init__()
        self.original_host = original_host.casefold().rstrip(".")
        self.original_port = original_port or 443
        self.redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirects += 1
        parsed = urlsplit(newurl)
        if (self.redirects > 2 or parsed.scheme != "https" or not parsed.hostname or
                parsed.username or parsed.password or (parsed.port or 443) != self.original_port or
                parsed.hostname.casefold().rstrip(".") != self.original_host or
                not _public_host(parsed.hostname)):
            raise SourceError("unsafe_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].casefold().replace("-", "_")


def _clean_html(value: str) -> str:
    class TextOnly(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []
            self.hidden = 0

        def handle_starttag(self, tag, attrs):
            if tag.casefold() in {"script", "style"}:
                self.hidden += 1
            elif tag.casefold() in {"p", "br", "div", "li", "tr", "h1", "h2", "h3"}:
                self.parts.append(" ")

        def handle_endtag(self, tag):
            if tag.casefold() in {"script", "style"} and self.hidden:
                self.hidden -= 1
            elif tag.casefold() in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
                self.parts.append(" ")

        def handle_data(self, data):
            if not self.hidden:
                self.parts.append(data)

    parser = TextOnly()
    try:
        parser.feed(value)
    except Exception:
        return " ".join(value.split())[:MAX_DESCRIPTION_CHARS]
    return " ".join("".join(parser.parts).split())[:MAX_DESCRIPTION_CHARS]


def _text(element: ET.Element | None, *, html: bool = False, limit: int = 1000) -> str:
    if element is None:
        return ""
    value = " ".join(part.strip() for part in element.itertext() if part.strip()).strip()
    if html:
        value = _clean_html(value)
    return value[:limit]


def _first_text(item: ET.Element, names: set[str], limit: int = 1000) -> str:
    for node in item.iter():
        if _local_name(node) in names:
            value = _text(node, limit=limit)
            if value:
                return value
    return ""


def _listing(item: ET.Element) -> JobListing | None:
    role = _first_text(item, {"title"}, 500)
    link = ""
    for node in item:
        if _local_name(node) != "link":
            continue
        rel = node.attrib.get("rel", "alternate").casefold()
        candidate = node.attrib.get("href", "") or (node.text or "").strip()
        if candidate and rel in {"alternate", ""}:
            link = candidate
            break
    if not link or not role:
        return None
    try:
        validate_public_job_url(link)
    except ValueError:
        return None

    description = ""
    for node in item.iter():
        if _local_name(node) in {"description", "summary", "content", "encoded"}:
            description = _text(node, html=True, limit=MAX_DESCRIPTION_CHARS)
            if description:
                break
    company = _first_text(item, {"company", "employer", "hiringorganization", "hiring_organization"}, 300)
    location = _first_text(item, {"joblocation", "job_location", "location", "addresslocality"}, 300)
    employer_job_id = _first_text(item, {"employer_job_id", "employerjobid"}, 300)
    date_posted = _first_text(item, {"pubdate", "published", "updated", "date", "dateposted"}, 100)
    return JobListing(link, role, company, location, description, employer_job_id, date_posted=date_posted)


def parse_feed(content: bytes) -> list[JobListing]:
    if len(content) > MAX_FEED_BYTES:
        raise SourceError("feed_too_large")
    upper = content.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise SourceError("unsafe_xml")
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise SourceError("invalid_feed") from None
    result: list[JobListing] = []
    for element in root.iter():
        if _local_name(element) not in {"item", "entry"}:
            continue
        listing = _listing(element)
        if listing:
            result.append(listing)
            if len(result) >= MAX_LISTINGS:
                break
    return result


def _jsonld_listings(document: str, page_url: str) -> list[JobListing]:
    """Extract schema.org JobPosting records embedded in a public HTML page."""
    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.active = False
            self.parts: list[str] = []
            self.scripts: list[str] = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            self.active = tag.casefold() == "script" and "ld+json" in attrs.get("type", "").casefold()
            if self.active:
                self.parts = []

        def handle_data(self, data):
            if self.active:
                self.parts.append(data)

        def handle_endtag(self, tag):
            if tag.casefold() == "script" and self.active:
                self.scripts.append("".join(self.parts))
                self.active = False

    parser = Scripts()
    parser.feed(document)
    records: list[dict] = []

    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            kind = value.get("@type", "")
            if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
                records.append(value)
            collect(value.get("@graph", []))

    for script in parser.scripts:
        try:
            collect(json.loads(script))
        except (ValueError, TypeError):
            continue
    listings: list[JobListing] = []
    for item in records[:MAX_LISTINGS]:
        title = _plain(item.get("title", ""), 500)
        raw_url = item.get("url") or item.get("sameAs") or page_url
        if isinstance(raw_url, list):
            raw_url = raw_url[0] if raw_url else page_url
        try:
            job_url = urljoin(page_url, str(raw_url))
            validate_public_job_url(job_url)
        except ValueError:
            continue
        org = item.get("hiringOrganization", {})
        company = _plain(org.get("name", "") if isinstance(org, dict) else "", 300)
        location_value = item.get("jobLocation", [])
        if isinstance(location_value, dict):
            location_value = [location_value]
        locations = []
        for place in location_value if isinstance(location_value, list) else []:
            address = place.get("address", {}) if isinstance(place, dict) else {}
            if isinstance(address, list):
                address = address[0] if address else {}
            if isinstance(address, dict):
                value = ", ".join(str(address.get(k, "")).strip() for k in
                                  ("addressLocality", "addressRegion", "addressCountry") if address.get(k))
                if value:
                    locations.append(value[:300])
        identifier = item.get("identifier", "")
        if isinstance(identifier, dict):
            identifier = identifier.get("value", "")
        description = _clean_html(str(item.get("description", "")))
        date_posted = _plain(item.get("datePosted", ""), 100)
        if title:
            listings.append(JobListing(job_url, title, company,
                                        "; ".join(dict.fromkeys(locations)),
                                        description, _plain(identifier, 300),
                                        date_posted=date_posted))
    return listings


def _plain(value: object, limit: int) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return _clean_html(str(value))[:limit]


def check_public_page_access(url: str, *,
                             robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] | None = None,
                             timeout: int = 10) -> None:
    """Check a public HTTPS page's robots rules without fetching the page."""
    try:
        validate_feed_url(url)
        parsed = urlsplit(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        cache = robots_cache if robots_cache is not None else {}
        if robots_url not in cache:
            robot = urllib.robotparser.RobotFileParser(robots_url)
            request = Request(robots_url, headers={"User-Agent": USER_AGENT}, method="GET")
            opener = build_opener(_SameHostRedirects(parsed.hostname or "", parsed.port))
            try:
                with opener.open(request, timeout=min(timeout, 10)) as response:
                    robots = response.read(256 * 1024 + 1)
            except HTTPError as exc:
                if exc.code not in {404, 410}:
                    raise SourceError("robots_unavailable") from None
                cache[robots_url] = None
            else:
                if len(robots) > 256 * 1024:
                    raise SourceError("robots_too_large")
                robot.parse(robots.decode("utf-8", "replace").splitlines())
                cache[robots_url] = robot
        if cache[robots_url] is not None and not cache[robots_url].can_fetch(USER_AGENT, url):
            raise SourceError("robots_disallowed")
    except SourceError:
        raise
    except (URLError, TimeoutError, OSError, ValueError):
        raise SourceError("robots_unavailable") from None


def _fetch_html(url: str, *, robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] | None = None,
                timeout: int = 20) -> str:
    """Fetch one public HTML URL after a same-host robots check."""
    check_public_page_access(url, robots_cache=robots_cache, timeout=timeout)
    parsed = urlsplit(url)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9"}
    opener = build_opener(_SameHostRedirects(parsed.hostname or "", parsed.port))
    try:
        with opener.open(Request(url, headers=headers, method="GET"), timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise SourceError("not_html")
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > MAX_FEED_BYTES:
                raise SourceError("page_too_large")
            content = response.read(MAX_FEED_BYTES + 1)
            if len(content) > MAX_FEED_BYTES:
                raise SourceError("page_too_large")
            charset = response.headers.get_content_charset() or "utf-8"
            document = content.decode(charset, "replace")
            return document
    except SourceError:
        raise
    except HTTPError as exc:
        raise SourceError("http_error") from None
    except (URLError, TimeoutError, OSError, ValueError, LookupError):
        raise SourceError("network_error") from None


def parse_date_string(date_str: str) -> datetime | None:
    if not date_str or not isinstance(date_str, str):
        return None
    s = date_str.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    return None


def is_within_timespan(date_str: str, timespan: str) -> bool:
    if not timespan or timespan == "any":
        return True
    if not date_str:
        return True
    dt = parse_date_string(date_str)
    if not dt:
        return True
    seconds_map = {
        "24h": 86400, "1d": 86400,
        "3d": 3 * 86400,
        "7d": 7 * 86400,
        "14d": 14 * 86400,
        "30d": 30 * 86400,
    }
    span = seconds_map.get(timespan)
    if not span:
        try:
            span = int(timespan) * 86400
        except ValueError:
            return True
    now = datetime.now(timezone.utc)
    return dt >= (now - timedelta(seconds=span))


def fetch_job_posting_exact(url: str) -> JobListing | None:
    """Recheck one permitted page; do not crawl links or guess a replacement URL."""
    try:
        validate_feed_url(url)
    except ValueError:
        raise SourceError("unsafe_page_url") from None
    document = _fetch_html(url)
    return next((item for item in _jsonld_listings(document, url) if item.job_url == url), None)


def fetch_job_page(url: str, *, max_listings: int = MAX_LISTINGS) -> FeedResult:
    """Scrape a configured public job page and a bounded set of same-site job links.

    Only structured schema.org JobPosting records are turned into queue items.
    """
    limit = max(1, min(MAX_LISTINGS, max_listings))
    try:
        validate_feed_url(url)
    except ValueError:
        raise SourceError("unsafe_page_url") from None
    root = urlsplit(url)
    document = _fetch_html(url)
    listings = _jsonld_listings(document, url)

    class JobLinks(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag.casefold() != "a":
                return
            href = dict(attrs).get("href", "")
            if not href:
                return
            absolute = urljoin(url, href)
            parsed = urlsplit(absolute)
            path = parsed.path.casefold()
            if (parsed.scheme == "https" and parsed.hostname and
                    parsed.hostname.casefold().rstrip(".") == (root.hostname or "").casefold().rstrip(".") and
                    any(token in path for token in ("job", "career", "requisition", "opening", "position"))):
                self.links.append(absolute)

    links = JobLinks()
    links.feed(document)
    seen = {listing.job_url for listing in listings}
    candidates = list(dict.fromkeys(links.links))[:MAX_JOB_PAGES_PER_SOURCE]
    for candidate in candidates:
        if len(listings) >= limit:
            break
        try:
            validate_feed_url(candidate)
            child = _fetch_html(candidate)
        except (ValueError, SourceError):
            continue
        time.sleep(0.5)
        for listing in _jsonld_listings(child, candidate):
            if listing.job_url not in seen:
                seen.add(listing.job_url)
                listings.append(listing)
                if len(listings) >= limit:
                    break
    return FeedResult(listings[:limit], "", "")


def crawl_search_collection(url: str, *, max_pages: int = MAX_SEARCH_CRAWL_PAGES,
                            max_depth: int = 2) -> list[JobListing]:
    """Follow bounded same-host job links from one public search collection.

    Every fetched page passes the normal HTTPS, public-host, robots, redirect,
    and size checks. Only pages with structured JobPosting data yield jobs.
    """
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_SEARCH_CRAWL_PAGES:
        raise ValueError("Invalid search crawl page limit")
    if type(max_depth) is not int or not 1 <= max_depth <= 2:
        raise ValueError("Invalid search crawl depth")
    try:
        validate_feed_url(url)
    except ValueError:
        raise SourceError("unsafe_page_url") from None
    host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    pending = deque([(url, 0)])
    seen_pages = {url}
    seen_jobs: set[str] = set()
    listings: list[JobListing] = []
    fetched = 0
    deadline = time.monotonic() + 30
    robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    class JobLinks(HTMLParser):
        def __init__(self, page_url: str):
            super().__init__()
            self.page_url = page_url
            self.links: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag.casefold() != "a":
                return
            href = dict(attrs).get("href", "")
            if not href:
                return
            try:
                absolute = urljoin(self.page_url, href)
                parsed = urlsplit(absolute)
            except ValueError:
                return
            path = parsed.path.casefold()
            if (parsed.scheme == "https" and parsed.hostname and
                    parsed.hostname.casefold().rstrip(".") == host and
                    not parsed.fragment and not parsed.username and not parsed.password and
                    any(token in path for token in
                        ("job", "career", "requisition", "opening", "position", "vacanc"))):
                self.links.append(absolute)

    while pending and fetched < max_pages and time.monotonic() < deadline:
        page_url, depth = pending.popleft()
        try:
            document = _fetch_html(page_url, robots_cache=robots_cache, timeout=8)
        except SourceError:
            if depth == 0:
                raise
            continue
        fetched += 1
        for listing in _jsonld_listings(document, page_url):
            if (listing.job_url != url and
                    (urlsplit(listing.job_url).hostname or "").casefold().rstrip(".") == host and
                    listing.job_url not in seen_jobs):
                seen_jobs.add(listing.job_url)
                listings.append(listing)
                if len(listings) >= MAX_LISTINGS:
                    return listings
        if depth < max_depth:
            links = JobLinks(page_url)
            links.feed(document)
            for candidate in links.links:
                if len(seen_pages) >= max_pages * 5:
                    break
                if candidate not in seen_pages:
                    seen_pages.add(candidate)
                    pending.append((candidate, depth + 1))
        if pending:
            parsed = urlsplit(page_url)
            robot = robots_cache.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
            crawl_delay = robot.crawl_delay(USER_AGENT) if robot is not None else None
            if crawl_delay is not None and crawl_delay > 5:
                break
            time.sleep(max(0.5, float(crawl_delay or 0)))
    return listings


def fetch_feed(url: str, *, etag: str = "", last_modified: str = "") -> FeedResult:
    try:
        validate_feed_url(url)
    except ValueError:
        raise SourceError("unsafe_feed_url") from None
    parsed = urlsplit(url)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9"}
    if etag:
        headers["If-None-Match"] = etag[:500]
    if last_modified:
        headers["If-Modified-Since"] = last_modified[:200]
    request = Request(url, headers=headers, method="GET")
    opener = build_opener(_SameHostRedirects(parsed.hostname or "", parsed.port))
    try:
        with opener.open(request, timeout=20) as response:
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > MAX_FEED_BYTES:
                raise SourceError("feed_too_large")
            content = response.read(MAX_FEED_BYTES + 1)
            if len(content) > MAX_FEED_BYTES:
                raise SourceError("feed_too_large")
            return FeedResult(
                parse_feed(content),
                response.headers.get("ETag", "")[:500],
                response.headers.get("Last-Modified", "")[:200],
            )
    except HTTPError as exc:
        if exc.code == 304:
            return FeedResult([], etag, last_modified, True)
        raise SourceError("http_error") from None
    except SourceError:
        raise
    except (URLError, TimeoutError, OSError, ValueError):
        raise SourceError("network_error") from None
