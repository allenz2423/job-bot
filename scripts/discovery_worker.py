"""Read configured, policy-permitted public job feeds into per-user queues."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sqlite3
import time
from dataclasses import replace
from typing import Any, Callable
from urllib.parse import urlsplit

try:
    from . import control_store, embedding_ranker, global_job_pool, job_queue, local_llm, search_adapters, source_adapters
except ImportError:
    import control_store
    import embedding_ranker
    import global_job_pool
    import job_queue
    import local_llm
    import search_adapters
    import source_adapters


LOG = logging.getLogger("jobbot.discovery")
_COLLECTION_CRAWL_CACHE: dict[str, tuple[float, list[source_adapters.JobListing], str]] = {}
SAVED_SEARCH_INTERVAL_SECONDS = 6 * 60 * 60


class SearchIdentityUnavailable(Exception):
    """A public search result cannot be safely classified right now."""


def _crawl_collection_cached(job_url: str) -> list[source_adapters.JobListing]:
    """Avoid revisiting the same public collection URL for every search phrase."""
    now = time.monotonic()
    cached = _COLLECTION_CRAWL_CACHE.get(job_url)
    if cached and cached[0] > now:
        if cached[2]:
            raise source_adapters.SourceError(cached[2])
        return cached[1]
    if len(_COLLECTION_CRAWL_CACHE) >= 100:
        _COLLECTION_CRAWL_CACHE.clear()
    try:
        children = source_adapters.crawl_search_collection(job_url, max_pages=10, max_depth=2)
    except source_adapters.SourceError as exc:
        _COLLECTION_CRAWL_CACHE[job_url] = (now + 60, [], exc.code)
        raise
    _COLLECTION_CRAWL_CACHE[job_url] = (now + 60, children, "")
    return children


def _cache_connect() -> sqlite3.Connection:
    path = control_store.STATE_DIR / "discovery_cache.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(path, timeout=15)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""CREATE TABLE IF NOT EXISTS feed_cache (
        feed_key TEXT PRIMARY KEY,
        etag TEXT NOT NULL DEFAULT '',
        last_modified TEXT NOT NULL DEFAULT '',
        last_checked_at REAL NOT NULL DEFAULT 0
    )""")
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(feed_cache)")}
    if "last_checked_at" not in columns:
        connection.execute("ALTER TABLE feed_cache ADD COLUMN last_checked_at REAL NOT NULL DEFAULT 0")
    connection.execute("""CREATE TABLE IF NOT EXISTS search_attempts (
        search_key TEXT PRIMARY KEY,
        attempted_at REAL NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS provider_requests (
        provider_key TEXT NOT NULL,
        attempted_at REAL NOT NULL
    )""")
    connection.execute("""CREATE INDEX IF NOT EXISTS provider_requests_recent
        ON provider_requests(provider_key, attempted_at)""")
    connection.commit()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return connection


def _cache_key(user_id: str, url: str) -> str:
    return hashlib.sha256((user_id + "\0" + url).encode("utf-8")).hexdigest()


def _claim_feed_request(user_id: str, url: str, interval_seconds: int) -> tuple[str, str] | None:
    key = _cache_key(user_id, url)
    connection = _cache_connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT etag,last_modified,last_checked_at FROM feed_cache WHERE feed_key=?", (key,)
        ).fetchone()
        now = time.time()
        if row and now - float(row[2]) < interval_seconds:
            connection.commit()
            return None
        connection.execute(
            "INSERT INTO feed_cache(feed_key,last_checked_at) VALUES(?,?) "
            "ON CONFLICT(feed_key) DO UPDATE SET last_checked_at=excluded.last_checked_at",
            (key, now),
        )
        connection.commit()
        return (str(row[0]), str(row[1])) if row else ("", "")
    finally:
        connection.close()


def _save_validators(user_id: str, url: str, etag: str, last_modified: str) -> None:
    key = _cache_key(user_id, url)
    connection = _cache_connect()
    try:
        connection.execute(
            "INSERT INTO feed_cache(feed_key,etag,last_modified) VALUES(?,?,?) "
            "ON CONFLICT(feed_key) DO UPDATE SET etag=excluded.etag,last_modified=excluded.last_modified",
            (key, etag[:500], last_modified[:200]),
        )
        connection.commit()
    finally:
        connection.close()


