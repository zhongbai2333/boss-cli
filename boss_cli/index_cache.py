"""Search result index cache for short-index navigation.

Stores the last search/recommend result set so that users can quickly
access a job by its 1-based index number (e.g., `boss show 3`).

Cache database: ~/.config/boss-cli/cache.db
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .constants import CACHE_DB_FILE, CONFIG_DIR, INDEX_CACHE_TTL_S
from .sqlite_cache import SQLiteCache

logger = logging.getLogger(__name__)

INDEX_CACHE_FILE = CONFIG_DIR / "index_cache.json"
INDEX_CACHE_DB_FILE = CACHE_DB_FILE
INDEX_NAMESPACE = "job-index"
INDEX_KEY = "current"


def _cache() -> SQLiteCache:
    return SQLiteCache(INDEX_CACHE_DB_FILE)


def _migrate_legacy_cache() -> None:
    """Import the previous JSON index once, preserving its original age."""
    if not INDEX_CACHE_FILE.exists():
        return
    try:
        data = json.loads(INDEX_CACHE_FILE.read_text(encoding="utf-8"))
        saved_at = float(data.get("saved_at", 0))
        remaining_ttl = saved_at + INDEX_CACHE_TTL_S - time.time()
        if isinstance(data.get("items"), list) and remaining_ttl > 0:
            _cache().set(INDEX_NAMESPACE, INDEX_KEY, data, ttl_s=remaining_ttl)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Unable to migrate legacy index cache", exc_info=True)
    finally:
        try:
            INDEX_CACHE_FILE.unlink(missing_ok=True)
        except OSError:
            logger.warning("Unable to remove legacy index cache", exc_info=True)


def save_index(jobs: list[dict[str, Any]], source: str = "search") -> None:
    """Persist an ordered list of jobs for short-index navigation.

    Each entry stores the minimum metadata needed for `show` and `detail`:
    securityId, jobName, brandName, salaryDesc, lid.
    """
    if not jobs:
        return

    entries = []
    for job in jobs:
        entry = {
            "securityId": job.get("securityId", ""),
            "jobName": job.get("jobName", ""),
            "brandName": job.get("brandName", ""),
            "salaryDesc": job.get("salaryDesc", ""),
            "cityName": job.get("cityName", ""),
            "areaDistrict": job.get("areaDistrict", ""),
            "jobExperience": job.get("jobExperience", ""),
            "jobDegree": job.get("jobDegree", ""),
            "skills": job.get("skills", []),
            "lid": job.get("lid", ""),
        }
        if entry["securityId"]:
            entries.append(entry)

    payload: dict[str, Any] = {
        "source": source,
        "saved_at": time.time(),
        "count": len(entries),
        "items": entries,
    }

    _cache().set(INDEX_NAMESPACE, INDEX_KEY, payload, ttl_s=INDEX_CACHE_TTL_S)
    logger.debug("Saved index cache with %d entries from %s", len(entries), source)


def get_job_by_index(index: int) -> dict[str, Any] | None:
    """Resolve a 1-based short index to a cached job reference.

    Returns the job entry dict or None if index is out of range.
    """
    if index <= 0:
        return None

    _migrate_legacy_cache()
    data = _cache().get(INDEX_NAMESPACE, INDEX_KEY)
    if not isinstance(data, dict):
        return None

    items = data.get("items", [])
    if not isinstance(items, list) or index > len(items):
        return None

    item = items[index - 1]
    return item if isinstance(item, dict) else None


def get_index_info() -> dict[str, Any]:
    """Get metadata about the current index cache."""
    _migrate_legacy_cache()
    data = _cache().get(INDEX_NAMESPACE, INDEX_KEY)
    if not isinstance(data, dict):
        return {"exists": False, "count": 0}
    return {
        "exists": True,
        "source": data.get("source", "unknown"),
        "count": data.get("count", 0),
        "saved_at": data.get("saved_at", 0),
    }
