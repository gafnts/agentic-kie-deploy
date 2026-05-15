"""
Pydantic models defining the canonical NDA extraction schema.
"""

import re
from datetime import date

from pydantic import BaseModel, Field, field_validator


class Party(BaseModel):
    name: str = Field(..., description="Name of one party involved in the contract.")

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, v: str) -> str:
        # Normalize unicode curly quotes to ASCII apostrophes/quotes
        v = v.replace("\u2018", "'").replace("\u2019", "'")
        v = v.replace("\u201c", '"').replace("\u201d", '"')
        # Strip commas (e.g., "Inc.," → "Inc.")
        v = v.replace(",", "")
        # Replace spaces and colons with underscores
        v = v.replace(" ", "_").replace(":", "_")
        return v


class NDA(BaseModel):
    """
    Extract key information from a Non-Disclosure Agreement (NDA).

    General formatting rules:
    * In all attribute values, replace spaces ` ` and colons `:` with underscores `_`.
    * If an attribute is not present or cannot be determined from the document, return `null`.
    """

    effective_date: str | None = Field(
        None,
        description=(
            "Date in `YYYY-MM-DD` format at which point the contract becomes legally "
            "binding. Use the explicitly stated effective date; if none is stated, use "
            "the latest signature date (i.e., the date on which the last party signed, "
            "making the agreement fully executed). Do NOT use individual signature dates "
            "when an explicit effective date is stated elsewhere in the agreement text."
        ),
    )
    jurisdiction: str | None = Field(
        None,
        description=(
            "The state or country under whose laws the contract is governed. "
            "Return only the name (e.g., `New_York`, `Delaware`, `Florida`). "
            "Do NOT include prefixes such as `State_of_` or `Commonwealth_of_`."
        ),
    )
    party: list[Party] = Field(
        default_factory=list,
        description=(
            "Named parties to the contract. Use the short legal entity name as written "
            "in the agreement (e.g., `Nike_Inc.` not "
            "`NIKE_Inc._divisions_subsidiaries_and_affiliates`). Do NOT include "
            "parenthetical descriptions (e.g., drop `_(An_Electric_Membership_Corporation)`), "
            "subsidiary clauses, or affiliate lists. Do NOT use role labels such as "
            "`Recipient`, `Disclosing_Party`, `Company`, or `Employee` as party names. "
            "If a party's actual name is not stated, omit that party."
        ),
    )
    term: str | None = Field(
        None,
        description=(
            "Overall fixed duration of the agreement itself (e.g., `2_years`). "
            "Extract ONLY the agreement-level term. Do NOT extract durations of "
            "individual obligations such as confidentiality survival periods, "
            "non-compete restricted periods, or non-solicitation periods. "
            "If the agreement is terminable at will, on notice, or tied to the "
            "duration of employment with no independent fixed end date, return `null`. "
            "Normalize with original units: e.g., `eleven months` → `11_months`. "
            "Format: `{number}_{units}`."
        ),
    )

    @field_validator("effective_date", mode="before")
    @classmethod
    def validate_effective_date(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError(
                f"effective_date must be in YYYY-MM-DD format, got '{v}'"
            ) from None
        return v

    @field_validator("jurisdiction", "term", mode="before")
    @classmethod
    def normalize_underscores(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return v.replace(" ", "_").replace(":", "_")

    @field_validator("jurisdiction", mode="after")
    @classmethod
    def strip_jurisdiction_prefix(cls, v: str | None) -> str | None:
        """
        Deterministically strip "State_of_" / "Commonwealth_of_" prefixes
        that models sometimes prepend despite prompt instructions.
        """
        if v is None:
            return v
        for prefix in ("State_of_", "Commonwealth_of_"):
            if v.startswith(prefix):
                return v[len(prefix) :]
        return v

    @field_validator("term", mode="after")
    @classmethod
    def validate_term_format(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not re.fullmatch(r"\d+(?:\.\d+)?_\w+", v):
            raise ValueError(
                f"term must be in '{{number}}_{{units}}' format, got '{v}'"
            ) from None
        return v
