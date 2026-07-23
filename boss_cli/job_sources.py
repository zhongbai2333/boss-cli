"""Unified job-source models and adapters used by the Astron plugin API."""

from __future__ import annotations

import os
from typing import Any, Literal

from typing_extensions import TypedDict

from .auth import get_credential, refresh_credential
from .client import BossClient, resolve_city
from .exceptions import SessionExpiredError
from .sqlite_cache import SQLiteCache, make_cache_key

SourceName = Literal["boss", "public"]
_AUTH_FAILURE_NAMESPACE = "source:boss:auth-failure"


def _safe_float_env(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _credential_fingerprint(credential: object) -> str:
    """Return an opaque fingerprint that changes when any session cookie changes."""
    cookies = getattr(credential, "cookies", {})
    return make_cache_key("boss-plugin-credential", sorted(cookies.items()))


def _search_with_credential(
    credential: object,
    keyword: str,
    city: str,
    page: int,
    limit: int,
    cache_keyword: str | None,
    *,
    cache: SQLiteCache,
    cache_ttl_s: float,
    min_interval_s: float,
) -> dict[str, Any]:
    with BossClient(
        credential,
        cache=cache,
        cache_ttl_s=cache_ttl_s,
        min_request_interval_s=min_interval_s,
    ) as client:
        return client.search_jobs(
            query=keyword,
            city=resolve_city(city or "全国"),
            page=page,
            page_size=min(limit, 15),
            cache_query=cache_keyword,
        )


class UnifiedJob(TypedDict):
    """Stable, source-neutral job representation exposed to agents."""

    id: str
    source: SourceName
    source_name: str
    title: str
    company: str
    salary: str
    location: str
    experience: str
    education: str
    description: str
    published_at: str
    url: str


def normalize_boss_job(job: dict[str, Any]) -> UnifiedJob:
    """Convert a BOSS search card into the public plugin schema."""
    security_id = str(job.get("securityId", ""))
    location = " ".join(
        str(value).strip()
        for value in (job.get("cityName"), job.get("areaDistrict"), job.get("businessDistrict"))
        if value
    )
    return {
        "id": security_id,
        "source": "boss",
        "source_name": "BOSS直聘",
        "title": str(job.get("jobName", "")),
        "company": str(job.get("brandName", "")),
        "salary": str(job.get("salaryDesc", "")),
        "location": location,
        "experience": str(job.get("jobExperience", "")),
        "education": str(job.get("jobDegree", "")),
        "description": str(job.get("jobDesc", "")),
        "published_at": str(job.get("lastModifyTime", "")),
        "url": f"https://www.zhipin.com/job_detail/{security_id}.html" if security_id else "https://www.zhipin.com/",
    }


def search_boss_jobs(
    keyword: str,
    city: str,
    page: int,
    limit: int,
    *,
    cache_keyword: str | None = None,
) -> list[UnifiedJob]:
    """Search BOSS with an existing authorized user session."""
    credential = get_credential()
    if credential is None:
        raise RuntimeError("BOSS 数据源未配置凭据，请设置 BOSS_COOKIES 或先执行 boss login")

    cache = SQLiteCache()
    cache_ttl_s = _safe_float_env("BOSS_PLUGIN_CACHE_TTL_S", 1800.0, 60.0)
    min_interval_s = _safe_float_env("BOSS_PLUGIN_MIN_INTERVAL_S", 10.0, 5.0)
    auth_cooldown_s = _safe_float_env("BOSS_PLUGIN_AUTH_FAILURE_COOLDOWN_S", 3600.0, 300.0)
    fingerprint = _credential_fingerprint(credential)
    if cache.get(_AUTH_FAILURE_NAMESPACE, fingerprint) is not None:
        raise SessionExpiredError()

    try:
        data = _search_with_credential(
            credential,
            keyword,
            city,
            page,
            limit,
            cache_keyword,
            cache=cache,
            cache_ttl_s=cache_ttl_s,
            min_interval_s=min_interval_s,
        )
    except SessionExpiredError:
        # Block concurrent/follow-up plugin calls before attempting one safe,
        # idempotent replay. A newly imported/refreshed cookie set has a new
        # fingerprint and is therefore not poisoned by this cooldown entry.
        cache.set(_AUTH_FAILURE_NAMESPACE, fingerprint, True, ttl_s=auth_cooldown_s)
        fresh, _ = refresh_credential(current_credential=credential)
        if fresh is None:
            raise

        fresh_fingerprint = _credential_fingerprint(fresh)
        if fresh_fingerprint == fingerprint:
            raise
        if cache.get(_AUTH_FAILURE_NAMESPACE, fresh_fingerprint) is not None:
            raise

        try:
            data = _search_with_credential(
                fresh,
                keyword,
                city,
                page,
                limit,
                cache_keyword,
                cache=cache,
                cache_ttl_s=cache_ttl_s,
                min_interval_s=min_interval_s,
            )
        except SessionExpiredError:
            cache.set(_AUTH_FAILURE_NAMESPACE, fresh_fingerprint, True, ttl_s=auth_cooldown_s)
            raise
        else:
            cache.delete(_AUTH_FAILURE_NAMESPACE, fresh_fingerprint)
    return [normalize_boss_job(job) for job in data.get("jobList", [])[:limit]]