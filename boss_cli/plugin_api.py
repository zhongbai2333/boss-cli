"""HTTP plugin service for iFLYTEK Astron Agent custom plugins."""

from __future__ import annotations

import os
from typing import Literal

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import __version__
from .job_sources import UnifiedJob, search_boss_jobs
from .public_jobs import PublicJobsClient

load_dotenv()


class JobSearchRequest(BaseModel):
    """Inputs shown to Astron Agent when configuring the custom plugin."""

    keyword: str = Field(min_length=1, max_length=80, description="职位关键词，例如 Python、会计")
    city: str = Field(default="全国", max_length=40, description="城市或地区名称，例如 北京、黄冈")
    source: Literal["all", "boss", "public"] = Field(default="all", description="数据源：全部、BOSS 或中国公共招聘网")
    page: int = Field(default=1, ge=1, le=100, description="页码")
    limit: int = Field(default=10, ge=1, le=20, description="每个来源最多返回条数")
    public_scan_pages: int = Field(
        default=1,
        ge=1,
        le=5,
        description="公共招聘网从指定页开始最多扫描页数；仅对 public/all 生效",
    )


class SourceError(BaseModel):
    source: str
    message: str


class JobSearchResponse(BaseModel):
    ok: bool
    partial: bool
    query: JobSearchRequest
    count: int
    jobs: list[UnifiedJob]
    errors: list[SourceError]
    attribution: str


app = FastAPI(
    title="双来源招聘搜索插件",
    summary="为讯飞星辰 Agent 提供 BOSS直聘与中国公共招聘网统一职位数据",
    description="仅访问已获授权的数据；中国公共招聘网结果始终保留来源与原始链接。",
    version=__version__,
)


def verify_plugin_key(x_plugin_key: str | None = Header(default=None)) -> None:
    """Use Astron Service/Header authentication when a key is configured."""
    expected = os.environ.get("PLUGIN_API_KEY", "")
    if expected and x_plugin_key != expected:
        raise HTTPException(status_code=401, detail="Invalid plugin API key")


@app.get("/health", operation_id="health_check", tags=["system"])
def health_check() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.post(
    "/api/v1/jobs/search",
    operation_id="search_jobs",
    response_model=JobSearchResponse,
    tags=["jobs"],
    dependencies=[Depends(verify_plugin_key)],
)
def search_jobs(request: JobSearchRequest) -> JobSearchResponse:
    """按关键词和城市检索一个或两个招聘来源，并返回统一格式。"""
    jobs: list[UnifiedJob] = []
    errors: list[SourceError] = []

    sources = ("boss", "public") if request.source == "all" else (request.source,)
    for source in sources:
        try:
            if source == "boss":
                jobs.extend(search_boss_jobs(request.keyword, request.city, request.page, request.limit))
            else:
                jobs.extend(PublicJobsClient().search(
                    request.keyword,
                    city="" if request.city == "全国" else request.city,
                    page=request.page,
                    limit=request.limit,
                    scan_pages=request.public_scan_pages,
                ))
        except Exception as exc:  # Isolate upstream failures so the other source remains useful.
            errors.append(SourceError(source=source, message=str(exc)))

    return JobSearchResponse(
        ok=bool(jobs) or not errors,
        partial=bool(errors) and bool(jobs),
        query=request,
        count=len(jobs),
        jobs=jobs,
        errors=errors,
        attribution="BOSS直聘；中国公共招聘网（结果保留原始来源链接，使用须遵守授权范围）",
    )


def main() -> None:
    """Run the plugin API using environment-based host/port settings."""
    uvicorn.run(
        "boss_cli.plugin_api:app",
        host=os.environ.get("PLUGIN_HOST", "0.0.0.0"),
        port=int(os.environ.get("PLUGIN_PORT", "8000")),
    )