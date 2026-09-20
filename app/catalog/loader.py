"""Loads the deeplink catalog from JSON.

The catalog is the authority on which deeplinks exist. Nothing may invent a URI, so the
loader is also where synthetic fixture data is detected and flagged: a plan built against
fixtures is not interchangeable with one built against the real catalog.
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass
from typing import Literal

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

CatalogSource = Literal["synthetic", "official"]


@dataclass(frozen=True)
class CatalogEntry:
    deeplink: str
    description: str
    message: str
    qna_description: str
    original_type: str | None

    @property
    def match_text(self) -> str:
        """The text that gets embedded and keyword-indexed.

        Built only from descriptive metadata. The masked URI is excluded deliberately:
        it is an obfuscated token and matching against it is meaningless.
        """
        parts = [self.description, self.message, self.qna_description]
        return " ".join(part.strip() for part in parts if part and part.strip())


@dataclass(frozen=True)
class Catalog:
    entries: tuple[CatalogEntry, ...]
    source: CatalogSource
    path: pathlib.Path

    @property
    def is_synthetic(self) -> bool:
        return self.source == "synthetic"

    def deeplink_set(self) -> frozenset[str]:
        """Every URI the system is permitted to emit."""
        return frozenset(entry.deeplink for entry in self.entries)


def load_catalog(
    path: str | pathlib.Path | None = None,
    settings: Settings | None = None,
) -> Catalog:
    settings = settings or get_settings()
    catalog_path = pathlib.Path(path or settings.catalog_path)

    if not catalog_path.exists():
        raise FileNotFoundError(
            f"Catalog not found at {catalog_path}. Set CATALOG_PATH to the real "
            f"deeplinks.json, or generate fixtures with "
            f"`python data/fixtures_synthetic/generate_fixtures.py`."
        )

    raw = json.loads(catalog_path.read_text(encoding="utf-8"))

    # The official file may be a bare list; fixtures are an object carrying the marker.
    if isinstance(raw, list):
        records, synthetic = raw, False
    else:
        records = raw.get("deeplinks", [])
        synthetic = bool(raw.get("_synthetic", False))

    entries = tuple(
        CatalogEntry(
            deeplink=record["deeplink"],
            description=record.get("description", ""),
            message=record.get("message", "") or "",
            qna_description=record.get("qna_description", "") or "",
            original_type=record.get("originalType"),
        )
        for record in records
    )

    if not entries:
        raise ValueError(f"Catalog at {catalog_path} contains no deeplinks.")

    duplicates = len(entries) - len({entry.deeplink for entry in entries})
    if duplicates:
        raise ValueError(f"Catalog at {catalog_path} has {duplicates} duplicate URIs.")

    source: CatalogSource = "synthetic" if synthetic else "official"
    if synthetic:
        # Loud on every load. A demo must never quietly run on fixtures.
        logger.warning(
            "SYNTHETIC CATALOG IN USE: %s (%d entries). These deeplinks are not real. "
            "Replace with the official catalog and run scripts.reset_catalog before "
            "relying on any result.",
            catalog_path,
            len(entries),
        )
    else:
        logger.info("Loaded official catalog: %s (%d entries)", catalog_path, len(entries))

    return Catalog(entries=entries, source=source, path=catalog_path)