def _approved_preferences(profile: dict[str, Any]) -> dict[str, object]:
    fields = profile.get("data", {}).get("fields", {})
    selected: dict[str, object] = {}
    for field_id, payload in fields.items():
        if not field_id.startswith("preferences.") or payload.get("status") != "known":
            continue
        selected[field_id.removeprefix("preferences.")] = payload.get("value")
    return selected


def _ranking_queries(user: dict[str, object], preferences: dict[str, object]) -> list[str]:
    """Build relevance queries from explicit searches and soft preferences."""
    queries: list[str] = []
    ignored = {"excluded_titles", "hard_dealbreakers"}

    def add(label: str, value: object) -> None:
        if isinstance(value, str) and value.strip():
            queries.append(f"{label}: {' '.join(value.split())[:700]}")
        elif isinstance(value, list):
            for item in value[:100]:
                add(label, item)
        elif isinstance(value, dict):
            for key, item in list(value.items())[:50]:
                add(f"{label} {str(key)[:80]}", item)

    for key, value in preferences.items():
        if key not in ignored:
            add(key.replace("_", " "), value)
    try:
        for search in control_store.list_discovery_searches(user):
            if search.get("enabled"):
                phrase = str(search.get("phrase") or "").strip()
                location = str(search.get("location") or "").strip()
                if phrase:
                    add("Job search", f"{phrase}; location {location}" if location else phrase)
        for source in control_store.list_discovery_sources(user):
            if not source.get("enabled") or not source.get("permitted"):
                continue
            for title in re.split(r"[\n,]+", str(source.get("job_title") or "")):
                if title.strip():
                    add("Target role", title)
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        LOG.warning("Ranking hints unavailable for user_id=%s", user.get("user_id"))
    return list(dict.fromkeys(queries))[:100]


def _ranking(queries: list[str], preferences: dict[str, object],
             listing: source_adapters.JobListing) -> tuple[float | None, str, str]:
    if not queries:
        return None, "Add a job search phrase or profile preference to enable a relevance score.", ""
    result = embedding_ranker.score_listing(queries, {
        "title": listing.role,
        "company": listing.company,
        "location": listing.location,
        "description": listing.description,
    })
    if result is None:
        return None, "The local embedding ranker is unavailable; the listing remains queued.", ""
    score = float(result["score"])
    next_action = (
        f"Embedding similarity {score:g}/100 advisory; this is not a probability or qualification check."
    )
    review_reason = ""
    # Keep the existing local LLM as a separate, optional hard-dealbreaker
    # review signal. It does not produce or suppress the embedding score.
    if preferences and local_llm.configured():
        try:
            outcome = local_llm.rank_listing(preferences, {
                "title": listing.role,
                "company": listing.company,
                "location": listing.location,
                "description": listing.description,
            })
        except (RuntimeError, ValueError):
            outcome = None
        if outcome and outcome.get("possible_hard_conflict"):
            review_reason = "possible_hard_conflict"
            next_action += " The local model flagged a possible hard-dealbreaker conflict; review it."
    return score, next_action[:1000], review_reason


