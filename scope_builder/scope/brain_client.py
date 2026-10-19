"""Client for the Knowledge Brain API (Task 1). The Scope Builder never searches the help center itself."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("scope.brain")


class BrainClient:
    def __init__(self, base_url: str, timeout: float = 60.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport

    async def ask(self, question: str) -> dict[str, Any]:
        """Returns the Brain's /v1/ask response, or a structured 'unavailable' result on failure."""
        last_error = ""
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            for attempt in range(2):
                try:
                    resp = await client.post(
                        f"{self.base_url}/v1/ask",
                        # Scope questions are hypothetical client needs, not support gaps: no escalation.
                        json={"question": question[:2000], "source": "scope", "create_escalation": False},
                    )
                    if resp.status_code < 300:
                        data: dict[str, Any] = resp.json()
                        data["available"] = True
                        return data
                    last_error = f"Knowledge Brain HTTP {resp.status_code}"
                    if resp.status_code < 500:
                        break
                except httpx.HTTPError as exc:
                    last_error = f"Knowledge Brain unreachable: {type(exc).__name__}"
                if attempt == 0:
                    await asyncio.sleep(0.5)
        log.warning("brain ask failed: %s", last_error)
        return {"available": False, "answered": False, "answer": "", "citations": [], "confidence": 0.0, "error": last_error}

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0, transport=self._transport) as client:
                return (await client.get(f"{self.base_url}/health")).status_code == 200
        except httpx.HTTPError:
            return False
