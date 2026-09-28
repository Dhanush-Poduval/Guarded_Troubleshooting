"""TEMPORARY stand-in for the Phase 0-2 pipeline.

DELETE THIS PACKAGE once the real query enrichment, structure extraction and deeplink
sequencing pipeline exists. It is isolated behind PipelinePort precisely so that deletion
is a one-line change in app.service and nothing else.

What it is for: the semantic cache cannot be built or tested without something to call on
a miss. This produces a schema-valid, rule-compliant plan whose deeplinks come from the
real catalog via the retrieval layer, so the validation gate and the cache write path are
exercised honestly rather than against hand-written constants.

What it is NOT: it does no language understanding. It selects catalog screens by
retrieval and fills a fixed template. Plan quality from this mock says nothing about the
quality of the real pipeline, and no accuracy figure should ever be quoted from it.

It deliberately simulates latency, because a cache whose miss path returns instantly does
not demonstrate anything about cache value.
"""

from __future__ import annotations

import asyncio
import logging
import re

import psycopg

from app.config import Settings, get_settings
from app.contract.schema import (
    Action,
    Condition,
    ContextDeeplinkResponse,
    Deeplink,
    Goal,
    ResultTypes,
    StepGroup,
    ValidationDeepLink,
    actionCategory,
)
from app.embeddings.encoder import Encoder
from app.pipeline.port import PipelineMeta, PipelineRequest, PipelineResult
from app.retrieval.resolver import BM25Index, search_catalog

logger = logging.getLogger(__name__)

# Keyword to topic, used only to produce a plausible <Topic> for the goal sentence.
_TOPIC_KEYWORDS = (
    ("batter", "Battery"),
    ("charg", "Battery"),
    ("power", "Battery"),
    ("screen", "Display"),
    ("display", "Display"),
    ("bright", "Display"),
    ("swipe", "Swipe Navigation"),
    ("gestur", "Swipe Navigation"),
    ("navigat", "Swipe Navigation"),
    ("photo", "Camera"),
    ("camera", "Camera"),
    ("video", "Camera"),
    ("picture", "Camera"),
    ("slow", "Performance"),
    ("lag", "Performance"),
    ("storage", "Performance"),
    ("memory", "Performance"),
    ("app", "Performance"),
)

# Templates used to synthesise paraphrases. The real Phase 0 is expected to generate these
# across varied registers; these are structural stand-ins only.
_VARIATION_TEMPLATES = (
    "{q}",
    "Why does {q_lower}",
    "How do I fix {q_lower}",
    "{q_lower} and I cannot work out why",
    "Help, {q_lower}",
    "Is there a way to stop {q_lower}",
    "{q_lower} after a recent update",
    "My phone has an issue where {q_lower}",
    "What should I do about {q_lower}",
    "{q_lower} - this is really annoying",
)

_ARTICLE = re.compile(r"^(my|the|a|an)\s+", re.IGNORECASE)


def _topic_for(query: str) -> str:
    lowered = query.lower()
    for keyword, topic in _TOPIC_KEYWORDS:
        if keyword in lowered:
            return topic
    return "Device"


def _canonicalise(query: str) -> str:
    """A crude normalisation standing in for Phase 0 query enrichment."""
    text = " ".join(query.lower().split())
    return text.rstrip(" .?!")


def _paraphrases(query: str) -> list[str]:
    stem = _ARTICLE.sub("", query.strip().rstrip(".?!"))
    stem_lower = stem[0].lower() + stem[1:] if stem else stem
    seen: list[str] = []
    for template in _VARIATION_TEMPLATES:
        candidate = template.format(q=query.rstrip(".?!"), q_lower=stem_lower).strip()
        if candidate and candidate not in seen:
            seen.append(candidate)
    return seen[:10]


def _title_case(text: str) -> str:
    small = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or",
             "the", "to", "with"}
    out = []
    for index, token in enumerate(text.split()):
        if index > 0 and token.lower() in small:
            out.append(token.lower())
        else:
            out.append(token[:1].upper() + token[1:])
    return " ".join(out)


# The official catalog's originalType values are onURL, offURL, onClickURL, updateURL
# and placeholder. None of them encodes whether an action is disruptive, so category has
# to be inferred from what the action actually does. Physical interventions and
# destructive operations are recognised by their language.
_MANUAL_MARKERS = (
    "service center", "service centre", "contact samsung", "contact support",
    "authorized", "authorised", "technician", "repair", "replace the",
    "physically", "clean the", "visit",
)
_CRITICAL_MARKERS = (
    "factory reset", "factory data reset", "reset all", "erase all",
    "restart", "reboot", "safe mode", "firmware", "software update",
    "format", "wipe",
)


def _classify(*texts: str) -> "actionCategory":
    blob = " ".join(t.lower() for t in texts if t)
    if any(marker in blob for marker in _MANUAL_MARKERS):
        return actionCategory.manual
    if any(marker in blob for marker in _CRITICAL_MARKERS):
        return actionCategory.critical
    return actionCategory.auto


