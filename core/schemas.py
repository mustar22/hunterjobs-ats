"""
schemas.py

Pydantic models enforced as Gemma 4 response_schema. Keep enums tight
enough to be useful, loose enough that Gemma doesn't reject-fail on edge wording.
"""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class JobFilter(BaseModel):
    verdict: Literal["GOOD", "MAYBE", "BAD"]
    reject_reason: str = Field(
        default="",
        description="Brief reason for BAD verdict, or empty string for GOOD/MAYBE.",
    )


class CompanyResearch(BaseModel):
    company_summary: str = Field(
        default="",
        description="2-3 sentences max. What they do, stage, notable facts only.",
    )
    hiring_signal: Literal["looks_real", "ghost", "uncertain"] = "uncertain"
    real_stack: list[str] = Field(
        default_factory=list,
        description="Actual tech stack found in the content. Empty list if nothing found.",
    )
    culture_flags: list[str] = Field(
        default_factory=list,
        description="Red flags only. Empty list if none.",
    )
    company_size: Literal["tiny", "mid", "enterprise"] = "tiny"


class WebContact(BaseModel):
    name: str = ""
    title: str = ""


class WebContacts(BaseModel):
    """Stage 3 web-snippet extraction: real people named in search results only."""
    contacts: list[WebContact] = Field(default_factory=list)


class ContactFind(BaseModel):
    name: str = "Founder"
    title: str = "Founder"
    email: str = ""
    email_confidence: Literal["verified", "pattern", "unconfirmed"] = "pattern"
    email_source: Literal["github", "permutation", "hunter"] = "permutation"
    outreach_draft: str = Field(
        default="",
        description="Max 4 sentences. Technical, direct, no fluff.",
    )
