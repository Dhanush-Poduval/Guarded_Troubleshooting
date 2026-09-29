"""Phase 1: grounded structure extraction from SIIS text."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from app.pipeline.enrichment.schema import EnrichedQuery
from app.pipeline.extraction.prompts import (
    STRUCTURE_EXTRACTION_SYSTEM_PROMPT,
    build_extraction_prompt,
)
from app.pipeline.extraction.schema import ExtractedPlan


class StructureExtractionError(RuntimeError):
    """Raised when grounded structure extraction fails."""


class StructureExtractor:
    """Convert SIIS reference text into structured troubleshooting actions."""

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
        timeout_s: float = 45.0,
    ) -> None:
        self._api_key = (
            api_key
            or os.getenv("GEMINI_API_KEY")
        )

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
                "Add it to the environment or pass "
                "api_key explicitly."
            )

    # ==============================================================
    # Properties
    # ==============================================================

    @property
    def model(self) -> str:
        return self._model

    @property
    def fallback_model(self) -> str:
        return self._fallback_model

    # ==============================================================
    # Public extraction API
    # ==============================================================

    async def extract(
        self,
        *,
        enriched: EnrichedQuery,
        siis_reference: str,
    ) -> ExtractedPlan:
        """
        Extract a grounded troubleshooting plan from SIIS text.
        """

        siis_reference = siis_reference.strip()

        if not siis_reference:
            raise ValueError(
                "siis_reference cannot be empty"
            )

        raw = await asyncio.to_thread(
            self._call_gemini,
            STRUCTURE_EXTRACTION_SYSTEM_PROMPT,
            build_extraction_prompt(
                enriched,
                siis_reference,
            ),
        )

        payload = self._parse_json(raw)

        try:
            plan = ExtractedPlan.model_validate(
                payload
            )

        except Exception as exc:
            raise StructureExtractionError(
                "Gemini returned an invalid extraction "
                f"response: {exc}"
            ) from exc

        # Verify that every action is supported by
        # the supplied SIIS reference.
        self._verify_grounding(
            plan,
            siis_reference,
        )

        # Disruptive / critical actions must occur last.
        self._ensure_critical_last(
            plan
        )

        return plan

    # ==============================================================
    # Gemini model selection
    # ==============================================================

    def _call_gemini(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """
        Call the configured primary Gemini model.

        If it fails, try the configured fallback model.
        """

        models = [
            self._model,
        ]

        if (
            self._fallback_model
            and self._fallback_model
            != self._model
        ):
            models.append(
                self._fallback_model
            )

        last_error: Exception | None = None

        for model in models:
            try:
                return self._call_model(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                )

            except StructureExtractionError as exc:
                last_error = exc

        raise StructureExtractionError(
            "All configured Gemini models failed. "
            f"Last error: {last_error}"
        ) from last_error

    # ==============================================================
    # Gemini HTTP request
    # ==============================================================

    def _call_model(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """
        Call one Gemini model with retry handling.
        """

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
                "temperature": 0.05,
                "responseMimeType": "application/json",
            },
        }

        max_attempts = 4
        last_error: Exception | None = None

        for attempt in range(
            max_attempts
        ):
            request = urllib.request.Request(
                url,
                data=json.dumps(
                    body
                ).encode("utf-8"),
                headers={
                    "Content-Type":
                        "application/json",
                    "x-goog-api-key":
                        self._api_key,
                },
                method="POST",
            )

            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self._timeout_s,
                ) as response:
                    response_body = json.loads(
                        response
                        .read()
                        .decode("utf-8")
                    )

                return self._extract_text(
                    response_body
                )

            except urllib.error.HTTPError as exc:
                error_body = (
                    exc.read().decode(
                        "utf-8",
                        errors="replace",
                    )
                )

                last_error = (
                    StructureExtractionError(
                        f"Gemini model '{model}' "
                        f"returned HTTP {exc.code}: "
                        f"{error_body}"
                    )
                )

                if (
                    exc.code
                    in self.RETRYABLE_STATUS_CODES
                    and attempt
                    < max_attempts - 1
                ):
                    time.sleep(
                        2 ** attempt
                    )
                    continue

                raise last_error from exc

            except urllib.error.URLError as exc:
                last_error = (
                    StructureExtractionError(
                        "Could not reach Gemini "
                        f"model '{model}': {exc}"
                    )
                )

                if (
                    attempt
                    < max_attempts - 1
                ):
                    time.sleep(
                        2 ** attempt
                    )
                    continue

                raise last_error from exc

        raise StructureExtractionError(
            f"Gemini model '{model}' "
            f"failed after {max_attempts} "
            f"attempts: {last_error}"
        )

    # ==============================================================
    # Gemini response handling
    # ==============================================================

    @staticmethod
    def _extract_text(
        response_body: dict,
    ) -> str:
        """
        Extract generated text from a Gemini response.
        """

        try:
            return (
                response_body
                ["candidates"][0]
                ["content"]
                ["parts"][0]
                ["text"]
            )

        except (
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            raise StructureExtractionError(
                "Gemini returned an unexpected "
                "response structure"
            ) from exc

    # ==============================================================
    # JSON parsing
    # ==============================================================

    @staticmethod
    def _parse_json(
        text: str,
    ) -> dict:
        """
        Parse JSON returned by Gemini.
        """

        text = text.strip()

        # Remove optional markdown JSON fences.
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
            payload = json.loads(
                text
            )

        except json.JSONDecodeError as exc:
            raise StructureExtractionError(
                "Gemini did not return "
                "valid JSON"
            ) from exc

        if not isinstance(
            payload,
            dict,
        ):
            raise StructureExtractionError(
                "Extraction response must "
                "be a JSON object"
            )

        return payload

    # ==============================================================
    # SIIS reference preparation
    # ==============================================================

    @staticmethod
    def _extract_reference_text(
        reference_text: str,
    ) -> str:
        """
        Convert an SIIS reference into plain text.

        Official SIIS entries may be represented as serialized JSON:

            {
                "title": "...",
                "content": "line 1\\nline 2\\nline 3"
            }

        Running json.loads() first converts escaped newlines and other
        JSON escape sequences into their actual characters.

        Grounding should be performed against the real SIIS content,
        rather than against its serialized JSON representation.
        """

        reference_text = (
            reference_text.strip()
        )

        if not reference_text:
            return ""

        try:
            payload = json.loads(
                reference_text
            )

        except (
            json.JSONDecodeError,
            TypeError,
        ):
            # Already plain text.
            return reference_text

        # ----------------------------------------------------------
        # Dictionary SIIS format
        # ----------------------------------------------------------

        if isinstance(
            payload,
            dict,
        ):
            parts: list[str] = []

            title = payload.get(
                "title"
            )

            content = payload.get(
                "content"
            )

            if isinstance(
                title,
                str,
            ):
                parts.append(
                    title
                )

            if isinstance(
                content,
                str,
            ):
                parts.append(
                    content
                )

            # Preferred official structure found.
            if parts:
                return "\n".join(
                    parts
                )

            # Generic dictionary fallback.
            for value in payload.values():
                if isinstance(
                    value,
                    str,
                ):
                    parts.append(
                        value
                    )

            if parts:
                return "\n".join(
                    parts
                )

        # ----------------------------------------------------------
        # List SIIS format
        # ----------------------------------------------------------

        if isinstance(
            payload,
            list,
        ):
            parts = [
                item
                for item in payload
                if isinstance(
                    item,
                    str,
                )
            ]

            if parts:
                return "\n".join(
                    parts
                )

        # Unknown JSON structure.
        # Preserve original input rather than discarding it.
        return reference_text

    # ==============================================================
    # Grounding normalization
    # ==============================================================

    @staticmethod
    def _normalise_for_grounding(
        text: str,
    ) -> str:
        """
        Normalize text for deterministic SIIS provenance checks.

        Formatting differences should not cause false negatives.

        For example:

            "Safe-mode"
            "Safe mode"

        both normalize to:

            "safe mode"
        """

        text = text.casefold()

        # Normalize Unicode quotation marks.
        text = (
            text
            .replace("’", "'")
            .replace("‘", "'")
            .replace("“", '"')
            .replace("”", '"')
        )

        # Replace punctuation with spaces.
        #
        # Do not simply delete punctuation because:
        #
        #     "safe-mode"
        #
        # would incorrectly become:
        #
        #     "safemode"
        #
        # Replacing it gives:
        #
        #     "safe mode"
        text = re.sub(
            r"[^\w\s]",
            " ",
            text,
        )

        # Collapse newlines, tabs, and repeated spaces.
        text = re.sub(
            r"\s+",
            " ",
            text,
        )

        return text.strip()

    # ==============================================================
    # Grounding validation
    # ==============================================================

    @classmethod
    def _is_grounded(
        cls,
        evidence: str,
        reference_text: str,
    ) -> bool:
        """
        Determine whether evidence is supported by SIIS.

        Validation has two stages:

        1. Exact normalized substring matching.
        2. Conservative token coverage for minor formatting
           differences.

        The fallback remains deliberately strict to prevent unrelated
        or hallucinated troubleshooting instructions from passing.
        """

        # Convert serialized SIIS JSON into real text first.
        plain_reference = (
            cls._extract_reference_text(
                reference_text
            )
        )

        normalized_reference = (
            cls._normalise_for_grounding(
                plain_reference
            )
        )

        normalized_evidence = (
            cls._normalise_for_grounding(
                evidence
            )
        )

        if not normalized_evidence:
            return False

        if not normalized_reference:
            return False

        # ----------------------------------------------------------
        # Stage 1:
        # Exact normalized contiguous match.
        # ----------------------------------------------------------

        if (
            normalized_evidence
            in normalized_reference
        ):
            return True

        # ----------------------------------------------------------
        # Stage 2:
        # Conservative token coverage.
        # ----------------------------------------------------------

        evidence_tokens = (
            normalized_evidence.split()
        )

        reference_tokens = set(
            normalized_reference.split()
        )

        # Short snippets must match exactly.
        #
        # Fuzzy matching very short text would be too permissive.
        if len(
            evidence_tokens
        ) < 5:
            return False

        supported_tokens = sum(
            1
            for token in evidence_tokens
            if token in reference_tokens
        )

        coverage = (
            supported_tokens
            / len(evidence_tokens)
        )

        # Keep this deliberately strict.
        return coverage >= 0.90

    def _verify_grounding(
        self,
        plan: ExtractedPlan,
        reference_text: str,
    ) -> None:
        """
        Ensure every extracted action contains evidence
        supported by the supplied SIIS reference.
        """

        for index, action in enumerate(
            plan.actions
        ):
            evidence = (
                action
                .source_text
                .strip()
            )

            if not evidence:
                raise StructureExtractionError(
                    f"Action {index} has "
                    "empty grounding evidence"
                )

            if not self._is_grounded(
                evidence,
                reference_text,
            ):
                raise StructureExtractionError(
                    "Extraction failed grounding check: "
                    f"source_text for action {index} "
                    "was not sufficiently supported by "
                    "the supplied SIIS reference. "
                    f"Evidence: {evidence!r}"
                )

    # ==============================================================
    # Critical action sequencing
    # ==============================================================

    @staticmethod
    def _ensure_critical_last(
        plan: ExtractedPlan,
    ) -> None:
        """
        Ensure critical/disruptive actions occur only after
        all non-critical actions.
        """

        seen_critical = False

        for index, action in enumerate(
            plan.actions
        ):
            if (
                action.category.value
                == "critical"
            ):
                seen_critical = True
                continue

            if seen_critical:
                raise StructureExtractionError(
                    "Invalid action sequence: "
                    f"non-critical action at index "
                    f"{index} follows a "
                    "critical action"
                )
