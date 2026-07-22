"""Tests for the process-safe SQLite cache and safety throttle."""

from __future__ import annotations

from boss_cli.sqlite_cache import SQLiteCache


def test_cache_returns_fresh_value_and_deletes_expired(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.db")
    cache.set("test", "key", {"value": 1}, ttl_s=10, now=100)

    assert cache.get("test", "key", now=109) == {"value": 1}
    assert cache.get("test", "key", now=110) is None
    assert cache.get("test", "key", now=109) is None


def test_purge_expired_keeps_fresh_entries(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.db")
    cache.set("test", "expired", 1, ttl_s=5, now=100)
    cache.set("test", "fresh", 2, ttl_s=20, now=100)

    assert cache.purge_expired(now=110) == 1
    assert cache.get("test", "expired", now=110) is None
    assert cache.get("test", "fresh", now=110) == 2


def test_rate_limit_reservations_are_shared_across_instances(tmp_path):
    path = tmp_path / "cache.db"
    first = SQLiteCache(path)
    second = SQLiteCache(path)

    assert first.reserve_request_slot("account", min_interval_s=10, now=100) == 0
    assert second.reserve_request_slot("account", min_interval_s=10, now=100) == 10
    assert first.reserve_request_slot("account", min_interval_s=10, now=105) == 15


def test_rate_limit_scopes_are_independent(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.db")

    assert cache.reserve_request_slot("account-a", min_interval_s=10, now=100) == 0
    assert cache.reserve_request_slot("account-b", min_interval_s=10, now=100) == 0