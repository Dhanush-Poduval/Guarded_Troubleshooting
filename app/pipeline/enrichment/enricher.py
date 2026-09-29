"""Phase 0: query enrichment."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from app.pipeline.enrichment.prompts import (
    QUERY_ENRICHMENT_SYSTEM_PROMPT,
    build_enrichment_prompt,
)
from app.pipeline.enrichment.schema import EnrichedQuery


class QueryEnrichmentError(RuntimeError):
    """Raised when query enrichment cannot produce a valid result."""


class QueryEnricher:
    """Normalize user complaints and generate semantic paraphrases."""

    RETRYABLE_STATUS_CODES = {
        429,
        500,
        502,
        503,
        504,
    }

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._api_key = api_key or os.getenv("GEMINI_API_KEY")

        self._model = (
            model
            or os.getenv("GEMINI_MODEL")
            or "gemini-3.5-flash"
        )

        self._fallback_model = (
            fallback_model
            or os.getenv("GEMINI_FALLBACK_MODEL")
            or "gemini-3.5-flash-lite"
        )

        self._timeout_s = timeout_s

        if not self._api_key:
            raise ValueError(
                "GEMINI_API_KEY is not configured. "
                "Add it to the environment or pass api_key explicitly."
            )

    @property
    def model(self) -> str:
        return self._model

    @property
    def fallback_model(self) -> str:
        return self._fallback_model

    async def enrich(
        self,
        query: str,
    ) -> EnrichedQuery:
        """Convert a raw complaint into a normalized semantic query."""

        query = " ".join(query.split()).strip()

        if not query:
            raise ValueError("query cannot be empty")

        raw = await asyncio.to_thread(
            self._call_gemini,
            QUERY_ENRICHMENT_SYSTEM_PROMPT,
            build_enrichment_prompt(query),
        )

        payload = self._parse_json(raw)

        try:
            return EnrichedQuery.model_validate(payload)

        except Exception as exc:
            raise QueryEnrichmentError(
                "Gemini returned an invalid enrichment "
                f"response: {exc}"
            ) from exc

    def _call_gemini(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """
        Call Gemini.

        The primary model is tried first. If it remains unavailable
        after retries, the configured fallback model is attempted.
        """

        models = [self._model]

        if (
            self._fallback_model
            and self._fallback_model != self._model
        ):
            models.append(self._fallback_model)

        last_error: Exception | None = None

        for model in models:
            try:
                return self._call_model(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                )

            except QueryEnrichmentError as exc:
                last_error = exc

        raise QueryEnrichmentError(
            "All configured Gemini models failed. "
            f"Last error: {last_error}"
        ) from last_error

    def _call_model(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Call one Gemini model with retry handling."""

        encoded_model = urllib.parse.quote(
            model,
            safe="",
        )

        url = (
            "https://generativelanguage.googleapis.com/"
            "v1beta/models/"
            f"{encoded_model}:generateContent"
        )

        body = {
            "system_instruction": {
                "parts": [
                    {
                        "text": system_prompt,
                    }
                ]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": user_prompt,
                        }
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.35,
                "responseMimeType": "application/json",
            },
        }

        max_attempts = 4
        last_error: Exception | None = None

        for attempt in range(max_attempts):
            request = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self._api_key,
                },
                method="POST",
            )

            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self._timeout_s,
                ) as response:
                    response_body = json.loads(
                        response.read().decode("utf-8")
                    )

                return self._extract_text(response_body)

            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode(
                    "utf-8",
                    errors="replace",
                )

                last_error = QueryEnrichmentError(
                    f"Gemini model '{model}' returned "
                    f"HTTP {exc.code}: {error_body}"
                )

                if (
                    exc.code in self.RETRYABLE_STATUS_CODES
                    and attempt < max_attempts - 1
                ):
                    time.sleep(2 ** attempt)
                    continue

                raise last_error from exc

            except urllib.error.URLError as exc:
                last_error = QueryEnrichmentError(
                    f"Could not reach Gemini model "
                    f"'{model}': {exc}"
                )

                if attempt < max_attempts - 1:
                    time.sleep(2 ** attempt)
                    continue

                raise last_error from exc

        raise QueryEnrichmentError(
            f"Gemini model '{model}' failed after "
            f"{max_attempts} attempts: {last_error}"
        )

    @staticmethod
    def _extract_text(
        response_body: dict,
    ) -> str:
        """Extract generated text from Gemini response."""

        try:
            return (
                response_body["candidates"][0]
                ["content"]["parts"][0]["text"]
            )

        except (KeyError, IndexError, TypeError) as exc:
            raise QueryEnrichmentError(
                "Gemini returned an unexpected "
                "response structure"
            ) from exc

    @staticmethod
    def _parse_json(
        text: str,
    ) -> dict:
        """Parse JSON while tolerating markdown fences."""

        text = text.strip()

        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )

        text = re.sub(
            r"\s*```$",
            "",
            text,
        )

        try:
            payload = json.loads(text)

        except json.JSONDecodeError as exc:
            raise QueryEnrichmentError(
                "Gemini did not return valid JSON"
            ) from exc

        if not isinstance(payload, dict):
            raise QueryEnrichmentError(
                "Gemini enrichment response must "
                "be a JSON object"
            )

        return payload