def _identify_search_listings(listings: list[source_adapters.JobListing],
                              target_title: str,
                              crawl_budget: list[int] | None = None) -> list[source_adapters.JobListing]:
    """Classify search results and inspect a few public collection pages."""
    if not listings:
        return []
    if crawl_budget is None:
        crawl_budget = [0]
    identified = local_llm.identify_search_results([
        {"title": listing.role, "snippet": listing.description, "url": listing.job_url}
        for listing in listings
    ])
    if identified is None:
        raise SearchIdentityUnavailable("local_model_identity_unavailable")
    found: list[source_adapters.JobListing] = []
    seen_urls: set[str] = set()
    for listing, identity in zip(listings, identified):
        known_collection = search_adapters.is_known_collection_page(listing.job_url)
        if identity["is_job_posting"] and not known_collection:
            direct = replace(listing, role=str(identity["role"]),
                             company=str(identity["company"]))
            if direct.job_url not in seen_urls:
                found.append(direct)
                seen_urls.add(direct.job_url)
            continue
        if not known_collection and identity["classification"] != "collection_page":
            continue
        if crawl_budget[0] >= 2:
            continue
        crawl_budget[0] += 1
        try:
            children = _crawl_collection_cached(listing.job_url)
        except source_adapters.SourceError as exc:
            LOG.info("Collection crawl skipped reason=%s", exc.code)
            continue
        if target_title and children:
            matches = local_llm.match_job_titles([target_title], [child.role for child in children])
            children = [child for child, matched in zip(children, matches) if matched]
        for child in children:
            if child.job_url not in seen_urls and not search_adapters.is_known_collection_page(child.job_url):
                # A board's schema identifier is not a confirmed employer job ID.
                found.append(replace(child, employer_job_id=""))
                seen_urls.add(child.job_url)
    return found


def _eligible_users() -> list[tuple[dict[str, object], dict[str, Any], list[dict[str, object]],
                                    list[dict[str, object]], dict[str, str] | None]]:
    result: list[tuple[dict[str, object], dict[str, Any], list[dict[str, object]],
                       list[dict[str, object]], dict[str, str] | None]] = []
    for user in control_store.list_worker_users():
        if not user["active"] or user["must_change_password"]:
            continue
        try:
            settings = control_store.get_settings(user)
            if settings["discovery_paused"]:
                continue
            profile = control_store.get_profile(user)
            sources = control_store.list_discovery_sources(user)
            searches = control_store.list_discovery_searches(user)
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            LOG.warning("Skipping user_id=%s reason=tenant_state_unavailable", user["user_id"])
            continue
        try:
            adzuna = control_store.adzuna_credentials(user)
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            LOG.warning("Skipping Adzuna search user_id=%s reason=integration_unavailable", user["user_id"])
            adzuna = None
        if not profile["version"] or profile["approved_version"] != profile["version"]:
            LOG.info("Skipping user_id=%s reason=profile_not_approved", user["user_id"])
            continue
        enabled = [source for source in sources if source["enabled"] and source["permitted"]]
        enabled_searches = [search for search in searches if search["enabled"]]
        if enabled or enabled_searches:
            result.append((user, profile, enabled, enabled_searches, adzuna))
    return result


def _can_record(user: dict[str, object], expected_version: int,
                source: dict[str, object]) -> bool:
    try:
        current_user = next(
            (record for record in control_store.list_worker_users()
             if record["user_id"] == user["user_id"]), None
        )
        settings = control_store.get_settings(user)
        profile = control_store.get_profile(user)
        sources = control_store.list_discovery_sources(user)
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        LOG.warning("Skipping user_id=%s reason=tenant_state_unavailable", user["user_id"])
        return False
    return bool(
        current_user and current_user["active"] and
        not current_user["must_change_password"] and
        not settings["discovery_paused"] and
        profile["version"] == expected_version and
        profile["approved_version"] == expected_version and
        any(current["source_id"] == source["source_id"] and
            current["enabled"] and current["permitted"] for current in sources)
    )


def _can_record_search(user: dict[str, object], expected_version: int,
                       search: dict[str, object], provider_name: str,
                       credential_version: str) -> bool:
    try:
        current_user = next(
            (record for record in control_store.list_worker_users()
             if record["user_id"] == user["user_id"]), None
        )
        settings = control_store.get_settings(user)
        profile = control_store.get_profile(user)
        searches = control_store.list_discovery_searches(user)
        provider = control_store.adzuna_status(user) if provider_name == "adzuna" else None
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        LOG.warning("Skipping user_id=%s reason=tenant_state_unavailable", user["user_id"])
        return False
    return bool(
        current_user and current_user["active"] and
        not current_user["must_change_password"] and
        not settings["discovery_paused"] and
        profile["version"] == expected_version and
        profile["approved_version"] == expected_version and
        (provider_name != "adzuna" or
         (provider and provider["enabled"] and provider["updated_at"] == credential_version)) and
        any(current["search_id"] == search["search_id"] and
            current["phrase"] == search["phrase"] and
            current["location"] == search["location"] and
            current["enabled"] for current in searches)
    )


