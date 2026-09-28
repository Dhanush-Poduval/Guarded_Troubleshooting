"""Loads the deeplink catalog from JSON.

The catalog is the authority on which deeplinks exist. Nothing may invent a URI, so the
loader is also where the provenance of the data is established.

Official format:

    {"_readme": "...", "count": 578, "deeplinks": [
        {"id", "deeplink", "description", "message", "originalType",
         "control_type", "qna_description", "validation": {...} | null}, ...]}

The catalog's own readme instructs matching on description, message, qna_description and
originalType, then copying the URI verbatim.
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

# originalType is a technical token, not prose, so embedding it raw would add noise. It
# does carry real intent though: offURL and onURL distinguish turning a setting off from
# turning it on, which is exactly the distinction a step like "turn off adaptive
# brightness" depends on. Each token is therefore expanded into the words a user would
# actually say.
_ORIGINAL_TYPE_HINTS = {
    "onURL": "turn on enable activate",
    "offURL": "turn off disable deactivate",
    "onClickURL": "open view screen settings",
    "updateURL": "change update set adjust",
    "placeholder": "",
}


@dataclass(frozen=True)
class CatalogValidation:
    """The validation deeplink attached to a catalog entry.

    Most entries carry only a deeplink and a key; a subset also carries the comparison
    needed to assert a toggle is on.
    """

    deeplink: str
    key: str
    result_type: str | None = None
    condition: str | None = None
    value: str | None = None

    @property
    def is_complete(self) -> bool:
        """True when there is enough detail to emit a full ValidationDeepLink."""
        return self.result_type is not None and self.condition is not None


@dataclass(frozen=True)
class CatalogEntry:
    deeplink: str
    description: str
    message: str
    qna_description: str
    original_type: str | None
    catalog_id: str | None = None
    control_type: int | None = None
    validation: CatalogValidation | None = None

    @property
    def match_text(self) -> str:
        """The text that is embedded and keyword-indexed.

        The masked URI is excluded deliberately: it is an obfuscated token, so matching
        against it is meaningless. originalType is included as an expanded hint rather
        than as its raw token.
        """
        hint = _ORIGINAL_TYPE_HINTS.get(self.original_type or "", "")
        parts = [self.description, self.message, self.qna_description, hint]
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


def _parse_validation(raw: dict | None) -> CatalogValidation | None:
    if not raw or not raw.get("deeplink") or not raw.get("key"):
        return None
    return CatalogValidation(
        deeplink=raw["deeplink"],
        key=raw["key"],
        result_type=raw.get("resultType"),
        condition=raw.get("condition"),
        value=None if raw.get("value") is None else str(raw["value"]),
    )


def load_catalog(
    path: str | pathlib.Path | None = None,
    settings: Settings | None = None,
) -> Catalog:
    settings = settings or get_settings()
    catalog_path = pathlib.Path(path or settings.catalog_path)

    if not catalog_path.exists():
        raise FileNotFoundError(
            f"Catalog not found at {catalog_path}. Set CATALOG_PATH to the official "
            f"deeplinks.json."
        )

    raw = json.loads(catalog_path.read_text(encoding="utf-8"))

    if isinstance(raw, list):
        records, synthetic = raw, False
    else:
        records = raw.get("deeplinks", [])
        # Only fixture data carries this marker; the official catalog does not.
        synthetic = bool(raw.get("_synthetic", False))
        declared = raw.get("count")
        if declared is not None and declared != len(records):
            logger.warning(
                "catalog declares count=%s but contains %d entries",
                declared, len(records),
            )

    entries = tuple(
        CatalogEntry(
            deeplink=record["deeplink"],
            description=record.get("description", ""),
            message=record.get("message", "") or "",
            qna_description=record.get("qna_description", "") or "",
            original_type=record.get("originalType"),
            catalog_id=record.get("id"),
            control_type=record.get("control_type"),
            validation=_parse_validation(record.get("validation")),
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
        logger.warning(
            "SYNTHETIC CATALOG IN USE: %s (%d entries). These deeplinks are not real.",
            catalog_path, len(entries),
        )
    else:
        with_validation = sum(1 for e in entries if e.validation)
        complete = sum(1 for e in entries if e.validation and e.validation.is_complete)
        logger.info(
            "Loaded official catalog: %s (%d entries, %d with a validation deeplink, "
            "%d of those complete)",
            catalog_path, len(entries), with_validation, complete,
        )

    return Catalog(entries=entries, source=source, path=catalog_path)
