"""Authorized, rate-limited client for 中国公共招聘网 public job listings."""

from __future__ import annotations

import ast
import json
import os
import re
import threading
import time
from typing import Any
from urllib.parse import urljoin

import httpx
from lxml import html

from .job_sources import UnifiedJob

PUBLIC_JOBS_BASE_URL = "http://job.mohrss.gov.cn"
PUBLIC_JOBS_LIST_URL = f"{PUBLIC_JOBS_BASE_URL}/cjobs/jobinfolist/listJobinfolist"
PUBLIC_SOURCE_NAME = "中国公共招聘网"
_AUTH_VALUES = {"1", "true", "yes", "on"}


def authorization_confirmed() -> bool:
    """Require an explicit deployment-time confirmation of written authorization."""
    return os.environ.get("MOHRSS_AUTHORIZED", "").strip().lower() in _AUTH_VALUES


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _first_text(node: Any, xpath: str) -> str:
    values = node.xpath(xpath)
    if not values:
        return ""
    value = values[0]
    return _clean(value if isinstance(value, str) else value.text_content())


def _parse_metadata(raw: str) -> dict[str, Any]:
    """Parse the site's Python-literal-like metadata without executing code."""
    try:
        value = ast.literal_eval(raw.strip())
    except (SyntaxError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _job_from_record(record: dict[str, Any]) -> UnifiedJob:
    job_id = _clean(record.get("acb200", ""))
    location = " ".join(
        part for part in (_clean(record.get("aab302", "")), _clean(record.get("acb202", ""))) if part
    )
    return {
        "id": job_id,
        "source": "public",
        "source_name": PUBLIC_SOURCE_NAME,
        "title": _clean(record.get("aca112", "")),
        "company": _clean(record.get("aab004", "")),
        "salary": f"{_clean(record.get('acb241', ''))}元以上" if record.get("acb241") else "",
        "location": location,
        "experience": "",
        "education": "",
        "description": "",
        "published_at": _clean(record.get("aae397", "")),
        "url": f"{PUBLIC_JOBS_BASE_URL}/cjobs/jobinfolist/cb21/showgw?id={job_id}",
    }


def parse_public_jobs(
    html_text: str, *, keyword: str = "", city: str = "", limit: int = 20,
) -> list[UnifiedJob]:
    """Parse the hidden JSON or rendered cards while excluding contact details."""
    document = html.fromstring(html_text)
    jobs: list[UnifiedJob] = []
    city_filter = _clean(city)
    keyword_filter = _clean(keyword).casefold()

    hidden_values = document.xpath('//*[@id="findjoblist"]/@value')
    if hidden_values:
        try:
            records = json.loads(hidden_values[0])
        except (json.JSONDecodeError, TypeError):
            records = []
        if isinstance(records, list):
            for record in records:
                if not isinstance(record, dict):
                    continue
                job = _job_from_record(record)
                haystack = " ".join((job["title"], job["company"], job["description"])).casefold()
                if keyword_filter and keyword_filter not in haystack:
                    continue
                if city_filter and city_filter not in job["location"]:
                    continue
                jobs.append(job)
                if len(jobs) >= limit:
                    return jobs

    # The live page contains legacy, occasionally unbalanced markup. Browsers
    # repair it under #findjobTable, while lxml may move cards outside that
    # container. Anchor on a card that owns a job-detail link instead.
    cards = document.xpath(
        '//div[contains(concat(" ", normalize-space(@class), " "), " list_con ")]'
        '[.//a[contains(@href, "showgw?id=")]]'
    )
    for card in cards:
        href = _first_text(card, './/a[contains(@href, "showgw?id=")]/@href')
        if not href:
            continue
        location = _first_text(card, './/*[contains(concat(" ", normalize-space(@class), " "), " josbs_usetime ")]')
        if city_filter and city_filter not in location:
            continue

        metadata_raw = _first_text(card, './div[contains(concat(" ", normalize-space(@class), " "), " hidden ")]/text()')
        metadata = _parse_metadata(metadata_raw)
        match = re.search(r"[?&]id=(\d+)", href)
        job_id = match.group(1) if match else str(metadata.get("acb200", ""))
        description = _first_text(card, './/ul[contains(@class, "showorhidden")]//li[contains(., "岗位描述")]/span')
        job: UnifiedJob = {
            "id": job_id,
            "source": "public",
            "source_name": PUBLIC_SOURCE_NAME,
            "title": _first_text(card, './/a[contains(@href, "showgw?id=")]'),
            "company": _first_text(card, './/a[contains(@href, "showdw?id=")]'),
            "salary": _first_text(card, './/*[contains(concat(" ", normalize-space(@class), " "), " jobs_pay ")]'),
            "location": location,
            "experience": "",
            "education": "",
            "description": description,
            "published_at": _clean(metadata.get("aae397", "")),
            "url": urljoin(PUBLIC_JOBS_LIST_URL, href),
        }
        haystack = " ".join((job["title"], job["company"], job["description"])).casefold()
        if keyword_filter and keyword_filter not in haystack:
            continue
        jobs.append(job)
        if len(jobs) >= limit:
            break
    return jobs


class PublicJobsClient:
    """Small authorized scraper with process-wide conservative throttling."""

    _request_lock = threading.Lock()
    _last_request_at = 0.0

    def __init__(self, *, timeout: float = 20.0, minimum_interval: float = 2.0) -> None:
        self.timeout = timeout
        self.minimum_interval = max(minimum_interval, 1.0)

    def _wait_for_slot(self) -> None:
        with self._request_lock:
            elapsed = time.monotonic() - type(self)._last_request_at
            if elapsed < self.minimum_interval:
                time.sleep(self.minimum_interval - elapsed)
            type(self)._last_request_at = time.monotonic()

    def search(
        self, keyword: str, *, city: str = "", page: int = 1, limit: int = 20, scan_pages: int = 1,
    ) -> list[UnifiedJob]:
        if not authorization_confirmed():
            raise PermissionError(
                "中国公共招聘网采集未启用：仅在已取得书面授权时设置 MOHRSS_AUTHORIZED=true"
            )

        headers = {
            "User-Agent": "boss-cli-astron-plugin/0.1 (authorized educational integration)",
            "Accept": "text/html,application/xhtml+xml",
        }
        # The legacy site's `textfield` query currently returns an empty data
        # field for valid terms. Fetch only the explicitly bounded page range
        # and filter each page's (maximum 20) records locally.
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=headers) as client:
            jobs: list[UnifiedJob] = []
            for current_page in range(page, page + min(max(scan_pages, 1), 5)):
                self._wait_for_slot()
                response = client.get(
                    PUBLIC_JOBS_LIST_URL,
                    params={"pageNo": current_page, "orderType": "score"},
                )
                response.raise_for_status()
                jobs.extend(parse_public_jobs(
                    response.text,
                    keyword=keyword,
                    city=city,
                    limit=min(limit - len(jobs), 20),
                ))
                if len(jobs) >= limit:
                    break
        return jobs[:limit]