def _record_listings(user: dict[str, object], profile: dict[str, Any],
                     source_name: str, listings: list[source_adapters.JobListing],
                     allowed: Callable[[], bool], source_kind: str,
                     max_new_items: int | None = None) -> tuple[int, int, bool]:
    user_id = str(user["user_id"])
    db_path = control_store.tenant_db_path(user)
    preferences = _approved_preferences(profile)
    ranking_queries = _ranking_queries(user, preferences)
    added = 0
    review = 0
    complete = True
    for listing in listings:
        if max_new_items is not None and added >= max_new_items:
            break
        if not allowed():
            complete = False
            break
        score, next_action, review_reason = _ranking(ranking_queries, preferences, listing)
        if not allowed():
            complete = False
            break
        payload: dict[str, object] = {
            "job_url": listing.job_url,
            "job_source": source_name,
            "employer_job_id": listing.employer_job_id,
            "company": listing.company,
            "role": listing.role,
            "location": listing.location,
            "next_action": next_action,
        }
        if review_reason:
            payload["review_reason"] = review_reason
        if score is not None:
            payload["match_score"] = score
        try:
            pooled = None
            if source_kind != "adzuna":
                try:
                    pooled = global_job_pool.upsert_listing(listing, source_kind)
                except ValueError:
                    # A public result can still contain a personalized URL.
                    # Keep it private when its URL cannot safely be shared.
                    pass
            queued = job_queue.enqueue_job(user_id, payload, db_path=db_path,
                                           pool_id=str(pooled["pool_id"]) if pooled else "")
        except (ValueError, OSError, RuntimeError, sqlite3.Error):
            LOG.warning("Listing skipped user_id=%s reason=queue_write_failed", user_id)
            complete = False
            continue
        if queued.get("created"):
            added += 1
        if queued.get("state") == "needs_review":
            review += 1
    return added, review, complete


def _permitted_job_page(job_url: str) -> bool:
    """Require a currently enabled, user-confirmed source on the same host."""
    host = (urlsplit(job_url).hostname or "").casefold().rstrip(".")
    if not host:
        return False
    for user in control_store.list_worker_users():
        if not user["active"] or user["must_change_password"]:
            continue
        try:
            if control_store.get_settings(user)["discovery_paused"]:
                continue
            sources = control_store.list_discovery_sources(user)
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            continue
        for source in sources:
            if not source["enabled"] or not source["permitted"] or source["source_type"] not in {"job_page", "scrape_jobs"}:
                continue
            source_host = (urlsplit(str(source["url"])).hostname or "").casefold().rstrip(".")
            if source_host == host:
                return True
    return False


def _process_rescrape_requests(limit: int = 5) -> int:
    processed = 0
    for _ in range(limit):
        request = global_job_pool.claim_rescrape()
        if request is None:
            break
        pool_id = str(request["pool_id"])
        token = str(request["lease_token"])
        job_url = str(request["job_url"])
        if not _permitted_job_page(job_url):
            global_job_pool.finish_rescrape(pool_id, token, "unresolved",
                                            error="source_not_permitted")
            processed += 1
            continue
        try:
            listing = source_adapters.fetch_job_posting_exact(job_url)
            if listing is None:
                global_job_pool.finish_rescrape(pool_id, token, "unresolved",
                                                error="posting_not_found")
            else:
                global_job_pool.upsert_listing(listing, "job_page")
                global_job_pool.finish_rescrape(pool_id, token, "verified")
        except source_adapters.SourceError as exc:
            code = exc.code if exc.code in global_job_pool.RESCRAPE_ERRORS else "unknown"
            outcome = "retry" if code in {"network_error", "robots_unavailable"} else "unresolved"
            global_job_pool.finish_rescrape(pool_id, token, outcome, error=code)
        processed += 1
    return processed


