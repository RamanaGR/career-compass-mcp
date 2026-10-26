"""Pydantic v2 data models for Career Compass MCP."""

from __future__ import annotations

import re
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PortalType = Literal["greenhouse", "lever", "ashby", "personio", "generic"]
ControlType = Literal[
    "text",
    "textarea",
    "select",
    "radio",
    "checkbox",
    "combobox",
    "file",
    "numeric",
    "date",
]
HighlightColor = Literal["blue", "green", "amber", "none"]

_MARKDOWN_LINK_RE = re.compile(r"^\[(?P<text>[^\]]+)\]\((?P<url>[^)]+)\)$")


def _clean_markdown_url(value: Any) -> str:
    """Clean markdown-wrapped URLs like `[https://...](https://...)` into plain URLs."""
    if not isinstance(value, str):
        return ""
    stripped = value.strip()
    match = _MARKDOWN_LINK_RE.match(stripped)
    if match:
        return match.group("url").strip()
    return stripped


class PersonalInfo(BaseModel):
    """Standard user identity and contact attributes."""

    model_config = ConfigDict(extra="allow")

    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    preferred_name: str = ""
    pronouns: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    postal_code: str = ""

    @model_validator(mode="after")
    def _ensure_full_or_split_name(self) -> PersonalInfo:
        if not self.full_name and (self.first_name or self.last_name):
            self.full_name = f"{self.first_name} {self.last_name}".strip()
        elif self.full_name and not (self.first_name or self.last_name):
            parts = self.full_name.strip().split(None, 1)
            self.first_name = parts[0] if parts else ""
            self.last_name = parts[1] if len(parts) > 1 else ""
        return self


class ProfileLinks(BaseModel):
    """Portfolio, code, and professional profile URLs."""

    model_config = ConfigDict(extra="allow")

    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    website: str = ""
    other: str = ""

    @field_validator("linkedin", "github", "portfolio", "website", "other", mode="before")
    @classmethod
    def _sanitize_url(cls, value: Any) -> str:
        return _clean_markdown_url(value)


class ProfileDocuments(BaseModel):
    """Local file paths for PDF attachments."""

    model_config = ConfigDict(extra="allow")

    resume_path: str = "assets/resume.pdf"
    cover_letter_path: str = "assets/cover_letter.pdf"


class WorkAuthorization(BaseModel):
    """Work eligibility, sponsorship, and relocation attributes."""

    model_config = ConfigDict(extra="allow")

    current_location: str = ""
    authorized_india: bool = True
    authorized_eu: bool = False
    authorized_us: bool = False
    authorized_uk: bool = False
    requires_eu_visa_sponsorship: bool = True
    requires_us_visa_sponsorship: bool = True
    willing_to_relocate_eu_germany: bool = True
    visa_status: str = ""
    security_clearance: str = ""

    @property
    def authorized_to_work_us(self) -> bool:
        return self.authorized_us

    @property
    def authorized_to_work_eu(self) -> bool:
        return self.authorized_eu

    @property
    def requires_visa_sponsorship(self) -> bool:
        return self.requires_us_visa_sponsorship or self.requires_eu_visa_sponsorship


class RolePreferences(BaseModel):
    """Compensation, notice period, target roles, and location preferences."""

    model_config = ConfigDict(extra="allow")

    target_roles: list[str] = Field(default_factory=list)
    remote_preference: str = ""
    relocation: str = ""
    notice_period: str = ""
    availability_date: str = ""
    salary_expectations: str = ""


class EducationEntry(BaseModel):
    """Academic degree entry from profile.yaml."""

    model_config = ConfigDict(extra="allow")

    degree: str = ""
    institution: str = ""
    location: str = ""
    graduation_date: str = ""


class LanguageEntry(BaseModel):
    """Spoken/written language proficiency entry from profile.yaml."""

    model_config = ConfigDict(extra="allow")

    language: str = ""
    proficiency: str = ""


class ExperienceHighlight(BaseModel):
    """Documented role and project achievement in profile.yaml."""

    model_config = ConfigDict(extra="allow")

    role: str = ""
    company: str = ""
    location: str = ""
    period: str = ""
    details: str = ""


