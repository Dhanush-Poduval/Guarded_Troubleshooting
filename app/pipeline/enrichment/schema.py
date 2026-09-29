"""Internal schemas used by the query-enrichment stage."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class EnrichedQuery(BaseModel):
    """Normalized representation of a raw customer complaint.

    canonical_query:
        Concise technical representation of the user's actual problem.

    topic:
        High-level troubleshooting domain such as Battery, Display,
        Camera or Performance.

    query_variations:
        8-10 semantically equivalent ways the same complaint may be
        expressed. These are later useful for semantic-cache matching.
    """

    canonical_query: str = Field(min_length=3)
    topic: str = Field(min_length=2)

    query_variations: list[str] = Field(
        min_length=8,
        max_length=10,
    )

    @field_validator("canonical_query", "topic")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = " ".join(value.split()).strip()

        if not value:
            raise ValueError("value cannot be empty")

        return value

    @field_validator("query_variations")
    @classmethod
    def validate_variations(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()

        for value in values:
            value = " ".join(value.split()).strip()

            if not value:
                raise ValueError("query variation cannot be empty")

            key = value.casefold()

            if key in seen:
                raise ValueError(
                    "query variations must be semantically distinct strings"
                )

            seen.add(key)
            cleaned.append(value)

        if not 8 <= len(cleaned) <= 10:
            raise ValueError(
                "query_variations must contain between 8 and 10 items"
            )

        return cleaned