def _resume_verified_rescrapes() -> int:
    resumed = 0
    for user in control_store.list_worker_users():
        if not user["active"] or user["must_change_password"]:
            continue
        try:
            if control_store.get_settings(user)["discovery_paused"]:
                continue
            profile = control_store.get_profile(user)
            if not profile["version"] or profile["approved_version"] != profile["version"]:
                continue
            resumed += job_queue.resume_verified_rescrapes(
                str(user["user_id"]), db_path=control_store.tenant_db_path(user))
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            LOG.warning("Rescrape resume skipped user_id=%s reason=tenant_state_unavailable", user["user_id"])
    return resumed


def _search_request_key(user_id: str, search_id: int | str, provider: str,
                        credentials: dict[str, str]) -> str:
    credential_version = credentials["updated_at"]
    return hashlib.sha256(f"{provider}\0{user_id}\0{search_id}\0{credential_version}".encode()).hexdigest()


def _prepare_requested_source_run(user: dict[str, object], source: dict[str, Any]) -> None:
    requested_at = str(source.get("run_requested_at") or "")
    if not requested_at:
        return
    user_id = str(user["user_id"])
    source_id = int(source["source_id"])
    feed_url = str(source.get("url") or "")
    connection = _cache_connect()
    try:
        if feed_url:
            cache_key = _cache_key(user_id, f"source:{source_id}:{feed_url}")
            connection.execute(
                "UPDATE feed_cache SET last_checked_at=0 WHERE feed_key=?", (cache_key,)
            )
        else:
            targets = local_llm.parse_allowed_titles(str(source.get("job_title") or ""))
            if not targets:
                targets = [str(source.get("name") or "")]
            for target in targets:
                request_key = f"source:{source_id}:{target.casefold()}"
                search_key = _search_request_key(
                    user_id, request_key, "searxng", {"updated_at": "local"})
                connection.execute(
                    "UPDATE search_attempts SET attempted_at=0 WHERE search_key=?", (search_key,)
                )
        connection.commit()
    finally:
        connection.close()
    # The reset cadence marker is durable before the request is acknowledged. If
    # discovery exits here, the normal worker pass will still see the source due.
    cleared = control_store.clear_discovery_source_run_request(user, source_id, requested_at)
    LOG.info("On-demand source run prepared source_id=%s acknowledged=%s", source_id, cleared)


def _search_attempted_at(user_id: str, search_id: int | str, provider: str,
                         credentials: dict[str, str]) -> float:
    key = _search_request_key(user_id, search_id, provider, credentials)
    connection = _cache_connect()
    try:
        row = connection.execute(
            "SELECT attempted_at FROM search_attempts WHERE search_key=?", (key,)
        ).fetchone()
        return float(row[0]) if row else 0.0
    finally:
        connection.close()


def _reserve_provider_request(connection: sqlite3.Connection, provider: str,
                              credentials: dict[str, str], now: float) -> bool:
    material = (credentials.get("app_id", "") + "\0" + credentials.get("app_key", "")
                if provider == "adzuna" else "local-searxng")
    provider_key = hashlib.sha256(f"{provider}\0{material}".encode()).hexdigest()
    connection.execute("DELETE FROM provider_requests WHERE attempted_at<?", (now - 86400,))
    daily = int(connection.execute(
        "SELECT COUNT(*) FROM provider_requests WHERE provider_key=? AND attempted_at>?",
        (provider_key, now - 86400),
    ).fetchone()[0])
    minute = int(connection.execute(
        "SELECT COUNT(*) FROM provider_requests WHERE provider_key=? AND attempted_at>?",
        (provider_key, now - 60),
    ).fetchone()[0])
    daily_limit, minute_limit = ((200, 20) if provider == "adzuna" else (500, 20))
    if daily >= daily_limit or minute >= minute_limit:
        return False
    connection.execute(
        "INSERT INTO provider_requests(provider_key,attempted_at) VALUES(?,?)",
        (provider_key, now),
    )
    return True


def _reserve_additional_search_page(provider: str, credentials: dict[str, str]) -> bool:
    connection = _cache_connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        reserved = _reserve_provider_request(connection, provider, credentials, time.time())
        connection.commit()
        return reserved
    finally:
        connection.close()


