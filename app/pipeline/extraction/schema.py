"""Internal schemas for grounded troubleshooting extraction."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class ExtractedCategory(str, Enum):
    auto = "auto"
    manual = "manual"
    critical = "critical"


class ExtractedAction(BaseModel):
    """One troubleshooting action extracted from SIIS text."""

    action_name: str = Field(min_length=2)

    # Description of the actual Settings screen or device feature that
    # Phase 2 should search for in the deeplink catalog.
    target_screen: str | None = None

    steps: list[str] = Field(min_length=1)

    category: ExtractedCategory

    # Exact supporting passage from SIIS. This is internal provenance
    # and is not returned by the public REST API.
    source_text: str = Field(min_length=1)

    @field_validator("action_name")
    @classmethod
    def clean_action_name(cls, value: str) -> str:
        value = " ".join(value.split()).strip()

        if not value:
            raise ValueError("action_name cannot be empty")

        return value

    @field_validator("target_screen")
    @classmethod
    def clean_target_screen(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None

        value = " ".join(value.split()).strip()

        return value or None

    @field_validator("steps")
    @classmethod
    def clean_steps(cls, values: list[str]) -> list[str]:
        result: list[str] = []

        for value in values:
            value = " ".join(value.split()).strip()

            if not value:
                raise ValueError("steps cannot contain empty strings")

            result.append(value)

        return result


class ExtractedPlan(BaseModel):
    """Grounded intermediate troubleshooting plan."""

    topic: str = Field(min_length=2)

    title: str = Field(min_length=2)

    actions: list[ExtractedAction] = Field(min_length=1)

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )
