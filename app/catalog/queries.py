"""Loads the canonical query set and its SIIS reference text.

The official kit ships no queries.json. Instead it provides siis_responses.json, holding
one record per query as {id, original_query, siis_response{title, content}}, and input.txt
holding the same queries as plain lines. siis_responses.json is the richer of the two and
is the default source, because pre-warming needs the reference text as well as the query:
the specification is explicit that plans must derive from the provided text rather than
from model memory.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
from dataclasses import dataclass

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Some original_query values carry a leading enumerator such as "1. " from the source
# document. It is not part of the complaint.
_LEADING_ENUMERATOR = re.compile(r'^\s*\d+\.\s*"?')
_TRAILING_QUOTE = re.compile(r'"\s*$')


@dataclass(frozen=True)
class SiisContext:
    title: str
    content: str

    def as_text(self) -> str:
        """Flatten to the single string the pipeline consumes as reference text."""
        if self.title and self.content:
            return f"{self.title}\n\n{self.content}"
        return self.content or self.title


@dataclass(frozen=True)
class QueryRecord:
    id: str
    query: str
    siis: SiisContext | None = None


def _clean(query: str) -> str:
    text = _LEADING_ENUMERATOR.sub("", query.strip())
    text = _TRAILING_QUOTE.sub("", text).strip()
    return text


def load_queries(
    path: str | pathlib.Path | None = None,
    settings: Settings | None = None,
) -> list[QueryRecord]:
    """Load the canonical queries, with SIIS text when the source provides it."""
    settings = settings or get_settings()
    query_path = pathlib.Path(path or settings.queries_path)

    if not query_path.exists():
        raise FileNotFoundError(
            f"Query set not found at {query_path}. Set QUERIES_PATH to the official "
            f"siis_responses.json."
        )

    if query_path.suffix.lower() == ".txt":
        lines = [
            line.strip()
            for line in query_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        records = [
            QueryRecord(id=f"row_{i}", query=_clean(line))
            for i, line in enumerate(lines, start=1)
        ]
        logger.info("Loaded %d queries from %s (no SIIS text)", len(records), query_path)
        return records

    raw = json.loads(query_path.read_text(encoding="utf-8"))

    # Official shape.
    if isinstance(raw, dict) and "responses" in raw:
        records = []
        for item in raw["responses"]:
            payload = item.get("siis_response") or {}
            siis = (
                SiisContext(
                    title=payload.get("title", "") or "",
                    content=payload.get("content", "") or "",
                )
                if payload
                else None
            )
            records.append(
                QueryRecord(
                    id=item.get("id", f"row_{len(records) + 1}"),
                    query=_clean(item.get("original_query", "")),
                    siis=siis,
                )
            )
        with_text = sum(1 for r in records if r.siis)
        logger.info(
            "Loaded %d queries from %s (%d with SIIS reference text)",
            len(records), query_path, with_text,
        )
        return records

    # Fixture shape, retained so a hand-written query list still loads.
    items = raw.get("queries", []) if isinstance(raw, dict) else raw
    records = [
        QueryRecord(
            id=f"row_{i}",
            query=_clean(item["query"] if isinstance(item, dict) else str(item)),
        )
        for i, item in enumerate(items, start=1)
    ]
    logger.info("Loaded %d queries from %s", len(records), query_path)
    return records