def _search_request_due(user_id: str, search_id: int | str, provider: str,
                        credentials: dict[str, str], interval_seconds: int) -> bool:
    # The calling job or saved phrase chooses the cadence; provider limits still
    # bound spend and protect against accidental request bursts.
    key = _search_request_key(user_id, search_id, provider, credentials)
    connection = _cache_connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = time.time()
        connection.execute("DELETE FROM provider_requests WHERE attempted_at<?", (now - 86400,))
        row = connection.execute(
            "SELECT attempted_at FROM search_attempts WHERE search_key=?", (key,)
        ).fetchone()
        if row and now - float(row[0]) < interval_seconds:
            connection.commit()
            return False
        if not _reserve_provider_request(connection, provider, credentials, now):
            connection.commit()
            return False
        connection.execute(
            "INSERT INTO search_attempts(search_key,attempted_at) VALUES(?,?) "
            "ON CONFLICT(search_key) DO UPDATE SET attempted_at=excluded.attempted_at",
            (key, now),
        )
        connection.commit()
        return True
    finally:
        connection.close()


def _search_request_failed(user_id: str, search_id: int | str, provider: str,
                           credentials: dict[str, str], interval_seconds: int) -> None:
    """Retry a failed request after 15 minutes, still under the provider cap."""
    key = _search_request_key(user_id, search_id, provider, credentials)
    connection = _cache_connect()
    try:
        connection.execute(
            "UPDATE search_attempts SET attempted_at=? WHERE search_key=?",
            (time.time() - interval_seconds + 900, key),
        )
        connection.commit()
    finally:
        connection.close()


