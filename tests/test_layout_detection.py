"""Unit tests for portal layout detection, closed-world abstention, fuzzy QA cache, and natural prose."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from compass_mcp.adapters import (
    AshbyAdapter,
    GenericPortalAdapter,
    GreenhouseAdapter,
    LeverAdapter,
    PersonioAdapter,
    detect_portal_adapter,
)
from compass_mcp.browser.pacing import PacingController
from compass_mcp.config_loader import QACacheManager, load_user_profile, normalize_prompt_key
from compass_mcp.llm.assistant import (
    ABSTENTION_REASONING,
    BANNED_CLICHES,
    OllamaAssistant,
    sanitize_natural_engineer_prose,
)
from compass_mcp.models import FormControl


def test_url_portal_layout_detection() -> None:
    """Verify URL pattern detection for Greenhouse (classic + modern), Lever, Ashby, Personio, and Generic."""
    gh_classic = asyncio.run(detect_portal_adapter("https://boards.greenhouse.io/acme/jobs/12345"))
    assert isinstance(gh_classic, GreenhouseAdapter)
    assert gh_classic.portal_type == "greenhouse"

    gh_modern = asyncio.run(detect_portal_adapter("https://job-boards.greenhouse.io/acme/jobs/98765"))
    assert isinstance(gh_modern, GreenhouseAdapter)
    assert gh_modern.portal_type == "greenhouse"

    lever = asyncio.run(detect_portal_adapter("https://jobs.lever.co/acme/0f123456-7890-abcd"))
    assert isinstance(lever, LeverAdapter)
    assert lever.portal_type == "lever"

    ashby = asyncio.run(detect_portal_adapter("https://jobs.ashbyhq.com/acme/11223344-5566"))
    assert isinstance(ashby, AshbyAdapter)
    assert ashby.portal_type == "ashby"

    personio_de = asyncio.run(detect_portal_adapter("https://acme.jobs.personio.de/job/123456"))
    assert isinstance(personio_de, PersonioAdapter)
    assert personio_de.portal_type == "personio"

    personio_com = asyncio.run(detect_portal_adapter("https://acme.jobs.personio.com/job/654321"))
    assert isinstance(personio_com, PersonioAdapter)

    generic = asyncio.run(detect_portal_adapter("https://careers.example.org/apply/software-engineer"))
    assert isinstance(generic, GenericPortalAdapter)
    assert generic.portal_type == "generic"


def test_exact_profile_yaml_loads_and_validates() -> None:
    """Ensure Ramana Gangarao's exact config/profile.yaml loads cleanly without dummy values."""
    profile = load_user_profile()
    assert profile.personal.first_name == "Ramana"
    assert profile.personal.last_name == "Gangarao"
    assert profile.personal.full_name == "Ramana Gangarao"
    assert profile.personal.email == "gangaraoramana0404@gmail.com"
    assert profile.personal.phone == "8555021702"
    assert profile.links.linkedin == "https://www.linkedin.com/in/ramana-gangarao"
    assert profile.links.github == "https://github.com/RamanaGR"
    assert profile.work_authorization.authorized_india is True
    assert profile.work_authorization.authorized_eu is True
    assert profile.work_authorization.authorized_us is False
    assert profile.preferences.notice_period == "30 days"
    assert profile.preferences.availability_date == ""
    assert profile.preferences.salary_expectations == "85000"
    assert profile.experience.years_of_experience == 8
    assert len(profile.education) == 2
    assert len(profile.languages) == 3


def test_fuzzy_qa_cache_lookup(tmp_path: Path) -> None:
    """Verify exact and SequenceMatcher >= 0.88 fuzzy lookup in QACacheManager."""
    cache_file = tmp_path / "qa_cache.json"
    cache = QACacheManager(cache_path=cache_file)

    cache.store(
        prompt="What is your notice period?",
        value="30 days",
        control_type="text",
        confidence=1.0,
    )

    # Exact match
    exact = cache.lookup("What is your notice period?")
    assert exact is not None
    assert exact.value == "30 days"

    # Fuzzy match ("Notice period" vs "What is your notice period?")
    fuzzy = cache.lookup("Notice period *")
    assert fuzzy is not None
    assert fuzzy.value == "30 days"
    assert fuzzy.confidence >= 0.88

    # Unrelated prompt must miss
    miss = cache.lookup("What is your expected annual base salary in EUR?")
    assert miss is None


