"""SQLite-backed JSON cache with TTL enforcement."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from .constants import CACHE_DB_FILE

logger = logging.getLogger(__name__)


class SQLiteCache:
    """Store JSON-compatible values in a process-safe SQLite cache."""

    def __init__(self, path: Path = CACHE_DB_FILE):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_entries (
                namespace TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (namespace, cache_key)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_entries_expires_at ON cache_entries(expires_at)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_limit_slots (
                scope TEXT PRIMARY KEY,
                next_allowed_at REAL NOT NULL
            )
            """
        )
        connection.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            logger.debug("Unable to update cache database permissions", exc_info=True)
        return connection

    def get(self, namespace: str, cache_key: str, *, now: float | None = None) -> Any | None:
        """Return a fresh cached value, deleting expired or invalid entries."""
        checked_at = time.time() if now is None else now
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT value_json, expires_at FROM cache_entries WHERE namespace = ? AND cache_key = ?",
                    (namespace, cache_key),
                ).fetchone()
                if row is None:
                    return None
                if float(row[1]) <= checked_at:
                    connection.execute(
                        "DELETE FROM cache_entries WHERE namespace = ? AND cache_key = ?",
                        (namespace, cache_key),
                    )
                    return None
                try:
                    return json.loads(row[0])
                except (TypeError, json.JSONDecodeError):
                    connection.execute(
                        "DELETE FROM cache_entries WHERE namespace = ? AND cache_key = ?",
                        (namespace, cache_key),
                    )
                    logger.warning("Removed invalid cache entry from namespace %s", namespace)
                    return None
        except sqlite3.Error:
            logger.warning("Unable to read SQLite cache", exc_info=True)
            return None

    def set(
        self,
        namespace: str,
        cache_key: str,
        value: Any,
        *,
        ttl_s: float,
        now: float | None = None,
    ) -> None:
        """Insert or replace a value with a positive TTL."""
        if ttl_s <= 0:
            return
        created_at = time.time() if now is None else now
        value_json = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO cache_entries(namespace, cache_key, value_json, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(namespace, cache_key) DO UPDATE SET
                        value_json = excluded.value_json,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at
                    """,
                    (namespace, cache_key, value_json, created_at, created_at + ttl_s),
                )
        except sqlite3.Error:
            logger.warning("Unable to write SQLite cache", exc_info=True)

    def delete(self, namespace: str, cache_key: str) -> None:
        """Delete one cached entry."""
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM cache_entries WHERE namespace = ? AND cache_key = ?",
                    (namespace, cache_key),
                )
        except sqlite3.Error:
            logger.warning("Unable to delete SQLite cache entry", exc_info=True)

    def purge_expired(self, *, now: float | None = None) -> int:
        """Delete all expired entries and return the number removed."""
        checked_at = time.time() if now is None else now
        try:
            with self._connect() as connection:
                cursor = connection.execute("DELETE FROM cache_entries WHERE expires_at <= ?", (checked_at,))
                return max(cursor.rowcount, 0)
        except sqlite3.Error:
            logger.warning("Unable to purge expired SQLite cache entries", exc_info=True)
            return 0

    def reserve_request_slot(
        self,
        scope: str,
        *,
        min_interval_s: float,
        now: float | None = None,
    ) -> float:
        """Atomically reserve a cross-process request slot and return wait seconds."""
        if min_interval_s <= 0:
            return 0.0
        requested_at = time.time() if now is None else now
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT next_allowed_at FROM rate_limit_slots WHERE scope = ?",
                    (scope,),
                ).fetchone()
                slot_at = max(requested_at, float(row[0])) if row else requested_at
                connection.execute(
                    """
                    INSERT INTO rate_limit_slots(scope, next_allowed_at) VALUES (?, ?)
                    ON CONFLICT(scope) DO UPDATE SET next_allowed_at = excluded.next_allowed_at
                    """,
                    (scope, slot_at + min_interval_s),
                )
                connection.commit()
                return max(0.0, slot_at - requested_at)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except sqlite3.Error:
            logger.warning("Unable to reserve SQLite rate-limit slot", exc_info=True)
            return min_interval_s


def make_cache_key(*parts: Any) -> str:
    """Build a stable, opaque key from JSON-compatible request identity parts."""
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def credential_cache_scope(credential: object | None) -> str:
    """Return a non-reversible account scope without persisting raw cookies."""
    cookies = getattr(credential, "cookies", {}) if credential is not None else {}
    if not isinstance(cookies, dict) or not cookies:
        return "anonymous"
    stable = {name: cookies[name] for name in ("wt2", "zp_at") if cookies.get(name)}
    if not stable:
        stable = cookies
    canonical = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()