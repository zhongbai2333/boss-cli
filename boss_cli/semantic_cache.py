"""Optional LLM-assisted, source-aware semantic query normalization."""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from .sqlite_cache import SQLiteCache, make_cache_key

logger = logging.getLogger(__name__)

SourceName = Literal["boss", "public"]
_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SemanticQueryPlan:
    """A locally validated query plan for one source."""

    source: SourceName
    original_keyword: str
    canonical_keyword: str
    confidence: float
    assisted: bool


def normalize_keyword(keyword: str) -> str:
    """Apply conservative deterministic normalization before any LLM call."""
    normalized = unicodedata.normalize("NFKC", keyword)
    return " ".join(normalized.split()).strip()


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        return min(maximum, max(minimum, float(os.environ.get(name, str(default)))))
    except ValueError:
        return default


class SemanticQueryPlanner:
    """Use an LLM only to create safe aliases for source-local exact caches."""

    def __init__(
        self,
        *,
        enabled: bool,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        model: str = "",
        confidence_threshold: float = 0.92,
        alias_ttl_s: float = 604800.0,
        timeout_s: float = 5.0,
        cache: SQLiteCache | None = None,
    ) -> None:
        self.enabled = enabled and bool(api_key and model)
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.confidence_threshold = min(0.99, max(0.5, confidence_threshold))
        self.alias_ttl_s = max(300.0, alias_ttl_s)
        self.timeout_s = min(15.0, max(1.0, timeout_s))
        self.cache = cache or SQLiteCache()

    @classmethod
    def from_env(cls, *, cache: SQLiteCache | None = None) -> SemanticQueryPlanner:
        """Build a planner from deployment configuration; disabled by default."""
        enabled = os.environ.get("LLM_SEMANTIC_CACHE_ENABLED", "").strip().lower() in _TRUE_VALUES
        return cls(
            enabled=enabled,
            api_key=os.environ.get("LLM_API_KEY", "").strip(),
            base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").strip(),
            model=os.environ.get("LLM_MODEL", "").strip(),
            confidence_threshold=_float_env("LLM_SEMANTIC_CONFIDENCE", 0.92, 0.5, 0.99),
            alias_ttl_s=_float_env("LLM_SEMANTIC_ALIAS_TTL_S", 604800.0, 300.0, 2592000.0),
            timeout_s=_float_env("LLM_TIMEOUT_S", 5.0, 1.0, 15.0),
            cache=cache,
        )

    def plan(self, source: SourceName, keyword: str, city: str) -> SemanticQueryPlan:
        """Return a source-local canonical keyword, failing safely to deterministic input."""
        original = normalize_keyword(keyword)
        fallback = SemanticQueryPlan(source, original, original, 0.0, False)
        if not original or not self.enabled:
            return fallback

        alias_key = make_cache_key(source, original.casefold(), normalize_keyword(city).casefold(), self.model)
        namespace = f"source:{source}:semantic-alias"
        cached = self.cache.get(namespace, alias_key)
        if self._is_negative_cache(original, cached):
            return fallback
        cached_plan = self._validated_plan(source, original, cached, assisted=True)
        if cached_plan is not None:
            return cached_plan

        try:
            response_data = self._request_plan(source, original, city)
            plan = self._validated_plan(source, original, response_data, assisted=True)
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            logger.warning("LLM semantic planning failed for source %s; using exact query", source, exc_info=True)
            plan = None

        if plan is None:
            # Cache the safe fallback briefly so a broken model cannot be called
            # repeatedly by an Agent loop. It never aliases to another query.
            self.cache.set(
                namespace,
                alias_key,
                {"canonical_keyword": original, "confidence": 0.0},
                ttl_s=min(self.alias_ttl_s, 600.0),
            )
            return fallback

        self.cache.set(
            namespace,
            alias_key,
            {"canonical_keyword": plan.canonical_keyword, "confidence": plan.confidence},
            ttl_s=self.alias_ttl_s,
        )
        return plan

    def _request_plan(self, source: SourceName, keyword: str, city: str) -> dict[str, Any]:
        source_description = "BOSS直聘" if source == "boss" else "中国公共招聘网"
        system_prompt = (
            "你是招聘搜索缓存查询规划器。只规范化职位关键词，不执行搜索。"
            "不得扩大或缩小用户意图，不得修改城市、平台、页码或筛选条件。"
            "只有同义表达、空格、大小写、常见中英文职位名称可统一。"
            "若无法确定等价，canonical_keyword 必须原样返回且 confidence 低于 0.92。"
            "只返回 JSON 对象，字段为 canonical_keyword 和 confidence。"
        )
        user_payload = json.dumps(
            {"source": source, "source_name": source_description, "keyword": keyword, "city": city},
            ensure_ascii=False,
        )
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ],
        }
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("LLM response content is not text")
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise TypeError("LLM response is not a JSON object")
        return parsed

    def _validated_plan(
        self,
        source: SourceName,
        original: str,
        value: Any,
        *,
        assisted: bool,
    ) -> SemanticQueryPlan | None:
        if not isinstance(value, dict):
            return None
        canonical = value.get("canonical_keyword")
        confidence = value.get("confidence")
        if not isinstance(canonical, str) or not isinstance(confidence, (int, float)):
            return None
        if re.search(r"[\r\n\x00]", canonical):
            return None
        canonical = normalize_keyword(canonical)
        confidence = float(confidence)
        if (
            not canonical
            or len(canonical) > 80
            or confidence < self.confidence_threshold
            or confidence > 1.0
        ):
            return None
        return SemanticQueryPlan(source, original, canonical, confidence, assisted)

    @staticmethod
    def _is_negative_cache(original: str, value: Any) -> bool:
        """Recognize a locally written fallback without trusting it as an alias."""
        if not isinstance(value, dict):
            return False
        canonical = value.get("canonical_keyword")
        confidence = value.get("confidence")
        return (
            isinstance(canonical, str)
            and normalize_keyword(canonical) == original
            and isinstance(confidence, (int, float))
            and float(confidence) == 0.0
        )