def test_closed_world_abstention_on_missing_or_unverified_facts() -> None:
    """Ensure OllamaAssistant abstains (confidence=0.0, value='') when facts are missing in profile.yaml."""
    profile = load_user_profile()
    profile.preferences.notice_period = ""
    profile.preferences.availability_date = ""
    profile.preferences.salary_expectations = ""
    profile.personal.phone = ""
    assistant = OllamaAssistant()

    # 1. Notice period when empty in profile.yaml
    notice_ctrl = FormControl(
        selector="#notice",
        label="What is your notice period?",
        control_type="text",
        required=True,
    )
    notice_res = assistant.check_closed_world_abstention(notice_ctrl, profile)
    assert notice_res is not None
    assert notice_res.confidence == 0.0
    assert notice_res.value == ""
    assert notice_res.reasoning == ABSTENTION_REASONING

    # 2. Salary expectation when empty in profile.yaml
    salary_ctrl = FormControl(
        selector="#salary",
        label="Salary expectations (EUR gross/year)",
        control_type="text",
        required=True,
    )
    salary_res = assistant.check_closed_world_abstention(salary_ctrl, profile)
    assert salary_res is not None
    assert salary_res.confidence == 0.0
    assert salary_res.value == ""

    # 3. Phone when empty in profile.yaml
    phone_ctrl = FormControl(
        selector="#phone",
        label="Mobile Phone Number",
        control_type="text",
        required=True,
    )
    phone_res = assistant.check_closed_world_abstention(phone_ctrl, profile)
    assert phone_res is not None
    assert phone_res.confidence == 0.0

    # 4. Unverified technology not in profile.yaml (e.g. Rust / COBOL kernel drivers)
    unverified_skill_ctrl = FormControl(
        selector="#skill_cobol",
        label="Do you have production experience with COBOL mainframe systems?",
        control_type="select",
        options=["Yes", "No"],
    )
    skill_res = assistant.check_closed_world_abstention(unverified_skill_ctrl, profile)
    assert skill_res is not None
    assert skill_res.confidence == 0.0
    assert skill_res.value == ""


def test_deterministic_structured_grounding_from_profile() -> None:
    """Verify documented work authorization and language facts map with high confidence."""
    profile = load_user_profile()
    assistant = OllamaAssistant()

    eu_sponsorship_ctrl = FormControl(
        selector="#eu_visa",
        label="Will you require visa sponsorship to work in Germany / the EU?",
        control_type="select",
        options=["Yes", "No"],
    )
    res = assistant.deterministic_structured_match(eu_sponsorship_ctrl, profile)
    assert res is not None
    assert res.value == ("Yes" if profile.work_authorization.requires_eu_visa_sponsorship else "No")
    assert res.confidence >= 0.95

    german_ctrl = FormControl(
        selector="#german_level",
        label="What is your German language proficiency?",
        control_type="select",
        options=["Fluent / C1", "Intermediate / B1", "Basic / A1", "None"],
    )
    lang_res = assistant.deterministic_structured_match(german_ctrl, profile)
    assert lang_res is not None
    assert lang_res.value == "Basic / A1"


def test_natural_engineer_prose_sanitizer() -> None:
    """Verify Mode 2 prose post-processor removes banned cliches, em-dashes, and semicolons."""
    raw = (
        "I am thrilled and passionate to delve into this cutting-edge role — "
        "I have spent the last eight years building production FastAPI microservices; "
        "moreover, I designed a 9-agent Google ADK pipeline at Innovapath for Optum."
    )
    sanitized = sanitize_natural_engineer_prose(raw)
    assert "—" not in sanitized
    assert ";" not in sanitized
    for cliche in BANNED_CLICHES:
        assert cliche not in sanitized.lower().split()


def test_pacing_controller_session_floor_window() -> None:
    """Verify PacingController initializes a target session floor in the 22.0s–32.0s window."""
    pacer = PacingController(min_session_seconds=25.0, enabled=True)
    assert 22.0 <= pacer.target_session_seconds <= 32.0
    assert pacer.remaining_floor_seconds > 0.0
    assert normalize_prompt_key("What is your notice period? *") == "what is your notice period"