class ExperienceSummary(BaseModel):
    """Engineering background, skills, and project highlights for closed-world LLM grounding."""

    model_config = ConfigDict(extra="allow")

    years_of_experience: int | float = 0
    current_title: str = ""
    current_company: str = ""
    summary: str = ""
    core_skills: list[str] = Field(default_factory=list)
    highlights: list[ExperienceHighlight | str] = Field(default_factory=list)


class UserProfile(BaseModel):
    """Top-level local YAML profile schema."""

    model_config = ConfigDict(extra="allow")

    personal: PersonalInfo = Field(default_factory=PersonalInfo)
    links: ProfileLinks = Field(default_factory=ProfileLinks)
    documents: ProfileDocuments = Field(default_factory=ProfileDocuments)
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    preferences: RolePreferences = Field(default_factory=RolePreferences)
    education: list[EducationEntry] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    languages: list[LanguageEntry] = Field(default_factory=list)
    experience: ExperienceSummary = Field(default_factory=ExperienceSummary)
    custom_facts: dict[str, Any] = Field(default_factory=dict)


class FormControl(BaseModel):
    """Accessible representation of a web form control."""

    model_config = ConfigDict(extra="ignore")

    selector: str = Field(description="Unique CSS or attribute selector for the control on the page")
    label: str = Field(description="Resolved accessible label, aria-label, or legend text")
    control_type: ControlType = Field(default="text", description="Normalized control type")
    required: bool = Field(default=False, description="Whether the control is marked required")
    current_value: str = Field(default="", description="Currently populated text or selected option")
    options: list[str] = Field(
        default_factory=list,
        description="Available option strings for select, radio, checkbox, or combobox controls",
    )
    placeholder: str = Field(default="", description="Placeholder attribute if present")
    section_heading: str = Field(default="", description="Nearest fieldset legend or section header")
    is_consent_control: bool = Field(
        default=False,
        description="True when the control asks for legal/GDPR/privacy consent and requires manual review",
    )
    highlight: HighlightColor = Field(default="none", description="Applied HITL visual highlight color")
    confidence: float | None = Field(default=None, description="Confidence score for the staged value")
    rationale: str = Field(default="", description="Brief explanation or source for the staged value")


class FieldSuggestion(BaseModel):
    """Structured output schema returned by local Ollama or QACacheManager."""

    value: str = Field(
        default="",
        description="The suggested text value or exact matching option string to populate into the control.",
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence score between 0.0 and 1.0 that the value is explicitly supported by profile.yaml.",
    )
    reasoning: str = Field(
        default="",
        description="Brief 1-sentence justification citing the exact profile.yaml fact or abstention reason.",
    )
    matched_option: str | None = Field(
        default=None,
        description="When options are provided, the exact string from control.options that was selected.",
    )
    should_cache: bool = Field(
        default=False,
        description="True if this is a reusable factual answer suitable for local QA caching.",
    )


class QACacheEntry(BaseModel):
    """Single cached prompt-to-answer mapping stored in config/qa_cache.json."""

    prompt: str
    normalized_key: str
    value: str
    control_type: ControlType = "text"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str = "user"
    updated_at: str = ""


class SessionReport(BaseModel):
    """Structured report returned by stage_form_session."""

    url: str
    portal_type: PortalType
    standard_fields_staged: list[str] = Field(default_factory=list)
    documents_attached: list[str] = Field(default_factory=list)
    llm_staged_controls: list[FormControl] = Field(default_factory=list)
    unfilled_controls: list[FormControl] = Field(default_factory=list)
    cache_hits: int = 0
    ollama_calls: int = 0
    elapsed_seconds: float = 0.0
    warnings: list[str] = Field(default_factory=list)
    browser_open: bool = True
    review_instructions: str = (
        "Visual review outlines applied inline (Blue = profile.yaml, Green = high-confidence "
        "cache/Ollama >= 0.75, Amber = manual input needed). Please review all fields and "
        "click the submit button manually in the open browser window."
    )
