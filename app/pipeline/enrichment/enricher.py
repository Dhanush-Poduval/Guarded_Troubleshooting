"""Phase 0: Query Enrichment."""

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
    SYSTEM_PROMPT,
    build_user_prompt,
)
from app.pipeline.enrichment.schema import EnrichedQuery


class QueryEnrichmentError(RuntimeError):
    """Raised when query enrichment fails."""


# ============================================================
# IMPORTANT SOURCE ENTITIES
# ============================================================

# These terms should not disappear during enrichment when they
# were explicitly present in the original customer query.
#
# This is intentionally conservative. We only preserve known
# technically meaningful names rather than arbitrary words.
_PRESERVED_ENTITY_DISPLAY_NAMES = {
    "samsung": "Samsung",
    "gmail": "Gmail",
    "android": "Android",
    "galaxy": "Galaxy",
    "whatsapp": "WhatsApp",
    "youtube": "YouTube",
    "bluetooth": "Bluetooth",
    "wifi": "Wi-Fi",
    "wi-fi": "Wi-Fi",
}


# ============================================================
# IDENTIFIER DETECTION
# ============================================================

# Matches tokens containing BOTH a letter and a digit.
#
# Examples:
#
# A115G
# S23
# M31
# SM-A115F
# SM-S911B
#
_IDENTIFIER_RE = re.compile(
    r"\b"
    r"(?=[A-Za-z0-9-]*[A-Za-z])"
    r"(?=[A-Za-z0-9-]*\d)"
    r"[A-Za-z0-9]+"
    r"(?:-[A-Za-z0-9]+)*"
    r"\b"
)


def _extract_identifiers(
    text: str,
) -> list[str]:
    """Extract likely device/product identifiers."""

    return _IDENTIFIER_RE.findall(text)


def _contains_identifier(
    text: str,
    identifier: str,
) -> bool:
    """Return True if an identifier appears intact."""

    return bool(
        re.search(
            rf"(?<![A-Za-z0-9])"
            rf"{re.escape(identifier)}"
            rf"(?![A-Za-z0-9])",
            text,
            flags=re.IGNORECASE,
        )
    )


def _restore_identifier(
    generated_text: str,
    identifier: str,
) -> str:
    """
    Repair an identifier when Gemini only inserted spaces.

    Example:

        original:
            A115G

        generated:
            A11 5G

        repaired:
            A115G
    """

    if _contains_identifier(
        generated_text,
        identifier,
    ):
        return generated_text

    # A115G becomes:
    #
    # A\s*1\s*1\s*5\s*G
    #
    # Therefore "A11 5G" can be repaired safely.
    pieces = [
        re.escape(character)
        for character in identifier
    ]

    pattern = r"\s*".join(pieces)

    return re.sub(
        rf"(?<![A-Za-z0-9])"
        rf"{pattern}"
        rf"(?![A-Za-z0-9])",
        identifier,
        generated_text,
        count=1,
        flags=re.IGNORECASE,
    )


def _restore_identifiers(
    original_query: str,
    generated_text: str,
) -> str:
    """Restore identifiers from the original query."""

    result = generated_text

    for identifier in _extract_identifiers(
        original_query
    ):
        result = _restore_identifier(
            result,
            identifier,
        )

    return result


# ============================================================
# NAMED ENTITY PRESERVATION
# ============================================================


