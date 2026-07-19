"""Unified job-source models and adapters used by the Astron plugin API."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from .auth import get_credential
from .client import BossClient, resolve_city

SourceName = Literal["boss", "public"]


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


def search_boss_jobs(keyword: str, city: str, page: int, limit: int) -> list[UnifiedJob]:
    """Search BOSS with an existing authorized user session."""
    credential = get_credential()
    if credential is None:
        raise RuntimeError("BOSS 数据源未配置凭据，请设置 BOSS_COOKIES 或先执行 boss login")

    with BossClient(credential) as client:
        data = client.search_jobs(
            query=keyword,
            city=resolve_city(city or "全国"),
            page=page,
            page_size=min(limit, 15),
        )
    return [normalize_boss_job(job) for job in data.get("jobList", [])[:limit]]