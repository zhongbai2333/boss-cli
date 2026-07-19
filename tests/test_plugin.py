"""Tests for the Astron HTTP plugin and authorized public-job parser."""

from __future__ import annotations

import html as html_stdlib
import json

from fastapi.testclient import TestClient

from boss_cli.plugin_api import app
from boss_cli.public_jobs import PublicJobsClient, parse_public_jobs

PUBLIC_HTML = """
<html><body><div id="findjobTable"><ul><li>
  <div class="list_con">
    <ul class="list_show">
      <li class="list_con_tit">
        <a href="../jobinfolist/cb21/showgw?id=80758208">采购专员</a>
        <a href="../jobinfolist/cb21/showdw?id=36029286">双杰电气湖北有限公司</a>
      </li>
      <li class="jobs_pay"><span>5000元以上</span></li>
      <li class="josbs_usetime">黄冈市</li>
    </ul>
    <ul class="showorhidden hidden">
      <li>单位地址：秘密地址 | 联系人：张女士 | 联系电话：13800000000</li>
      <li>岗位描述：<span>负责供应商管理和采购执行。</span></li>
    </ul>
    <div class="hidden">{'acb200':'80758208','aae397':'2026-07-18'}</div>
  </div>
</li></ul></div></body></html>
"""


def test_parse_public_jobs_normalizes_and_excludes_contacts():
    jobs = parse_public_jobs(PUBLIC_HTML, city="黄冈", limit=10)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == "80758208"
    assert job["source"] == "public"
    assert job["source_name"] == "中国公共招聘网"
    assert job["title"] == "采购专员"
    assert job["company"] == "双杰电气湖北有限公司"
    assert job["salary"] == "5000元以上"
    assert job["description"] == "负责供应商管理和采购执行。"
    assert job["published_at"] == "2026-07-18"
    assert "showgw?id=80758208" in job["url"]
    assert "13800000000" not in str(job)
    assert "秘密地址" not in str(job)


def test_parse_public_jobs_city_filter():
    assert parse_public_jobs(PUBLIC_HTML, city="北京", limit=10) == []


def test_parse_public_jobs_tolerates_card_outside_legacy_container():
  malformed_repair = PUBLIC_HTML.replace('<div id="findjobTable"><ul><li>', '<div id="findjobTable"></div><ul><li>')
  jobs = parse_public_jobs(malformed_repair, limit=10)
  assert len(jobs) == 1
  assert jobs[0]["id"] == "80758208"


def test_parse_public_jobs_hidden_json_and_local_keyword_filter():
  records = [{
    "acb200": 79835319,
    "aca112": "渠道销售",
    "aab004": "新疆乳业有限公司",
    "acb241": 6000,
    "aab302": "新疆生产建设兵团",
    "acb202": "五家渠市及周边",
    "aae397": "2026-07-18",
  }]
  payload = html_stdlib.escape(json.dumps(records, ensure_ascii=False), quote=True)
  source = f'<html><input id="findjoblist" value="{payload}"></html>'
  jobs = parse_public_jobs(source, keyword="渠道", city="新疆", limit=5)
  assert len(jobs) == 1
  assert jobs[0]["salary"] == "6000元以上"
  assert jobs[0]["location"] == "新疆生产建设兵团 五家渠市及周边"
  assert jobs[0]["description"] == ""
  assert parse_public_jobs(source, keyword="Python", limit=5) == []


def test_public_client_scans_bounded_pages_until_limit(monkeypatch):
    requested_pages = []

    class Response:
      def __init__(self, page):
        record = {
          "acb200": page,
          "aca112": f"Python 工程师 {page}",
          "aab004": "测试公司",
        }
        payload = html_stdlib.escape(json.dumps([record], ensure_ascii=False), quote=True)
        self.text = f'<input id="findjoblist" value="{payload}">'

      def raise_for_status(self):
        return None

    class Client:
      def __init__(self, **kwargs):
        pass

      def __enter__(self):
        return self

      def __exit__(self, *args):
        return None

      def get(self, url, *, params):
        requested_pages.append(params["pageNo"])
        return Response(params["pageNo"])

    monkeypatch.setenv("MOHRSS_AUTHORIZED", "true")
    monkeypatch.setattr("boss_cli.public_jobs.httpx.Client", Client)
    monkeypatch.setattr(PublicJobsClient, "_wait_for_slot", lambda self: None)

    jobs = PublicJobsClient().search("Python", page=3, limit=2, scan_pages=99)

    assert requested_pages == [3, 4]
    assert [job["id"] for job in jobs] == ["3", "4"]


def test_plugin_requires_configured_api_key(monkeypatch):
    monkeypatch.setenv("PLUGIN_API_KEY", "expected")
    client = TestClient(app)
    response = client.post("/api/v1/jobs/search", json={"keyword": "Python", "source": "boss"})
    assert response.status_code == 401


def test_plugin_returns_public_results(monkeypatch):
    monkeypatch.delenv("PLUGIN_API_KEY", raising=False)
    expected = parse_public_jobs(PUBLIC_HTML, keyword="采购")
    monkeypatch.setattr("boss_cli.plugin_api.PublicJobsClient.search", lambda self, *args, **kwargs: expected)

    response = TestClient(app).post(
        "/api/v1/jobs/search",
        json={"keyword": "采购", "city": "黄冈", "source": "public", "limit": 5},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["partial"] is False
    assert data["count"] == 1
    assert data["jobs"][0]["source"] == "public"


def test_plugin_keeps_successful_source_when_other_fails(monkeypatch):
    monkeypatch.delenv("PLUGIN_API_KEY", raising=False)
    expected = parse_public_jobs(PUBLIC_HTML, keyword="采购")
    monkeypatch.setattr("boss_cli.plugin_api.search_boss_jobs", lambda *args, **kwargs: expected)

    def fail_public(*args, **kwargs):
        raise PermissionError("authorization missing")

    monkeypatch.setattr("boss_cli.plugin_api.PublicJobsClient.search", fail_public)
    response = TestClient(app).post(
        "/api/v1/jobs/search",
        headers={"X-Plugin-Key": "ignored"},
        json={"keyword": "采购", "source": "all"},
    )
    data = response.json()
    assert response.status_code == 200
    assert data["ok"] is True
    assert data["partial"] is True
    assert data["count"] == 1
    assert data["errors"][0]["source"] == "public"


def test_openapi_exposes_single_astron_action():
    schema = TestClient(app).get("/openapi.json").json()
    action = schema["paths"]["/api/v1/jobs/search"]["post"]
    assert action["operationId"] == "search_jobs"
    assert "/health" in schema["paths"]
    request_schema = schema["components"]["schemas"]["JobSearchRequest"]
    assert request_schema["properties"]["public_scan_pages"]["maximum"] == 5