def _source_contains_entity(
    text: str,
    entity: str,
) -> bool:
    """Check whether a named entity occurs in source text."""

    if entity == "wifi":
        return bool(
            re.search(
                r"\bwi[\s-]?fi\b",
                text,
                flags=re.IGNORECASE,
            )
        )

    if entity == "wi-fi":
        return bool(
            re.search(
                r"\bwi[\s-]?fi\b",
                text,
                flags=re.IGNORECASE,
            )
        )

    return bool(
        re.search(
            rf"\b{re.escape(entity)}\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _generated_contains_entity(
    text: str,
    entity: str,
) -> bool:
    """Check whether generated text preserved an entity."""

    return _source_contains_entity(
        text,
        entity,
    )


def _extract_required_entities(
    original_query: str,
) -> list[str]:
    """
    Extract known technically relevant entities that were
    explicitly present in the source query.
    """

    found: list[str] = []

    seen_display_names: set[str] = set()

    for (
        entity,
        display_name,
    ) in _PRESERVED_ENTITY_DISPLAY_NAMES.items():

        if not _source_contains_entity(
            original_query,
            entity,
        ):
            continue

        normalized_display = (
            display_name.casefold()
        )

        if normalized_display in seen_display_names:
            continue

        found.append(display_name)

        seen_display_names.add(
            normalized_display
        )

    return found


def _restore_named_entities(
    original_query: str,
    generated_text: str,
) -> str:
    """
    Restore technically important named entities dropped
    entirely by the language model.

    Example:

        original:
            samsung a115g screen flashing gmail

        model:
            A115G screen flashes when Gmail opens

        repaired:
            Samsung A115G screen flashes when Gmail opens

    We only restore entities that actually existed in the
    original query.
    """

    result = generated_text.strip()

    required_entities = (
        _extract_required_entities(
            original_query
        )
    )

    missing: list[str] = []

    for display_name in required_entities:

        lookup = display_name.casefold()

        # Special handling for Wi-Fi.
        if lookup == "wi-fi":
            exists = bool(
                re.search(
                    r"\bwi[\s-]?fi\b",
                    result,
                    flags=re.IGNORECASE,
                )
            )

        else:
            exists = bool(
                re.search(
                    rf"\b"
                    rf"{re.escape(display_name)}"
                    rf"\b",
                    result,
                    flags=re.IGNORECASE,
                )
            )

        if not exists:
            missing.append(
                display_name
            )

    if not missing:
        return result

    # Prefix only entities genuinely lost by Gemini.
    #
    # This is intentionally deterministic. We do NOT ask
    # Gemini another time just to restore a source entity.
    return (
        " ".join(missing)
        + " "
        + result
    ).strip()


# ============================================================
# QUERY ENRICHER
# ============================================================


class QueryEnricher:
    """
    Phase 0 query enrichment.

    Responsibilities:

    - normalize noisy customer language
    - remove slang/filler/emojis through the LLM
    - preserve troubleshooting intent
    - preserve device identifiers
    - preserve important named entities
    - generate semantic query variations
    """

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
            or os.getenv(
                "GEMINI_API_KEY"
            )
        )

        self._model = (
            model
            or os.getenv(
                "GEMINI_MODEL",
                "gemini-3.5-flash",
            )
        )

        self._fallback_model = (
            fallback_model
            or os.getenv(
                "GEMINI_FALLBACK_MODEL",
                "gemini-3.5-flash-lite",
            )
        )

        self._timeout_s = timeout_s

        if not self._api_key:
            raise QueryEnrichmentError(
                "GEMINI_API_KEY is not configured"
            )

    # ========================================================
    # PUBLIC MODEL PROPERTIES
    # ========================================================

    @property
    def model(self) -> str:
        """
        Primary Gemini model.

        RealPipeline uses this property when constructing
        PipelineMeta.
        """

        return self._model

    @property
    def fallback_model(self) -> str:
        """Configured Gemini fallback model."""

        return self._fallback_model

    # ========================================================
    # ENRICH
    # ========================================================

    async def enrich(
        self,
        query: str,
    ) -> EnrichedQuery:
        """Enrich a raw customer troubleshooting query."""

        # Preserve original text for entity/identifier checks.
        original_query = query

        # Normalize whitespace only.
        #
        # Do NOT lowercase the source because original
        # identifiers/casing are useful for restoration.
        query = " ".join(
            query.split()
        ).strip()

        if not query:
            raise QueryEnrichmentError(
                "Query cannot be empty"
            )

        # ----------------------------------------------------
        # Prompt
        # ----------------------------------------------------

        user_prompt = build_user_prompt(
            query
        )

        # ----------------------------------------------------
        # Gemini
        # ----------------------------------------------------

        raw = await asyncio.to_thread(
            self._call_gemini,
            SYSTEM_PROMPT,
            user_prompt,
        )

        # ----------------------------------------------------
        # Parse
        # ----------------------------------------------------

        payload = self._parse_json(
            raw
        )

        # ----------------------------------------------------
        # Schema validation
        # ----------------------------------------------------

        try:

            enriched = (
                EnrichedQuery.model_validate(
                    payload
                )
            )

        except Exception as exc:

            raise QueryEnrichmentError(
                "Gemini returned an invalid "
                "EnrichedQuery response: "
                f"{exc}"
            ) from exc

        # ====================================================
        # RESTORE IDENTIFIERS
        # ====================================================

        canonical_query = (
            _restore_identifiers(
                original_query,
                enriched.canonical_query,
            )
        )

        query_variations = [
            _restore_identifiers(
                original_query,
                variation,
            )
            for variation
            in enriched.query_variations
        ]

        # ====================================================
        # RESTORE IMPORTANT NAMED ENTITIES
        # ====================================================

        canonical_query = (
            _restore_named_entities(
                original_query,
                canonical_query,
            )
        )

        query_variations = [
            _restore_named_entities(
                original_query,
                variation,
            )
            for variation
            in query_variations
        ]

        # ====================================================
        # REBUILD VALIDATED MODEL
        # ====================================================

        enriched = enriched.model_copy(
            update={
                "canonical_query":
                    canonical_query,
                "query_variations":
                    query_variations,
            }
        )

        # ====================================================
        # FINAL INTEGRITY CHECK
        # ====================================================

        self._verify_source_integrity(
            original_query,
            enriched,
        )

        return enriched

    # ========================================================
    # SOURCE INTEGRITY
    # ========================================================

    def _verify_source_integrity(
        self,
        original_query: str,
        enriched: EnrichedQuery,
    ) -> None:
        """
        Verify that important source information survived
        enrichment.
        """

        # ----------------------------------------------------
        # Identifiers
        # ----------------------------------------------------

        for identifier in _extract_identifiers(
            original_query
        ):

            if not _contains_identifier(
                enriched.canonical_query,
                identifier,
            ):

                raise QueryEnrichmentError(
                    "Canonical query lost or changed "
                    "protected identifier "
                    f"{identifier!r}. "
                    "Canonical query: "
                    f"{enriched.canonical_query!r}"
                )

        # ----------------------------------------------------
        # Named entities
        # ----------------------------------------------------

        required_entities = (
            _extract_required_entities(
                original_query
            )
        )

        for entity in required_entities:

            if entity.casefold() == "wi-fi":

                exists = bool(
                    re.search(
                        r"\bwi[\s-]?fi\b",
                        enriched.canonical_query,
                        flags=re.IGNORECASE,
                    )
                )

            else:

                exists = bool(
                    re.search(
                        rf"\b"
                        rf"{re.escape(entity)}"
                        rf"\b",
                        enriched.canonical_query,
                        flags=re.IGNORECASE,
                    )
                )

            if not exists:

                raise QueryEnrichmentError(
                    "Canonical query lost important "
                    f"source entity {entity!r}. "
                    "Canonical query: "
                    f"{enriched.canonical_query!r}"
                )

    # ========================================================
    # PRIMARY + FALLBACK
    # ========================================================

    def _call_gemini(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Try primary Gemini model, then fallback."""

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

        errors: list[str] = []

        for model_name in models:

            try:

                return self._call_model(
                    model_name,
                    system_prompt,
                    user_prompt,
                )

            except QueryEnrichmentError as exc:

                errors.append(
                    f"{model_name}: {exc}"
                )

        raise QueryEnrichmentError(
            "All Gemini models failed. "
            + " | ".join(errors)
        )

    # ========================================================
    # GEMINI REST API
    # ========================================================

    def _call_model(
        self,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Call one Gemini model with retry handling."""

        encoded_model = (
            urllib.parse.quote(
                model_name,
                safe="",
            )
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
                "temperature": 0.25,
                "responseMimeType":
                    "application/json",
            },
        }

        retry_statuses = {
            429,
            500,
            502,
            503,
            504,
        }

        max_attempts = 4

        for attempt in range(
            max_attempts
        ):

            request = urllib.request.Request(
                url,
                data=json.dumps(
                    body
                ).encode(
                    "utf-8"
                ),
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
                        .decode(
                            "utf-8"
                        )
                    )

                try:

                    return (
                        response_body[
                            "candidates"
                        ][0][
                            "content"
                        ][
                            "parts"
                        ][0][
                            "text"
                        ]
                    )

                except (
                    KeyError,
                    IndexError,
                    TypeError,
                ) as exc:

                    raise QueryEnrichmentError(
                        "Gemini response did not "
                        "contain generated text"
                    ) from exc

            # =================================================
            # HTTP ERROR
            # =================================================

            except urllib.error.HTTPError as exc:

                try:

                    error_body = (
                        exc.read().decode(
                            "utf-8",
                            errors="replace",
                        )
                    )

                except Exception:

                    error_body = str(
                        exc
                    )

                retryable = (
                    exc.code
                    in retry_statuses
                )

                if (
                    retryable
                    and attempt
                    < max_attempts - 1
                ):

                    time.sleep(
                        2 ** attempt
                    )

                    continue

                raise QueryEnrichmentError(
                    "Gemini API returned "
                    f"HTTP {exc.code}: "
                    f"{error_body}"
                ) from exc

            # =================================================
            # NETWORK ERROR
            # =================================================

            except urllib.error.URLError as exc:

                if (
                    attempt
                    < max_attempts - 1
                ):

                    time.sleep(
                        2 ** attempt
                    )

                    continue

                raise QueryEnrichmentError(
                    "Gemini network error: "
                    f"{exc}"
                ) from exc

            # =================================================
            # TIMEOUT
            # =================================================

            except TimeoutError as exc:

                if (
                    attempt
                    < max_attempts - 1
                ):

                    time.sleep(
                        2 ** attempt
                    )

                    continue

                raise QueryEnrichmentError(
                    "Gemini request timed out"
                ) from exc

        raise QueryEnrichmentError(
            f"Gemini model "
            f"{model_name!r} "
            "failed after retries"
        )

    # ========================================================
    # JSON
    # ========================================================

    @staticmethod
    def _parse_json(
        text: str,
    ) -> dict:
        """Parse Gemini JSON response."""

        text = text.strip()

        # Gemini may occasionally wrap JSON in ```json.
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

            raise QueryEnrichmentError(
                "Gemini did not return valid JSON"
            ) from exc

        if not isinstance(
            payload,
            dict,
        ):

            raise QueryEnrichmentError(
                "Gemini response must be "
                "a JSON object"
            )

        return payload
