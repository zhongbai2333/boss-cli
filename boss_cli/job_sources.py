"""Unified job-source models and adapters used by the Astron plugin API."""

from __future__ import annotations

import os
from typing import Any, Literal

from typing_extensions import TypedDict

from .auth import get_credential
from .client import BossClient, resolve_city

SourceName = Literal["boss", "public"]


def _safe_float_env(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


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

    cache_ttl_s = _safe_float_env("BOSS_PLUGIN_CACHE_TTL_S", 1800.0, 60.0)
    min_interval_s = _safe_float_env("BOSS_PLUGIN_MIN_INTERVAL_S", 10.0, 5.0)
    with BossClient(
        credential,
        cache_ttl_s=cache_ttl_s,
        min_request_interval_s=min_interval_s,
    ) as client:
        data = client.search_jobs(
            query=keyword,
            city=resolve_city(city or "全国"),
            page=page,
            page_size=min(limit, 15),
            cache_query=cache_keyword,
        )
    return [normalize_boss_job(job) for job in data.get("jobList", [])[:limit]]