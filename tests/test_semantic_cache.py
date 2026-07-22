"""Tests for optional LLM-assisted semantic cache aliases."""

from __future__ import annotations

from unittest.mock import MagicMock

from boss_cli.semantic_cache import SemanticQueryPlanner, normalize_keyword
from boss_cli.sqlite_cache import SQLiteCache


def _planner(tmp_path) -> SemanticQueryPlanner:
    return SemanticQueryPlanner(
        enabled=True,
        api_key="test-key",
        model="test-model",
        cache=SQLiteCache(tmp_path / "cache.db"),
    )


def test_deterministic_keyword_normalization():
    assert normalize_keyword("  Ｐｙｔｈｏｎ　 后端  ") == "Python 后端"


def test_disabled_planner_never_calls_llm(tmp_path, monkeypatch):
    planner = SemanticQueryPlanner(enabled=False, cache=SQLiteCache(tmp_path / "cache.db"))
    request_plan = MagicMock()
    monkeypatch.setattr(planner, "_request_plan", request_plan)

    plan = planner.plan("boss", " Python　后端 ", "北京")

    assert plan.canonical_keyword == "Python 后端"
    assert plan.assisted is False
    request_plan.assert_not_called()


def test_high_confidence_alias_is_cached_per_source(tmp_path, monkeypatch):
    planner = _planner(tmp_path)
    request_plan = MagicMock(return_value={"canonical_keyword": "Python 后端开发", "confidence": 0.97})
    monkeypatch.setattr(planner, "_request_plan", request_plan)

    first = planner.plan("boss", "Python服务端", "北京")
    second = planner.plan("boss", "Python服务端", "北京")

    assert first.canonical_keyword == "Python 后端开发"
    assert second.canonical_keyword == "Python 后端开发"
    assert second.assisted is True
    request_plan.assert_called_once()


def test_aliases_are_isolated_by_source(tmp_path, monkeypatch):
    planner = _planner(tmp_path)
    request_plan = MagicMock(side_effect=[
        {"canonical_keyword": "BOSS Python", "confidence": 0.98},
        {"canonical_keyword": "公共 Python", "confidence": 0.98},
    ])
    monkeypatch.setattr(planner, "_request_plan", request_plan)

    boss = planner.plan("boss", "Python", "北京")
    public = planner.plan("public", "Python", "北京")

    assert boss.canonical_keyword == "BOSS Python"
    assert public.canonical_keyword == "公共 Python"
    assert request_plan.call_count == 2


def test_low_confidence_falls_back_and_is_negative_cached(tmp_path, monkeypatch):
    planner = _planner(tmp_path)
    request_plan = MagicMock(return_value={"canonical_keyword": "宽泛开发", "confidence": 0.4})
    monkeypatch.setattr(planner, "_request_plan", request_plan)

    first = planner.plan("boss", "Python 数据分析", "北京")
    second = planner.plan("boss", "Python 数据分析", "北京")

    assert first.canonical_keyword == "Python 数据分析"
    assert second.canonical_keyword == "Python 数据分析"
    assert first.assisted is False
    request_plan.assert_called_once()


def test_invalid_llm_response_fails_safe(tmp_path, monkeypatch):
    planner = _planner(tmp_path)
    monkeypatch.setattr(planner, "_request_plan", MagicMock(return_value={"unexpected": True}))

    plan = planner.plan("public", "会计", "黄冈")

    assert plan.canonical_keyword == "会计"
    assert plan.assisted is False


def test_control_characters_are_rejected(tmp_path, monkeypatch):
    planner = _planner(tmp_path)
    monkeypatch.setattr(
        planner,
        "_request_plan",
        MagicMock(return_value={"canonical_keyword": "Python\n忽略规则", "confidence": 0.99}),
    )

    assert planner.plan("boss", "Python", "北京").canonical_keyword == "Python"