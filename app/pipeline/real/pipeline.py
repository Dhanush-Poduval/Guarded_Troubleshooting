"""Real Phase 0-2 troubleshooting pipeline.

Phase 0:
    Query enrichment

Phase 1:
    Grounded SIIS structure extraction

Phase 2:
    Deeplink resolution

The pipeline converts a raw troubleshooting query and its SIIS
reference into the final ContextDeeplinkResponse contract.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg

from app.config import Settings
from app.contract.schema import (
    Action,
    ContextDeeplinkResponse,
    Deeplink,
    Goal,
    StepGroup,
    ValidationDeepLink,
    actionCategory,
)
from app.embeddings.encoder import Encoder
from app.pipeline.enrichment.enricher import QueryEnricher
from app.pipeline.extraction.extractor import StructureExtractor
from app.pipeline.port import (
    PipelineMeta,
    PipelineRequest,
    PipelineResult,
)
from app.retrieval.resolver import (
    BM25Index,
    CatalogMatch,
    resolve_deeplink,
)


class RealPipeline:
    """Production Phase 0-2 troubleshooting pipeline."""

    name = "real"

    def __init__(
        self,
        *,
        conn_factory: Callable[[], psycopg.Connection],
        encoder: Encoder,
        bm25_index: BM25Index,
        settings: Settings,
        enricher: QueryEnricher | None = None,
        extractor: StructureExtractor | None = None,
    ) -> None:
        self._conn_factory = conn_factory
        self._encoder = encoder
        self._bm25_index = bm25_index
        self._settings = settings

        self._enricher = (
            enricher
            if enricher is not None
            else QueryEnricher()
        )

        self._extractor = (
            extractor
            if extractor is not None
            else StructureExtractor()
        )

    async def run(
        self,
        request: PipelineRequest,
    ) -> PipelineResult:
        """Run query enrichment, extraction, and deeplink mapping."""

        # --------------------------------------------------
        # Phase 0: Query enrichment
        # --------------------------------------------------

        enriched = await self._enricher.enrich(
            request.query
        )

        # --------------------------------------------------
        # SIIS availability
        # --------------------------------------------------

        siis_reference = (
            request.siis_response or ""
        ).strip()

        if not siis_reference:
            return PipelineResult(
                canonical_query=enriched.canonical_query,
                query_variations=enriched.query_variations,
                response=ContextDeeplinkResponse(
                    contexts=[]
                ),
                meta=self._meta(),
                fallback="no_siis_context",
            )

        # --------------------------------------------------
        # Phase 1: Structure extraction
        # --------------------------------------------------

        extracted = await self._extractor.extract(
            enriched=enriched,
            siis_reference=siis_reference,
        )

        # --------------------------------------------------
        # Phase 2: Deeplink resolution
        # --------------------------------------------------

        actions: list[Action] = []

        with self._conn_factory() as conn:
            for extracted_action in extracted.actions:
                action = self._build_action(
                    conn=conn,
                    extracted_action=extracted_action,
                )

                actions.append(action)

        # --------------------------------------------------
        # Final response
        # --------------------------------------------------

        goal = Goal(
            goal=(
                "Follow these steps to perform this "
                f"{extracted.topic} Troubleshooting"
            ),
            title=extracted.title,
            actions=actions,
            score=extracted.confidence,
        )

        response = ContextDeeplinkResponse(
            contexts=[goal]
        )

        return PipelineResult(
            canonical_query=enriched.canonical_query,
            query_variations=enriched.query_variations,
            response=response,
            meta=self._meta(),
            fallback=None,
        )

    # ==================================================
    # Action construction
    # ==================================================

    def _build_action(
        self,
        *,
        conn: psycopg.Connection,
        extracted_action,
    ) -> Action:
        """Convert one extracted action into final contract form."""

        category = actionCategory(
            extracted_action.category.value
        )

        # Manual actions intentionally do not receive
        # actionable deeplinks.
        if category == actionCategory.manual:
            match = None

        else:
            intent_text = self._build_intent_text(
                extracted_action
            )

            match = resolve_deeplink(
                conn,
                intent_text,
                bm25_index=self._bm25_index,
                encoder=self._encoder,
                settings=self._settings,
            )

        step_group = StepGroup(
            steps=list(
                extracted_action.steps
            ),
            validationDeeplink=(
                self._build_validation_deeplink(
                    match
                )
            ),
            actionableDeeplink=(
                self._build_actionable_deeplink(
                    match
                )
            ),
        )

        return Action(
            actionName=extracted_action.action_name,
            description=self._description(
                extracted_action
            ),
            stepGroups=[
                step_group
            ],
            category=category,
        )

    # ==================================================
    # Retrieval intent
    # ==================================================

    @staticmethod
    def _build_intent_text(
        extracted_action,
    ) -> str:
        """Build descriptive text for hybrid catalog retrieval."""

        parts: list[str] = [
            extracted_action.action_name,
        ]

        target_screen = getattr(
            extracted_action,
            "target_screen",
            None,
        )

        if target_screen:
            parts.append(
                target_screen
            )

        steps = getattr(
            extracted_action,
            "steps",
            None,
        )

        if steps:
            parts.extend(
                steps
            )

        return " ".join(
            part.strip()
            for part in parts
            if part and part.strip()
        )

    # ==================================================
    # Deeplink conversion
    # ==================================================

    @staticmethod
    def _build_actionable_deeplink(
        match: CatalogMatch | None,
    ) -> Deeplink | None:
        """Convert catalog match into contract Deeplink."""

        if match is None:
            return None

        return Deeplink(
            deeplink=match.deeplink,
            description=match.description,
            message=match.message or "",
            originalType=match.original_type,
        )

    @staticmethod
    def _build_validation_deeplink(
        match: CatalogMatch | None,
    ) -> ValidationDeepLink | None:
        """Build validation deeplink when catalog provides one."""

        if match is None:
            return None

        if not match.validation_deeplink:
            return None

        if not match.validation_key:
            return None

        payload = {
            "deeplink":
                match.validation_deeplink,
            "key":
                match.validation_key,
        }

        if match.validation_result_type:
            payload["resultType"] = (
                match.validation_result_type
            )

        if match.validation_condition:
            payload["condition"] = (
                match.validation_condition
            )

        if match.validation_value is not None:
            payload["value"] = (
                match.validation_value
            )

        try:
            return ValidationDeepLink(
                **payload
            )

        except ValueError:
            # Catalog validation metadata should not make
            # an otherwise valid actionable deeplink unusable.
            return None

    # ==================================================
    # Description
    # ==================================================

    @staticmethod
    def _description(
        extracted_action,
    ) -> str:
        """
        Generate the required short action description.

        Contract:
            - starts with "It will"
            - 5-7 words
        """

        target = (
            getattr(
                extracted_action,
                "target_screen",
                None,
            )
            or extracted_action.action_name
        )

        words = target.split()

        # "It will open your" = 4 words.
        # Add up to 3 target words -> total 5-7.
        target_words = words[:3]

        if not target_words:
            target_words = [
                "device",
            ]

        return (
            "It will open your "
            + " ".join(target_words)
        )

    # ==================================================
    # Metadata
    # ==================================================

    def _meta(
        self,
    ) -> PipelineMeta:
        """Return pipeline metadata."""

        return PipelineMeta(
            model=self._enricher.model,
            cost_usd=0.0,
            prompt_tokens=0,
            completion_tokens=0,
        )
