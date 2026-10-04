"""Real Phase 0-2 troubleshooting pipeline.

Phase 0:
    Query enrichment

Phase 1:
    Grounded SIIS structure extraction

Phase 2:
    Deeplink candidate retrieval and deterministic verification
"""

from __future__ import annotations

import logging
import re
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
    search_catalog,
)

logger = logging.getLogger(__name__)


class RealPipeline:
    """Production Phase 0-2 troubleshooting pipeline."""

    name = "real"

    _TOKEN_RE = re.compile(
        r"[a-z0-9]+"
    )

    # Words describing generic UI/navigation rather than the
    # actual feature being requested.
    _GENERIC_SCREEN_WORDS = frozenset(
        {
            "setting",
            "settings",
            "screen",
            "page",
            "device",
            "phone",
            "app",
            "apps",
            "application",
            "applications",
            "open",
            "opens",
            "view",
            "menu",
            "your",
            "the",
            "to",
            "in",
            "on",
            "of",
            "and",
        }
    )

    # A single overlap on these terms is not enough to prove
    # two screens represent the same feature.
    _AMBIGUOUS_SINGLE_TOKENS = frozenset(
        {
            "storage",
            "power",
            "mode",
            "data",
            "home",
            "network",
            "connection",
            "connections",
        }
    )

    # A single exact match on these features is distinctive
    # enough to identify a destination.
    _DISTINCTIVE_FEATURE_TOKENS = frozenset(
        {
            "wifi",
            "bluetooth",
            "camera",
            "microphone",
            "accessibility",
        }
    )

    def __init__(
        self,
        *,
        conn_factory: Callable[
            [],
            psycopg.Connection,
        ],
        encoder: Encoder,
        bm25_index: BM25Index,
        settings: Settings,
        enricher: QueryEnricher | None = None,
        extractor: StructureExtractor | None = None,
    ) -> None:

        self._conn_factory = (
            conn_factory
        )

        self._encoder = (
            encoder
        )

        self._bm25_index = (
            bm25_index
        )

        self._settings = (
            settings
        )

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

    # ==========================================================
    # Main pipeline
    # ==========================================================

    async def run(
        self,
        request: PipelineRequest,
    ) -> PipelineResult:
        """
        Run Phase 0, Phase 1 and Phase 2.
        """

        # ------------------------------------------------------
        # Phase 0
        # Query enrichment
        # ------------------------------------------------------

        enriched = (
            await self._enricher.enrich(
                request.query
            )
        )

        # ------------------------------------------------------
        # SIIS reference
        # ------------------------------------------------------

        siis_reference = (
            request.siis_response
            or ""
        ).strip()

        if not siis_reference:

            return PipelineResult(
                canonical_query=(
                    enriched.canonical_query
                ),
                query_variations=(
                    enriched.query_variations
                ),
                response=(
                    ContextDeeplinkResponse(
                        contexts=[]
                    )
                ),
                meta=self._meta(),
                fallback=(
                    "no_siis_context"
                ),
            )

        # ------------------------------------------------------
        # Phase 1
        # Grounded structure extraction
        # ------------------------------------------------------

        extracted = (
            await self._extractor.extract(
                enriched=enriched,
                siis_reference=(
                    siis_reference
                ),
            )
        )

        # ------------------------------------------------------
        # Phase 2
        # Deeplink mapping
        # ------------------------------------------------------

        actions: list[Action] = []

        with self._conn_factory() as conn:

            for extracted_action in (
                extracted.actions
            ):

                action = (
                    self._build_action(
                        conn=conn,
                        extracted_action=(
                            extracted_action
                        ),
                    )
                )

                actions.append(
                    action
                )

        # ------------------------------------------------------
        # Final contract
        # ------------------------------------------------------

        goal = Goal(
            goal=(
                "Follow these steps to perform this "
                f"{extracted.topic} Troubleshooting"
            ),
            title=extracted.title,
            actions=actions,
            score=extracted.confidence,
        )

        return PipelineResult(
            canonical_query=(
                enriched.canonical_query
            ),
            query_variations=(
                enriched.query_variations
            ),
            response=(
                ContextDeeplinkResponse(
                    contexts=[
                        goal
                    ]
                )
            ),
            meta=self._meta(),
            fallback=None,
        )

    # ==========================================================
    # Action construction
    # ==========================================================

    def _build_action(
        self,
        *,
        conn: psycopg.Connection,
        extracted_action,
    ) -> Action:
        """
        Convert an extracted Phase 1 action into the
        public response contract.
        """

        category = actionCategory(
            extracted_action.category.value
        )

        target_screen = getattr(
            extracted_action,
            "target_screen",
            None,
        )

        match: CatalogMatch | None = (
            None
        )

        # ------------------------------------------------------
        # Deeplink lookup is allowed only when:
        #
        # 1. the action is not manual
        # 2. Phase 1 identified a Settings target
        #
        # This prevents unsafe semantic mappings such as:
        #
        # Safe Mode -> Lockdown Mode
        # ------------------------------------------------------

        if (
            category
            != actionCategory.manual
            and target_screen
        ):

            match = (
                self._resolve_verified_deeplink(
                    conn=conn,
                    extracted_action=(
                        extracted_action
                    ),
                )
            )

        # ------------------------------------------------------
        # The auto invariant.
        #
        # "auto" tells the client this action can be carried out by
        # opening a setting. When verification returns no catalog
        # destination there is nothing to open, so the honest final
        # category is manual: the grounded steps are kept and the user
        # follows them by hand.
        #
        # The alternative -- keeping auto and attaching the nearest
        # ranked URI -- is what this whole resolution path exists to
        # prevent, and it is how a plan ends up opening the wrong
        # screen with an authoritative label on it.
        #
        # critical is never downgraded. Its category describes how
        # disruptive the action is, not how it is carried out, and
        # losing it would move a destructive step out of the ordering
        # and acknowledgement rules that depend on it.
        # ------------------------------------------------------

        if (
            category == actionCategory.auto
            and match is None
        ):
            logger.info(
                "no verified catalog destination for %r; "
                "downgrading auto to manual",
                extracted_action.action_name,
            )
            category = actionCategory.manual

        # ------------------------------------------------------
        # Contract conversion
        # ------------------------------------------------------

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

        description = (
            self._description(
                extracted_action
            )
        )

        return Action(
            actionName=(
                extracted_action.action_name
            ),
            description=description,
            stepGroups=[
                step_group
            ],
            category=category,
        )

    # ==========================================================
    # Candidate retrieval
    # ==========================================================

    def _resolve_verified_deeplink(
        self,
        *,
        conn: psycopg.Connection,
        extracted_action,
    ) -> CatalogMatch | None:
        """
        Retrieve multiple catalog candidates and return only a
        deterministically verified screen/feature match.

        Retrieval proposes possibilities.

        Verification decides whether a candidate is safe enough
        to attach to the troubleshooting response.
        """

        intent_text = (
            self._build_intent_text(
                extracted_action
            )
        )

        candidates = search_catalog(
            conn,
            intent_text,
            top_k=10,
            bm25_index=(
                self._bm25_index
            ),
            encoder=(
                self._encoder
            ),
            settings=(
                self._settings
            ),
        )

        if not candidates:
            return None

        target_screen = (
            extracted_action.target_screen
            or extracted_action.action_name
        )

        for candidate in candidates:

            if self._candidate_is_compatible(
                target_screen=(
                    target_screen
                ),
                action_name=(
                    extracted_action.action_name
                ),
                candidate=candidate,
            ):
                return candidate

        # No verified candidate is valid.
        #
        # Never attach a semantically nearby but incorrect URI
        # merely because retrieval ranked it highly.
        return None

    # ==========================================================
    # Candidate verification
    # ==========================================================

    @classmethod
    def _normalize_feature_text(
        cls,
        text: str,
    ) -> str:
        """
        Normalize common feature spelling variants.

        In particular:

            Wi-Fi
            Wi Fi
            WiFi
            wifi

        all become:

            wifi
        """

        normalized = (
            text.casefold()
        )

        normalized = re.sub(
            r"\bwi[\s\-]?fi\b",
            "wifi",
            normalized,
            flags=re.IGNORECASE,
        )

        return normalized

    @classmethod
    def _meaningful_tokens(
        cls,
        text: str,
    ) -> set[str]:
        """
        Return normalized feature tokens while excluding
        generic UI/navigation vocabulary.
        """

        normalized = (
            cls._normalize_feature_text(
                text
            )
        )

        tokens = set(
            cls._TOKEN_RE.findall(
                normalized
            )
        )

        return {
            token
            for token in tokens
            if (
                token
                not in (
                    cls._GENERIC_SCREEN_WORDS
                )
                and len(token) > 2
            )
        }

    @classmethod
    def _candidate_is_compatible(
        cls,
        *,
        target_screen: str,
        action_name: str,
        candidate: CatalogMatch,
    ) -> bool:
        """
        Verify that a retrieved catalog candidate represents
        the same feature/screen requested by Phase 1.

        Semantic similarity by itself is intentionally not
        enough.
        """

        expected_text = " ".join(
            (
                action_name,
                target_screen,
            )
        )

        candidate_text = " ".join(
            part
            for part in (
                candidate.description,
                candidate.message,
                candidate.qna_description,
            )
            if part
        )

        expected_tokens = (
            cls._meaningful_tokens(
                expected_text
            )
        )

        candidate_tokens = (
            cls._meaningful_tokens(
                candidate_text
            )
        )

        if not expected_tokens:
            return False

        if not candidate_tokens:
            return False

        overlap = (
            expected_tokens
            & candidate_tokens
        )

        if not overlap:
            return False

        # ------------------------------------------------------
        # Distinctive features
        # ------------------------------------------------------
        #
        # One exact distinctive feature is sufficient.
        #
        # Example:
        #
        # expected:
        #     Wi-Fi Settings
        #
        # candidate:
        #     Opens WiFi settings
        #
        # Both normalize to "wifi".
        # ------------------------------------------------------

        if (
            overlap
            & cls._DISTINCTIVE_FEATURE_TOKENS
        ):
            return True

        # ------------------------------------------------------
        # Ambiguous single-token matches
        # ------------------------------------------------------

        if len(overlap) == 1:

            token = next(
                iter(
                    overlap
                )
            )

            if (
                token
                in (
                    cls._AMBIGUOUS_SINGLE_TOKENS
                )
            ):
                return False

        # ------------------------------------------------------
        # Non-distinctive destinations require at least two
        # meaningful concepts in common.
        #
        # This prevents:
        #
        # Email App Storage
        #     ->
        # Storage Share
        # ------------------------------------------------------

        return (
            len(overlap) >= 2
        )

    # ==========================================================
    # Retrieval intent
    # ==========================================================

    @staticmethod
    def _build_intent_text(
        extracted_action,
    ) -> str:
        """
        Build the hybrid retrieval query used for Phase 2.
        """

        parts: list[str] = [
            extracted_action.action_name
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
                step
                for step in steps
                if step
            )

        return " ".join(
            part.strip()
            for part in parts
            if (
                part
                and part.strip()
            )
        )

    # ==========================================================
    # Actionable deeplink conversion
    # ==========================================================

    @staticmethod
    def _build_actionable_deeplink(
        match: CatalogMatch | None,
    ) -> Deeplink | None:
        """
        Convert a verified catalog candidate to the public
        actionable deeplink contract.
        """

        if match is None:
            return None

        if not match.deeplink:
            return None

        return Deeplink(
            deeplink=(
                match.deeplink
            ),
            description=(
                match.description
                or ""
            ),
            message=(
                match.message
                or ""
            ),
            originalType=(
                match.original_type
            ),
        )

    # ==========================================================
    # Validation deeplink conversion
    # ==========================================================

    @staticmethod
    def _build_validation_deeplink(
        match: CatalogMatch | None,
    ) -> ValidationDeepLink | None:
        """
        Convert validation metadata belonging to a verified
        catalog candidate.

        Validation deeplinks are NEVER invented. They are copied
        only from the selected official catalog row.
        """

        if match is None:
            return None

        if not (
            match.validation_deeplink
        ):
            return None

        if not (
            match.validation_key
        ):
            return None

        payload = {
            "deeplink": (
                match.validation_deeplink
            ),
            "key": (
                match.validation_key
            ),
        }

        if (
            match.validation_result_type
        ):

            payload[
                "resultType"
            ] = (
                match.validation_result_type
            )

        if (
            match.validation_condition
        ):

            payload[
                "condition"
            ] = (
                match.validation_condition
            )

        if (
            match.validation_value
            is not None
        ):

            payload[
                "value"
            ] = (
                match.validation_value
            )

        try:

            return ValidationDeepLink(
                **payload
            )

        except ValueError:

            # Invalid optional validation metadata should not
            # invalidate an otherwise correct actionable
            # deeplink.
            return None

    # ==========================================================
    # Description
    # ==========================================================

    @staticmethod
    def _description(
        extracted_action,
    ) -> str:
        """
        Build a deterministic contract-safe description.

        Contract:
            - begins with "It will"
            - 5 to 7 words total

        "It will open your" contributes four words.

        Therefore we append exactly 1-3 target words.
        """

        target = (
            getattr(
                extracted_action,
                "target_screen",
                None,
            )
            or getattr(
                extracted_action,
                "action_name",
                None,
            )
            or "device settings"
        )

        # Collapse unusual whitespace before counting.
        target = " ".join(
            str(
                target
            ).split()
        )

        target_words = (
            target.split()[:3]
        )

        if not target_words:

            target_words = [
                "device",
                "settings",
            ]

        description = (
            "It will open your "
            + " ".join(
                target_words
            )
        )

        # ------------------------------------------------------
        # Defensive contract enforcement
        # ------------------------------------------------------
        #
        # This should normally be unnecessary because:
        #
        #     4 prefix words + 1-3 target words = 5-7
        #
        # Keeping the check here prevents future edits from
        # silently producing an invalid cacheable plan.
        # ------------------------------------------------------

        word_count = len(
            re.findall(
                r"[A-Za-z0-9']+",
                description,
            )
        )

        if not (
            5
            <= word_count
            <= 7
        ):

            # Guaranteed six-word fallback.
            description = (
                "It will open your device settings"
            )

        return description

    # ==========================================================
    # Metadata
    # ==========================================================

    def _meta(
        self,
    ) -> PipelineMeta:
        """Return current pipeline metadata."""

        return PipelineMeta(
            model=(
                self._enricher.model
            ),
            cost_usd=0.0,
            prompt_tokens=0,
            completion_tokens=0,
        )
