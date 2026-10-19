"""Thin, typed LLM client over the Gemini REST API (primary) and the Anthropic Messages API (optional).

Why REST instead of SDKs: the request shapes are small, identical across our Python and TypeScript
services, and we control retries, timeouts and cost accounting in one place.

Callers ask for a *tier* ("fast", "smart", "vision") instead of a model name, so models are swapped via
environment variables only. Structured output is requested with a JSON schema and always re-validated
with Pydantic; one corrective retry is made if the model returns invalid JSON.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings

log = logging.getLogger("scope.llm")

Tier = Literal["fast", "smart", "vision"]
T = TypeVar("T", bound=BaseModel)

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


class LLMError(Exception):
    """kind: auth | config | rate_limit | server | network | bad_output | disabled"""

    def __init__(self, kind: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass
class LLMCall:
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float | None = None
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_base: float = 1.0,
    ) -> None:
        self.s = settings
        self._transport = transport
        self._backoff_base = backoff_base

    # ------------------------------------------------------------------ provider selection
    @property
    def provider(self) -> str:
        p = (self.s.llm_provider or "auto").lower()
        if p == "auto":
            if self.s.gemini_api_key:
                return "gemini"
            if self.s.anthropic_api_key:
                return "anthropic"
            return "offline"
        if p == "gemini" and not self.s.gemini_api_key:
            return "offline"
        if p == "anthropic" and not self.s.anthropic_api_key:
            return "offline"
        return p if p in ("gemini", "anthropic") else "offline"

    @property
    def enabled(self) -> bool:
        return self.provider != "offline"

    @property
    def embeddings_enabled(self) -> bool:
        return bool(self.s.gemini_api_key)

    @property
    def vision_enabled(self) -> bool:
        return bool(self.s.gemini_api_key) or self.provider == "anthropic"

    def model_for(self, tier: Tier) -> str:
        if self.provider == "anthropic":
            return self.s.anthropic_model_fast if tier == "fast" else self.s.anthropic_model_smart
        return {
            "fast": self.s.gemini_model_fast,
            "smart": self.s.gemini_model_smart,
            "vision": self.s.gemini_model_vision,
        }[tier]

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "models": {t: self.model_for(t) for t in ("fast", "smart", "vision")} if self.enabled else {},
            "embeddings": self.s.gemini_embed_model if self.embeddings_enabled else None,
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.s.llm_timeout_seconds, transport=self._transport)

    def _cost(self, model: str, inp: int, out: int) -> float | None:
        price = self.s.model_prices.get(model)
        if not price:
            return None
        return round(inp / 1e6 * price[0] + out / 1e6 * price[1], 6)

    # ------------------------------------------------------------------ public API
    async def generate_json(
        self,
        *,
        tier: Tier,
        system: str,
        prompt: str,
        schema: type[T],
        images: list[tuple[bytes, str]] | None = None,
        temperature: float = 0.2,
    ) -> tuple[T, LLMCall]:
        if not self.enabled:
            raise LLMError("disabled", "No LLM provider configured (offline mode).")
        json_schema = inline_refs(schema.model_json_schema())
        model = self.model_for(tier)
        started = time.perf_counter()
        attempt_prompt = prompt
        last_err = ""
        total_in = total_out = attempts = 0
        for _ in range(2):  # one corrective retry on invalid JSON
            if self.provider == "anthropic":
                raw, tin, tout, n = await self._anthropic(model, system, attempt_prompt, json_schema, images, temperature)
            else:
                raw, tin, tout, n = await self._gemini(model, system, attempt_prompt, json_schema, images, temperature)
            total_in += tin
            total_out += tout
            attempts += n
            try:
                parsed = schema.model_validate(raw if isinstance(raw, dict) else _parse_json(raw))
                call = LLMCall(
                    provider=self.provider,
                    model=model,
                    input_tokens=total_in,
                    output_tokens=total_out,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    cost_usd=self._cost(model, total_in, total_out),
                    attempts=attempts,
                )
                return parsed, call
            except (ValidationError, ValueError) as exc:
                last_err = str(exc)[:500]
                log.warning("LLM returned invalid structured output, retrying once: %s", last_err)
                attempt_prompt = (
                    f"{prompt}\n\nYour previous reply was not valid JSON for the required schema "
                    f"({last_err}). Reply again with ONLY valid JSON."
                )
        raise LLMError("bad_output", f"Model output failed schema validation: {last_err}")

    async def embed(self, texts: list[str], *, kind: Literal["document", "query"]) -> tuple[list[list[float]], LLMCall]:
        if not self.embeddings_enabled:
            raise LLMError("disabled", "Embeddings need GEMINI_API_KEY.")
        model = self.s.gemini_embed_model
        v2_style = "embedding-2" in model
        started = time.perf_counter()
        vectors: list[list[float]] = []
        attempts = 0
        for i in range(0, len(texts), 100):
            batch = texts[i : i + 100]
            reqs = []
            for t in batch:
                req: dict[str, Any] = {"model": f"models/{model}"}
                if v2_style:
                    text = f"task: search result | query: {t}" if kind == "query" else f"title: none | text: {t}"
                    req["content"] = {"parts": [{"text": text}]}
                else:
                    req["content"] = {"parts": [{"text": t}]}
                    req["taskType"] = "RETRIEVAL_QUERY" if kind == "query" else "RETRIEVAL_DOCUMENT"
                    req["outputDimensionality"] = self.s.gemini_embed_dim
                reqs.append(req)
            data, n = await self._post_with_retry(
                f"{GEMINI_BASE}/models/{model}:batchEmbedContents",
                {"requests": reqs},
                headers={"x-goog-api-key": self.s.gemini_api_key},
            )
            attempts += n
            vectors.extend(e["values"] for e in data.get("embeddings", []))
        if len(vectors) != len(texts):
            raise LLMError("bad_output", "Embedding count mismatch")
        approx_tokens = sum(len(t) for t in texts) // 4
        return vectors, LLMCall(
            provider="gemini",
            model=model,
            input_tokens=approx_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
            cost_usd=self._cost(model, approx_tokens, 0),
            attempts=attempts,
        )

    # ------------------------------------------------------------------ providers
    async def _gemini(
        self,
        model: str,
        system: str,
        prompt: str,
        json_schema: dict[str, Any],
        images: list[tuple[bytes, str]] | None,
        temperature: float,
    ) -> tuple[str, int, int, int]:
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for img, mime in images or []:
            parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(img).decode()}})
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
                "responseSchema": to_gemini_schema(json_schema),
            },
        }
        url = f"{GEMINI_BASE}/models/{model}:generateContent"
        headers = {"x-goog-api-key": self.s.gemini_api_key}
        try:
            data, n = await self._post_with_retry(url, body, headers=headers)
        except LLMError as exc:
            # Some models reject parts of a schema; fall back to prompt-described JSON once.
            if exc.kind == "config" and "schema" in str(exc).lower():
                body["generationConfig"].pop("responseSchema")
                body["contents"][0]["parts"][0]["text"] = (
                    f"{prompt}\n\nReturn JSON matching this JSON schema:\n{json.dumps(json_schema)}"
                )
                data, n = await self._post_with_retry(url, body, headers=headers)
            else:
                raise
        cands = data.get("candidates") or []
        if not cands:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMError("bad_output", f"Gemini returned no answer ({reason})")
        cand = cands[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
        if not text:
            raise LLMError("bad_output", f"Gemini returned empty content (finishReason={cand.get('finishReason')})")
        usage = data.get("usageMetadata") or {}
        out_tokens = int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0))
        return text, int(usage.get("promptTokenCount", 0)), out_tokens, n

    async def _anthropic(
        self,
        model: str,
        system: str,
        prompt: str,
        json_schema: dict[str, Any],
        images: list[tuple[bytes, str]] | None,
        temperature: float,
    ) -> tuple[dict[str, Any] | str, int, int, int]:
        content: list[dict[str, Any]] = []
        for img, mime in images or []:
            content.append(
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(img).decode()}}
            )
        content.append({"type": "text", "text": prompt})
        body = {
            "model": model,
            "max_tokens": 4096,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "tools": [
                {"name": "emit", "description": "Return the structured result.", "input_schema": _strip_titles(json_schema)}
            ],
            "tool_choice": {"type": "tool", "name": "emit"},
        }
        headers = {"x-api-key": self.s.anthropic_api_key, "anthropic-version": "2023-06-01"}
        data, n = await self._post_with_retry(ANTHROPIC_URL, body, headers=headers)
        usage = data.get("usage") or {}
        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                return block.get("input") or {}, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)), n
        raise LLMError("bad_output", "Anthropic response had no tool_use block")

    async def _post_with_retry(
        self, url: str, body: dict[str, Any], headers: dict[str, str], max_attempts: int = 3
    ) -> tuple[dict[str, Any], int]:
        last: LLMError | None = None
        async with self._client() as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = await client.post(url, json=body, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last = LLMError("network", f"{type(exc).__name__}: {exc}")
                else:
                    if resp.status_code < 300:
                        return resp.json(), attempt
                    last = _classify_http_error(resp)
                    if last.kind in ("auth", "config"):
                        raise last
                    if last.kind == "rate_limit" and resp.headers.get("retry-after"):
                        try:
                            await asyncio.sleep(min(float(resp.headers["retry-after"]), 30.0))
                            continue
                        except ValueError:
                            pass
                if attempt < max_attempts:
                    await asyncio.sleep(self._backoff_base * 2 ** (attempt - 1))
        assert last is not None
        raise last


# ---------------------------------------------------------------------- helpers
def _classify_http_error(resp: httpx.Response) -> LLMError:
    text = resp.text[:400]
    code = resp.status_code
    if code in (401, 403) or "API_KEY_INVALID" in text or "API key not valid" in text:
        return LLMError("auth", f"Provider rejected the API key (HTTP {code}). Check your API key.", code)
    if code == 404:
        return LLMError("config", f"Model or endpoint not found (HTTP 404). Check model names. {text}", code)
    if code == 429:
        return LLMError("rate_limit", "Provider rate limit hit (HTTP 429).", code)
    if code >= 500:
        return LLMError("server", f"Provider server error (HTTP {code}).", code)
    return LLMError("config", f"Provider rejected the request (HTTP {code}): {text}", code)


def _parse_json(raw: str) -> Any:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    return json.loads(text)


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve $ref/$defs from Pydantic schemas so every provider receives a flat schema."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result: dict[str, Any] = walk(schema)
    return result


def _strip_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if k not in ("title", "default")}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


_GEMINI_KEYS = {"type", "properties", "required", "items", "enum", "description", "nullable", "format"}


def to_gemini_schema(node: Any) -> Any:
    """Convert JSON schema to the OpenAPI subset accepted by Gemini's responseSchema."""
    if isinstance(node, list):
        return [to_gemini_schema(v) for v in node]
    if not isinstance(node, dict):
        return node
    if "anyOf" in node:  # Optional[X] -> X with nullable
        options = [o for o in node["anyOf"] if o.get("type") != "null"]
        converted = to_gemini_schema(options[0]) if options else {"type": "STRING"}
        if len(options) < len(node["anyOf"]):
            converted["nullable"] = True
        if "description" in node:
            converted["description"] = node["description"]
        return converted
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _GEMINI_KEYS:
            continue
        if key == "type":
            out["type"] = str(value).upper()
        elif key == "properties":
            out["properties"] = {k: to_gemini_schema(v) for k, v in value.items()}
        elif key == "items":
            out["items"] = to_gemini_schema(value)
        elif key == "format" and value not in ("date-time", "enum"):
            continue
        else:
            out[key] = value
    if "enum" in out and "type" not in out:
        out["type"] = "STRING"
    return out