def discover_once() -> dict[str, int]:
    _process_rescrape_requests()
    _resume_verified_rescrapes()
    users = _eligible_users()
    if not users:
        LOG.info("Discovery idle reason=no_active_approved_unpaused_users_with_sources_or_searches")
        return {"feeds": 0, "searches": 0, "listings": 0, "enqueued": 0, "review": 0}

    counts = {"feeds": 0, "searches": 0, "listings": 0, "enqueued": 0, "review": 0}
    for user, original_profile, sources, searches, adzuna in users:
        user_id = str(user["user_id"])
        for source in sources:
            if not _can_record(user, int(original_profile["version"]), source):
                continue
            _prepare_requested_source_run(user, source)
            feed_url = str(source.get("url") or "")
            job_title = str(source.get("job_title") or "").strip()
            allowed_titles = local_llm.parse_allowed_titles(job_title)
            max_jobs = max(1, min(200, int(source.get("max_jobs") or 25)))
            interval_seconds = int(source.get("interval_minutes") or 360) * 60
            timespan = str(source.get("timespan") or "any")
            source_type = source.get("source_type", "job_page")

            raw_key = f"source:{source['source_id']}:{feed_url}"
            feed_key = _cache_key(user_id, raw_key)[:12]
            feed_etag, feed_last_modified = "", ""
            source_search_pages = 0
            source_search_results = 0
            identified_count = 0
            matched_count = 0
            source_added = 0
            source_review = 0
            complete = True
            allowed = lambda: _can_record(user, int(original_profile["version"]), source)
            source_name = "SearXNG" if not feed_url else str(source["name"])
            source_kind = ("searxng" if not feed_url else
                           ("feed" if source_type == "feed" else "job_page"))
            try:
                if feed_url:
                    validators = _claim_feed_request(user_id, raw_key, interval_seconds)
                    if validators is None:
                        continue
                    etag, last_modified = validators
                    if source_type in {"job_page", "scrape_jobs"}:
                        feed = source_adapters.fetch_job_page(feed_url, max_listings=max_jobs)
                    else:
                        feed = source_adapters.fetch_feed(feed_url, etag=etag, last_modified=last_modified)
                    counts["feeds"] += 1
                    if feed.not_modified:
                        LOG.info("Feed key=%s result=not_modified", feed_key)
                        continue
                    feed_etag, feed_last_modified = feed.etag, feed.last_modified
                    raw_listings = feed.listings
                    identified_count = len(raw_listings)
                    time_filtered = [
                        item for item in raw_listings
                        if source_adapters.is_within_timespan(getattr(item, "date_posted", ""), timespan)
                    ]
                    if allowed_titles and time_filtered:
                        matches = local_llm.match_job_titles(
                            allowed_titles, [item.role for item in time_filtered],
                        )
                        matched_listings = [item for item, matched in zip(time_filtered, matches) if matched]
                    else:
                        matched_listings = time_filtered
                    matched_count = len(matched_listings)
                    counts["listings"] += matched_count
                    source_added, source_review, complete = _record_listings(
                        user, original_profile, source_name, matched_listings, allowed,
                        source_kind, max_new_items=max_jobs,
                    )
                    if complete and allowed():
                        _save_validators(user_id, raw_key, feed_etag, feed_last_modified)
                else:
                    seen_urls: set[str] = set()
                    search_targets = allowed_titles or [str(source["name"])]
                    search_credentials = {"updated_at": "local"}
                    request_budget_exhausted = False
                    for target in search_targets:
                        if not allowed() or source_added >= max_jobs:
                            break
                        request_key = f"source:{source['source_id']}:{target.casefold()}"
                        if not _search_request_due(
                            user_id, request_key, "searxng", search_credentials, interval_seconds):
                            last_attempt = _search_attempted_at(
                                user_id, request_key, "searxng", search_credentials,
                            )
                            if not last_attempt or time.time() - last_attempt >= interval_seconds:
                                request_budget_exhausted = True
                                break
                            continue
                        crawl_budget = [0]
                        for page_number in range(1, search_adapters.SEARXNG_MAX_PAGES_PER_QUERY + 1):
                            if not allowed() or source_added >= max_jobs:
                                break
                            if page_number > 1 and not _reserve_additional_search_page(
                                    "searxng", search_credentials):
                                request_budget_exhausted = True
                                break
                            try:
                                page_results = search_adapters.search_searxng(
                                    target, "", pageno=page_number)
                                counts["searches"] += 1
                                source_search_pages += 1
                                source_search_results += len(page_results)
                                if not page_results:
                                    break
                                identified = _identify_search_listings(page_results, target, crawl_budget)
                                page_unique = []
                                for listing in identified:
                                    if listing.job_url not in seen_urls:
                                        seen_urls.add(listing.job_url)
                                        page_unique.append(listing)
                                identified_count += len(page_unique)
                                time_filtered = [
                                    item for item in page_unique
                                    if source_adapters.is_within_timespan(
                                        getattr(item, "date_posted", ""), timespan,
                                    )
                                ]
                                if allowed_titles and time_filtered:
                                    matches = local_llm.match_job_titles(
                                        allowed_titles, [item.role for item in time_filtered],
                                    )
                                    matched_listings = [
                                        item for item, matched in zip(time_filtered, matches) if matched
                                    ]
                                else:
                                    matched_listings = time_filtered
                                matched_count += len(matched_listings)
                                counts["listings"] += len(matched_listings)
                                added, reviewed, page_complete = _record_listings(
                                    user, original_profile, source_name, matched_listings,
                                    allowed, source_kind, max_new_items=max_jobs - source_added,
                                )
                                source_added += added
                                source_review += reviewed
                                complete = complete and page_complete
                                if source_added >= max_jobs:
                                    break
                            except (search_adapters.SearchError, SearchIdentityUnavailable) as exc:
                                _search_request_failed(
                                    user_id, request_key, "searxng", search_credentials,
                                    interval_seconds,
                                )
                                complete = False
                                LOG.warning("Source key=%s result=search_error code=%s", feed_key,
                                            getattr(exc, "code", str(exc)))
                                break
                            if page_number < search_adapters.SEARXNG_MAX_PAGES_PER_QUERY:
                                time.sleep(3)
                        # A bad/unavailable result page should not prevent the
                        # other configured titles from filling this source's
                        # target. `allowed()` is checked before every title and
                        # page, so changed approval or pause state still stops
                        # the run immediately.
                        if source_added >= max_jobs or request_budget_exhausted:
                            break
                        time.sleep(3)
                    if source_added < max_jobs and request_budget_exhausted:
                        LOG.info("Source key=%s result=provider_request_limit", feed_key)
            except (source_adapters.SourceError, search_adapters.SearchError) as exc:
                LOG.warning("Source key=%s result=error code=%s", feed_key, getattr(exc, "code", str(exc)))
                continue

            if (not feed_url and source_search_pages == 0 and not source_added and
                    not request_budget_exhausted and complete):
                continue
            counts["enqueued"] += source_added
            counts["review"] += source_review
            target_met = source_added >= max_jobs
            LOG.info("Source key=%s search_pages=%d search_results=%d identified_jobs=%d "
                     "matched_jobs=%d new_target=%d new_queue_items=%d target_met=%s "
                     "request_limit=%s complete=%s",
                     feed_key, source_search_pages, source_search_results, identified_count,
                     matched_count, max_jobs, source_added, target_met,
                     request_budget_exhausted if not feed_url else False, complete)
        providers = [("searxng", {"updated_at": "local"})]
        if adzuna:
            providers.append(("adzuna", adzuna))
        for provider_name, credentials in providers:
            if not credentials:
                continue
            ordered_searches = sorted(
                searches,
                key=lambda search: (
                    _search_attempted_at(user_id, int(search["search_id"]), provider_name, credentials),
                    int(search["search_id"]),
                ),
            )
            for search in ordered_searches:
                credential_version = str(credentials["updated_at"])
                search_key = hashlib.sha256(
                    f"{provider_name}\0{user_id}\0{search['search_id']}\0{credential_version}".encode()).hexdigest()[:12]
                allowed = lambda: _can_record_search(
                    user, int(original_profile["version"]), search, provider_name, credential_version)
                if not allowed() or not _search_request_due(
                    user_id, int(search["search_id"]), provider_name, credentials,
                    SAVED_SEARCH_INTERVAL_SECONDS):
                    continue
                try:
                    if provider_name == "adzuna":
                        listings = search_adapters.search_adzuna(
                            str(search["phrase"]), str(search["location"]), credentials)
                    else:
                        listings = _identify_search_listings(search_adapters.search_searxng(
                            str(search["phrase"]), str(search["location"])), str(search["phrase"]))
                except (search_adapters.SearchError, SearchIdentityUnavailable) as exc:
                    _search_request_failed(
                        user_id, int(search["search_id"]), provider_name, credentials,
                        SAVED_SEARCH_INTERVAL_SECONDS)
                    LOG.warning("Search key=%s provider=%s result=error code=%s", search_key,
                                provider_name, getattr(exc, "code", str(exc)))
                    time.sleep(3)
                    continue
                counts["searches"] += 1
                counts["listings"] += len(listings)
                added, review, complete = _record_listings(
                    user, original_profile, "Adzuna" if provider_name == "adzuna" else "SearXNG",
                    listings, allowed, provider_name)
                counts["enqueued"] += added
                counts["review"] += review
                LOG.info("Search key=%s provider=%s listings=%d new_queue_items=%d review_items=%d complete=%s",
                         search_key, provider_name, len(listings), added, review, complete)
                time.sleep(3)
    return counts


def _interval() -> int:
    try:
        value = int(os.environ.get("DISCOVERY_INTERVAL_SECONDS", "60"))
    except ValueError:
        raise ValueError("DISCOVERY_INTERVAL_SECONDS must be a whole number") from None
    if not 60 <= value <= 86400:
        raise ValueError("DISCOVERY_INTERVAL_SECONDS must be between 60 and 86400")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Queue jobs from saved searches and permitted feeds or job pages")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="scan configured sources once and exit")
    mode.add_argument("--watch", action="store_true", help="scan sources on the configured interval")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.once:
        discover_once()
        return 0
    try:
        interval = _interval()
    except ValueError:
        LOG.error("Discovery stopped reason=invalid_interval")
        return 2
    while True:
        started = time.monotonic()
        try:
            discover_once()
        except Exception:
            # Exception strings can contain feed URLs or source response data.
            LOG.error("Discovery cycle failed reason=unexpected_error")
        time.sleep(max(1, interval - int(time.monotonic() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