def _action_name(description: str, message: str) -> str:
    """Title Case, naming one screen. The catalog message is the shorter, more
    screen-like of the two fields, so it is preferred."""
    text = (message or description).strip().rstrip(".")
    for prefix in ("Opens the ", "Opens ", "Open the ", "Open ", "Enables ", "Enable ",
                   "Configures ", "Configure ", "Switches ", "Switch ", "Sets ", "Set "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return _title_case(text)


# Words that read badly as the last word of a truncated phrase.
_DANGLING = frozenset(
    {"the", "a", "an", "and", "or", "of", "to", "for", "from", "with", "in", "on", "by"}
)


def _describe(description: str, max_words: int = 7) -> str:
    """Build the "It will ..." description structurally rather than hoping for compliance.

    Capped at 7 body-inclusive words: that satisfies the written rule and also sits inside
    the wider bound the official sample implies, so output is valid either way.
    """
    body = [w.strip(".,") for w in description.split() if w.strip(".,")]
    if body:
        # Lowercase the first body word so the phrase reads as one sentence, leaving
        # acronyms alone.
        if not body[0].isupper():
            body[0] = body[0][0].lower() + body[0][1:]
    # "It" and "will" occupy two of the five to seven words.
    body = body[:5]
    # Truncation can leave a dangling article or preposition.
    while len(body) > 3 and body[-1].lower() in _DANGLING:
        body.pop()
    while len(body) < 3:
        body.append("help")
    return "It will " + " ".join(body)


class MockPipeline:
    """TEMPORARY. Satisfies PipelinePort so the cache has a miss path."""

    def __init__(
        self,
        conn_factory,
        encoder: Encoder,
        bm25_index: BM25Index,
        settings: Settings | None = None,
        *,
        simulated_latency_s: float = 0.4,
    ) -> None:
        self._conn_factory = conn_factory
        self._encoder = encoder
        self._bm25 = bm25_index
        self._settings = settings or get_settings()
        self._latency = simulated_latency_s

    @property
    def name(self) -> str:
        return "mock"

    async def run(self, request: PipelineRequest) -> PipelineResult:
        logger.warning(
            "MOCK PIPELINE INVOKED for %r. This is not a real troubleshooting plan.",
            request.query,
        )
        # Stands in for LLM round-trip time so cache benefit is observable.
        await asyncio.sleep(self._latency)

        matches = await asyncio.to_thread(self._retrieve, request.query)

        if not matches:
            return PipelineResult(
                canonical_query=_canonicalise(request.query),
                query_variations=_paraphrases(request.query),
                response=ContextDeeplinkResponse(contexts=[]),
                meta=PipelineMeta(model=self.name, cost_usd=0.0),
                fallback="no_match",
            )

        topic = _topic_for(request.query)
        actions: list[Action] = []

        for match in matches[:3]:
            category = _classify(match.description, match.message)
            is_manual = category == actionCategory.manual

            steps = [
                "Navigate to and open Settings.",
                f"{match.description.rstrip('.')}.",
                f"{(match.message or 'Review the available options').rstrip('.')}.",
            ]

            # A manual action is a physical intervention and must not carry a deeplink.
            actionable = (
                None
                if is_manual
                else Deeplink(
                    deeplink=match.deeplink,
                    description=match.description,
                    message=match.message or "",
                    originalType=match.original_type,
                )
            )

            # Only 138 of 578 catalog entries carry the comparison needed for a complete
            # ValidationDeepLink, so it is emitted only where the catalog supports it
            # rather than being invented.
            validation = None
            if not is_manual and match.validation_deeplink and match.validation_key:
                validation = ValidationDeepLink(
                    deeplink=match.validation_deeplink,
                    key=match.validation_key,
                    resultType=(
                        ResultTypes(match.validation_result_type)
                        if match.validation_result_type
                        else None
                    ),
                    condition=(
                        Condition(match.validation_condition)
                        if match.validation_condition
                        else None
                    ),
                    value=match.validation_value,
                )

            actions.append(
                Action(
                    actionName=_action_name(match.description, match.message),
                    description=_describe(match.message or match.description),
                    stepGroups=[
                        StepGroup(
                            steps=steps,
                            actionableDeeplink=actionable,
                            validationDeeplink=validation,
                        )
                    ],
                    category=category,
                )
            )

        # Destructive operations last, stable within each group so output is repeatable.
        order = {actionCategory.auto: 0, actionCategory.manual: 1, actionCategory.critical: 2}
        actions.sort(key=lambda a: order[a.category])

        goal = Goal(
            goal=f"Follow these steps to perform this {topic} Troubleshooting",
            title=_title_for(topic),
            actions=actions,
            score=round(min(max(matches[0].cosine_similarity or 0.5, 0.0), 1.0), 2),
        )

        return PipelineResult(
            canonical_query=_canonicalise(request.query),
            query_variations=_paraphrases(request.query),
            response=ContextDeeplinkResponse(contexts=[goal]),
            meta=PipelineMeta(model=self.name, cost_usd=0.0),
        )

    def _retrieve(self, query: str):
        with self._conn_factory() as conn:
            return search_catalog(
                conn,
                query,
                top_k=3,
                bm25_index=self._bm25,
                encoder=self._encoder,
                settings=self._settings,
            )


def _title_for(topic: str) -> str:
    """Two to three words, sentence case."""
    mapping = {
        "Battery": "Battery fast drain",
        "Display": "Display settings",
        "Swipe Navigation": "Swipe navigation settings",
        "Camera": "Camera settings",
        "Performance": "Device performance",
        "Device": "Device settings",
    }
    return mapping.get(topic, "Device settings")


def connection_factory(settings: Settings):
    """Returns a callable opening a short-lived sync connection for the mock's retrieval.

    The mock runs retrieval on a worker thread, so it cannot share the request's async
    connection.
    """
    from pgvector.psycopg import register_vector

    def _factory():
        conn = psycopg.connect(settings.database_url)
        register_vector(conn)
        return conn

    return _factory
