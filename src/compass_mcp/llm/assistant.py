"""Local Ollama assistant with strict Closed-World Profile Grounding and Two-Mode routing."""

from __future__ import annotations

from difflib import SequenceMatcher
import json
import logging
import os
import re
from typing import Any

import httpx

from compass_mcp.config_loader import QACacheManager, normalize_prompt_key
from compass_mcp.models import ControlType, FieldSuggestion, FormControl, UserProfile

logger = logging.getLogger("compass_mcp.llm.assistant")

ABSTENTION_REASONING = (
    "Fact not explicitly present in profile.yaml — flagged for manual user input"
)

BANNED_CLICHES: tuple[str, ...] = (
    "delve",
    "testament",
    "tapestry",
    "passionate",
    "thrilled",
    "excited",
    "dynamic",
    "leverage",
    "spearhead",
    "synergy",
    "cutting-edge",
    "seamlessly",
    "pivotal",
    "honing",
    "eager",
    "align",
    "foster",
    "realm",
    "embark",
    "unwavering",
    "moreover",
    "furthermore",
)

_CLICHE_REPLACEMENTS: dict[str, str] = {
    "delve": "examine",
    "testament": "example",
    "tapestry": "set",
    "passionate": "focused",
    "thrilled": "ready",
    "excited": "ready",
    "dynamic": "production",
    "leverage": "use",
    "leveraged": "used",
    "leveraging": "using",
    "spearhead": "lead",
    "spearheaded": "led",
    "spearheading": "leading",
    "synergy": "collaboration",
    "cutting-edge": "modern",
    "seamlessly": "cleanly",
    "pivotal": "key",
    "honing": "refining",
    "eager": "ready",
    "align": "match",
    "aligns": "matches",
    "foster": "support",
    "realm": "area",
    "embark": "start",
    "unwavering": "consistent",
    "moreover": "also",
    "furthermore": "additionally",
}

_DEMOGRAPHIC_OR_SENSITIVE_RE = re.compile(
    r"\b("
    r"gender|sex|race|ethnicity|hispanic|latino|veteran|disability|disabled|pronoun|pronouns|"
    r"access\s+requirements|reasonable\s+accommodation|special\s+accommodation|"
    r"sexual\s+orientation|marital\s+status|religion|age\s+group|date\s+of\s+birth|"
    r"social\s+security|ssn|national\s+id|passport\s+number|tax\s+id|"
    r"security\s+clearance|top\s+secret|polygraph"
    r")\b",
    re.IGNORECASE,
)

_PHONE_PROMPT_RE = re.compile(r"\b(phone|mobile|telephone|cell\s+number|whatsapp)\b", re.IGNORECASE)
_NOTICE_PROMPT_RE = re.compile(r"\b(notice\s+period|kündigungsfrist)\b", re.IGNORECASE)
_AVAILABILITY_PROMPT_RE = re.compile(
    r"\b(earliest\s+start|start\s+date|available\s+from|availability|when\s+can\s+you\s+start|eintrittsdatum)\b",
    re.IGNORECASE,
)
_SALARY_PROMPT_RE = re.compile(
    r"\b(salary|compensation|pay\s+expectation|desired\s+pay|gehaltsvorstellung|expected\s+ctc|current\s+ctc)\b",
    re.IGNORECASE,
)
_PORTFOLIO_PROMPT_RE = re.compile(
    r"\b(portfolio|personal\s+website|blog\s+url)\b",
    re.IGNORECASE,
)
_EXPERIENCE_WITH_RE = re.compile(
    r"(?:years\s+of\s+experience\s+(?:with|in)|do\s+you\s+have\s+(?:production\s+)?experience\s+(?:with|in)|proficiency\s+in)\s+([a-z0-9+#.\-/ ]{2,40})",
    re.IGNORECASE,
)

_STRUCTURED_TYPES: set[ControlType] = {
    "select",
    "radio",
    "checkbox",
    "combobox",
    "numeric",
    "date",
}


def abstain_suggestion(reason: str = ABSTENTION_REASONING) -> FieldSuggestion:
    """Return the canonical zero-confidence abstention response."""
    return FieldSuggestion(
        value="",
        confidence=0.0,
        reasoning=reason,
        matched_option=None,
        should_cache=False,
    )


def sanitize_natural_engineer_prose(text: str, max_words: int = 75) -> str:
    """Enforce direct human engineer style: no em-dashes, semicolons, bullets, or banned cliches."""
    if not text or not text.strip():
        return ""

    cleaned = text.strip()
    # Strip markdown bullets or numbered list prefixes on lines
    lines = [
        re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", line).strip()
        for line in cleaned.splitlines()
        if line.strip()
    ]
    cleaned = " ".join(lines)

    # Replace em-dashes, en-dashes, and semicolons with simple punctuation
    cleaned = cleaned.replace("—", ", ").replace("–", "-").replace(";", ".")
    cleaned = re.sub(r",\s*,", ",", cleaned)
    cleaned = re.sub(r"\.\s*\.", ".", cleaned)

    # Replace any banned cliches (case-preserving where practical)
    for word, replacement in _CLICHE_REPLACEMENTS.items():
        pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)

        def _repl(match: re.Match[str], rep: str = replacement) -> str:
            original = match.group(0)
            if original[0].isupper():
                return rep.capitalize()
            return rep

        cleaned = pattern.sub(_repl, cleaned)

    # Also ensure none of BANNED_CLICHES remain
    for banned in BANNED_CLICHES:
        pattern = re.compile(rf"\b{re.escape(banned)}\b", re.IGNORECASE)
        cleaned = pattern.sub("", cleaned)

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([,.:!?])", r"\1", cleaned)

    # Enforce 2-4 sentences and max_words cap
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    if len(sentences) > 4:
        sentences = sentences[:4]
    cleaned = " ".join(sentences)

    words = cleaned.split()
    if len(words) > max_words:
        # Trim cleanly at the last sentence boundary <= max_words if possible
        trimmed_words = words[:max_words]
        candidate = " ".join(trimmed_words)
        last_period = candidate.rfind(".")
        if last_period >= int(len(candidate) * 0.55):
            cleaned = candidate[: last_period + 1].strip()
        else:
            cleaned = candidate.rstrip(",;:-") + "."

    return cleaned


class OllamaAssistant:
    """Closed-world local Ollama assistant with Two-Mode routing (`temperature=0.0` vs `0.35`)."""

    HIGH_CONFIDENCE_THRESHOLD: float = 0.75
    CACHE_SAVE_THRESHOLD: float = 0.85

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.base_url: str = (
            base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        ).rstrip("/")
        self.model: str = model or os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
        raw_timeout = timeout_seconds or float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "45"))
        self.timeout_seconds: float = float(raw_timeout)

    @staticmethod
    def _build_profile_corpus(profile: UserProfile) -> str:
        """Flatten all documented profile strings into a lowercase searchable corpus for closed-world verification."""
        dump = profile.model_dump()
        return json.dumps(dump, ensure_ascii=False).lower()

    def check_closed_world_abstention(
        self,
        control: FormControl,
        profile: UserProfile,
    ) -> FieldSuggestion | None:
        """Deterministically abstain when a prompt requests a fact absent from `profile.yaml`."""
        label = (control.label or "").strip()
        if not label:
            return abstain_suggestion()

        if control.is_consent_control:
            return abstain_suggestion(
                "Legal/privacy consent control requires explicit human confirmation"
            )

        if _DEMOGRAPHIC_OR_SENSITIVE_RE.search(label):
            return abstain_suggestion()

        # Check fields intentionally left empty ("") in profile.yaml
        if _PHONE_PROMPT_RE.search(label) and not profile.personal.phone.strip():
            return abstain_suggestion()

        if _NOTICE_PROMPT_RE.search(label) and not profile.preferences.notice_period.strip():
            return abstain_suggestion()

        if (
            _AVAILABILITY_PROMPT_RE.search(label)
            and not profile.preferences.availability_date.strip()
            and not profile.preferences.notice_period.strip()
        ):
            return abstain_suggestion()

        if _SALARY_PROMPT_RE.search(label) and not profile.preferences.salary_expectations.strip():
            return abstain_suggestion()

        if (
            _PORTFOLIO_PROMPT_RE.search(label)
            and not profile.links.portfolio.strip()
            and not profile.links.website.strip()
        ):
            return abstain_suggestion()

        # Check specific technology/skill queries against the closed-world profile corpus
        skill_match = _EXPERIENCE_WITH_RE.search(label)
        if skill_match:
            queried_topic = normalize_prompt_key(skill_match.group(1))
            # Strip trailing generic words
            for suffix in ("development", "programming", "systems", "tools", "framework", "frameworks"):
                if queried_topic.endswith(f" {suffix}"):
                    queried_topic = queried_topic[: -len(suffix)].strip()
            if queried_topic and queried_topic not in {
                "software",
                "software engineering",
                "backend",
                "ai",
                "machine learning",
            }:
                corpus = self._build_profile_corpus(profile)
                tokens = [t for t in queried_topic.split() if len(t) >= 2]
                if tokens and not all(tok in corpus for tok in tokens):
                    return abstain_suggestion()

        return None

    def deterministic_structured_match(
        self,
        control: FormControl,
        profile: UserProfile,
    ) -> FieldSuggestion | None:
        """Resolve unambiguous factual profile questions deterministically before calling Ollama."""
        label_lower = control.label.strip().lower()
        wa = profile.work_authorization

        # Work authorization checks
        if "authorized" in label_lower or "legally eligible" in label_lower or "right to work" in label_lower:
            if any(k in label_lower for k in ("united states", "u.s.", " us ")):
                ans = "Yes" if wa.authorized_us else "No"
                matched = QACacheManager._align_with_options(ans, control.options)
                if matched:
                    return FieldSuggestion(
                        value=matched,
                        confidence=0.98,
                        reasoning="Explicitly documented in profile.yaml work_authorization.authorized_us.",
                        matched_option=matched if control.options else None,
                        should_cache=True,
                    )
            if any(
                k in label_lower
                for k in (
                    "eu",
                    "european union",
                    "germany",
                    "poland",
                    "netherlands",
                    "france",
                    "spain",
                    "italy",
                    "austria",
                    "belgium",
                    "sweden",
                    "ireland",
                    "portugal",
                    "finland",
                    "denmark",
                    "czech",
                )
            ):
                ans = "Yes" if wa.authorized_eu else "No"
                matched = QACacheManager._align_with_options(ans, control.options)
                if matched:
                    return FieldSuggestion(
                        value=matched,
                        confidence=0.98,
                        reasoning="Explicitly documented in profile.yaml work_authorization.authorized_eu.",
                        matched_option=matched if control.options else None,
                        should_cache=True,
                    )
            if "india" in label_lower:
                ans = "Yes" if wa.authorized_india else "No"
                matched = QACacheManager._align_with_options(ans, control.options)
                if matched:
                    return FieldSuggestion(
                        value=matched,
                        confidence=0.98,
                        reasoning="Explicitly documented in profile.yaml work_authorization.authorized_india.",
                        matched_option=matched if control.options else None,
                        should_cache=True,
                    )

        # Visa sponsorship checks
        if "sponsorship" in label_lower or "visa" in label_lower:
            if any(
                k in label_lower
                for k in (
                    "eu",
                    "european union",
                    "germany",
                    "poland",
                    "netherlands",
                    "france",
                    "spain",
                )
            ):
                ans = "Yes" if wa.requires_eu_visa_sponsorship else "No"
            elif any(k in label_lower for k in ("united states", "u.s.", " us ", "h-1b", "h1b")):
                ans = "Yes" if wa.requires_us_visa_sponsorship else "No"
            else:
                ans = (
                    "Yes"
                    if (wa.requires_eu_visa_sponsorship or wa.requires_us_visa_sponsorship)
                    else "No"
                )
            matched = QACacheManager._align_with_options(ans, control.options)
            if matched:
                return FieldSuggestion(
                    value=matched,
                    confidence=0.97,
                    reasoning="Explicitly documented in profile.yaml work_authorization sponsorship fields.",
                    matched_option=matched if control.options else None,
                    should_cache=True,
                )

        # Legal First / Last Name checks
        if "first name" in label_lower and "preferred" not in label_lower and profile.personal.first_name.strip():
            return FieldSuggestion(
                value=profile.personal.first_name.strip(),
                confidence=0.99,
                reasoning="Explicitly documented in profile.yaml personal.first_name.",
                should_cache=True,
            )
        if "last name" in label_lower and profile.personal.last_name.strip():
            return FieldSuggestion(
                value=profile.personal.last_name.strip(),
                confidence=0.99,
                reasoning="Explicitly documented in profile.yaml personal.last_name.",
                should_cache=True,
            )

        # Current country / residence / location checks
        if any(
            k in label_lower
            for k in (
                "where do you currently live",
                "country of residence",
                "current country",
                "where are you based",
                "where are you located",
                "country",
                "location",
            )
        ):
            country = profile.personal.country.strip()
            location = profile.personal.location.strip()
            if control.options and country:
                matched = QACacheManager._align_with_options(country, control.options)
                if matched:
                    return FieldSuggestion(
                        value=matched,
                        confidence=0.98,
                        reasoning=f"Explicitly documented in profile.yaml personal.country ('{country}').",
                        matched_option=matched,
                        should_cache=True,
                    )
            elif not control.options and (country or location):
                val = country if "country" in label_lower and country else (location or country)
                return FieldSuggestion(
                    value=val,
                    confidence=0.96,
                    reasoning="Explicitly documented in profile.yaml personal.location / personal.country.",
                    should_cache=True,
                )

        # Notice period / Earliest availability checks
        if _NOTICE_PROMPT_RE.search(label_lower) and profile.preferences.notice_period.strip():
            val = profile.preferences.notice_period.strip()
            matched = QACacheManager._align_with_options(val, control.options)
            if matched:
                return FieldSuggestion(
                    value=matched,
                    confidence=0.97,
                    reasoning="Explicitly documented in profile.yaml preferences.notice_period.",
                    matched_option=matched if control.options else None,
                    should_cache=True,
                )

        if _AVAILABILITY_PROMPT_RE.search(label_lower):
            val = (
                profile.preferences.availability_date.strip()
                or profile.preferences.notice_period.strip()
            )
            if val:
                matched = QACacheManager._align_with_options(val, control.options)
                if matched:
                    return FieldSuggestion(
                        value=matched,
                        confidence=0.96,
                        reasoning="Explicitly documented in profile.yaml preferences (availability_date / notice_period).",
                        matched_option=matched if control.options else None,
                        should_cache=True,
                    )

        # Salary expectations checks
        if _SALARY_PROMPT_RE.search(label_lower) and profile.preferences.salary_expectations.strip():
            val = profile.preferences.salary_expectations.strip()
            matched = QACacheManager._align_with_options(val, control.options)
            if matched:
                return FieldSuggestion(
                    value=matched,
                    confidence=0.97,
                    reasoning="Explicitly documented in profile.yaml preferences.salary_expectations.",
                    matched_option=matched if control.options else None,
                    should_cache=True,
                )

        # LinkedIn / GitHub / Portfolio URL checks
        if "linkedin" in label_lower and profile.links.linkedin.strip() and not control.options:
            return FieldSuggestion(
                value=profile.links.linkedin.strip(),
                confidence=0.99,
                reasoning="Explicitly documented in profile.yaml links.linkedin.",
                should_cache=True,
            )
        if "github" in label_lower and profile.links.github.strip() and not control.options:
            return FieldSuggestion(
                value=profile.links.github.strip(),
                confidence=0.99,
                reasoning="Explicitly documented in profile.yaml links.github.",
                should_cache=True,
            )
        if _PORTFOLIO_PROMPT_RE.search(label_lower) and not control.options:
            val = (profile.links.portfolio or profile.links.website).strip()
            if val:
                return FieldSuggestion(
                    value=val,
                    confidence=0.98,
                    reasoning="Explicitly documented in profile.yaml links.portfolio.",
                    should_cache=True,
                )

        # Relocation checks
        if "relocat" in label_lower:
            ans = "Yes" if wa.willing_to_relocate_eu_germany else "No"
            matched = QACacheManager._align_with_options(ans, control.options)
            if matched:
                return FieldSuggestion(
                    value=matched,
                    confidence=0.95,
                    reasoning="Explicitly documented in profile.yaml preferences.relocation.",
                    matched_option=matched if control.options else None,
                    should_cache=True,
                )

        # Language proficiency checks
        for lang_entry in profile.languages:
            lang_name = lang_entry.language.strip().lower()
            if lang_name and re.search(rf"\b{re.escape(lang_name)}\b", label_lower):
                if not control.options:
                    return FieldSuggestion(
                        value=lang_entry.proficiency,
                        confidence=0.95,
                        reasoning=f"Explicitly documented in profile.yaml languages ({lang_entry.language}).",
                        should_cache=True,
                    )
                matched = self._match_language_option(lang_entry.proficiency, control.options)
                if matched:
                    return FieldSuggestion(
                        value=matched,
                        confidence=0.93,
                        reasoning=f"Matched {lang_entry.language} proficiency '{lang_entry.proficiency}' from profile.yaml.",
                        matched_option=matched,
                        should_cache=True,
                    )

        return None

    @staticmethod
    def _match_language_option(proficiency: str, options: list[str]) -> str | None:
        prof_lower = proficiency.lower()
        tokens = [
            tok
            for tok in re.split(r"[\s/(),;-]+", prof_lower)
            if len(tok) >= 2
        ]
        for opt in options:
            opt_lower = opt.lower()
            if any(tok in opt_lower for tok in tokens):
                return opt
        return QACacheManager._align_with_options(proficiency, options)

    def _is_mode_2_open_ended(self, control: FormControl) -> bool:
        if control.control_type == "textarea":
            return True
        if control.control_type == "text" and not control.options:
            label_lower = control.label.lower()
            open_prompts = (
                "why ",
                "describe ",
                "tell us ",
                "explain ",
                "what motivates",
                "summary",
                "cover note",
                "additional information",
                "project",
            )
            return any(k in label_lower for k in open_prompts)
        return False

    async def suggest_for_control(
        self,
        control: FormControl,
        profile: UserProfile,
        cache: QACacheManager,
        role_context: str = "",
        extra_notes: str = "",
    ) -> tuple[FieldSuggestion, bool, bool]:
        """Resolve a `FormControl` via cache, deterministic closed-world check, or local Ollama.

        Returns `(suggestion, from_cache, called_ollama)`.
        """
        # 1. Check QACacheManager first (exact + fuzzy >= 0.88)
        cached = cache.lookup(control.label, options=control.options or None)
        if cached is not None and cached.value.strip():
            return cached, True, False

        # 2. Strict Closed-World Abstention Pre-Check
        abstained = self.check_closed_world_abstention(control, profile)
        if abstained is not None:
            return abstained, False, False

        # 3. Deterministic factual match from profile.yaml
        deterministic = self.deterministic_structured_match(control, profile)
        if deterministic is not None:
            if (
                deterministic.should_cache
                and deterministic.confidence >= self.CACHE_SAVE_THRESHOLD
            ):
                cache.store(
                    prompt=control.label,
                    value=deterministic.value,
                    control_type=control.control_type,
                    confidence=deterministic.confidence,
                    source="profile_deterministic",
                )
            return deterministic, False, False

        # 4. Query Local Ollama (Mode 1 vs Mode 2)
        if self._is_mode_2_open_ended(control):
            suggestion = await self._query_mode2_open_ended(
                control=control,
                profile=profile,
                role_context=role_context,
                extra_notes=extra_notes,
            )
        else:
            suggestion = await self._query_mode1_structured(
                control=control,
                profile=profile,
            )
            if (
                suggestion.confidence >= self.CACHE_SAVE_THRESHOLD
                and suggestion.value.strip()
            ):
                cache.store(
                    prompt=control.label,
                    value=suggestion.value,
                    control_type=control.control_type,
                    confidence=suggestion.confidence,
                    source="ollama_mode1",
                )

        return suggestion, False, True

    async def _query_mode1_structured(
        self,
        control: FormControl,
        profile: UserProfile,
    ) -> FieldSuggestion:
        """Mode 1: Structured controls (`temperature=0.0`) with strict closed-world abstention."""
        profile_json = json.dumps(profile.model_dump(), indent=2, ensure_ascii=False)
        options_list = control.options or []

        system_prompt = (
            "You are a strict closed-world form accessibility assistant. "
            "Your ONLY source of truth is the provided `profile.yaml` JSON.\n"
            "CRITICAL ZERO-HALLUCINATION RULES:\n"
            "1. NEVER assume, extrapolate, or invent any fact, skill, certification, clearance, "
            "phone number, notice period, availability date, salary expectation, or demographic attribute.\n"
            "2. If the requested fact is empty (\"\") or NOT explicitly documented in `profile.yaml`, "
            "you MUST abstain by returning: "
            f'{{"value": "", "confidence": 0.0, "reasoning": "{ABSTENTION_REASONING}", "matched_option": null, "should_cache": false}}.\n'
            "3. When `control.options` is non-empty and the fact IS explicitly in `profile.yaml`, "
            "`value` and `matched_option` MUST be an exact string copied verbatim from `control.options`.\n"
            "4. Respond strictly with valid JSON matching the schema."
        )

        user_prompt = (
            f"CONTROL LABEL: {control.label}\n"
            f"CONTROL TYPE: {control.control_type}\n"
            f"SECTION HEADING: {control.section_heading}\n"
            f"AVAILABLE OPTIONS: {json.dumps(options_list, ensure_ascii=False)}\n\n"
            f"CLOSED-WORLD PROFILE.YAML:\n{profile_json}\n"
        )

        suggestion = await self._call_ollama_chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.0,
        )

        if suggestion.confidence <= 0.0 or not suggestion.value.strip():
            return abstain_suggestion(suggestion.reasoning or ABSTENTION_REASONING)

        # Verify option alignment when options exist
        if options_list:
            candidate = suggestion.matched_option or suggestion.value
            aligned = QACacheManager._align_with_options(candidate, options_list)
            if aligned is None:
                return abstain_suggestion(
                    "Suggested value did not match any available option in control.options"
                )
            suggestion.value = aligned
            suggestion.matched_option = aligned

        return suggestion

    async def _query_mode2_open_ended(
        self,
        control: FormControl,
        profile: UserProfile,
        role_context: str = "",
        extra_notes: str = "",
    ) -> FieldSuggestion:
        """Mode 2: Open-ended textareas (`temperature=0.35`) with natural engineer writing style."""
        profile_json = json.dumps(profile.model_dump(), indent=2, ensure_ascii=False)
        banned_str = ", ".join(BANNED_CLICHES)

        system_prompt = (
            "You are assisting Lead AI & Software Engineer Ramana Gangarao in drafting a short, "
            "factual response for a job application form.\n"
            "STRICT CLOSED-WORLD GROUNDING RULES:\n"
            "1. Treat `profile.yaml` as the single closed-world source of truth. ONLY mention companies, "
            "roles, architectures, metrics, and technologies explicitly listed in `profile.yaml` "
            "(such as Synechron/Wells Fargo AI governance & LLM-as-a-Judge platform, Innovapath/Optum "
            "9-agent Google ADK contract pipeline with PHI/PII compliance, Atlantis University TalentScreen "
            "RAG & multi-agent platform, Wipro MLOps, and INNOVAPATH IT Solutions).\n"
            "2. NEVER invent experiences or claim unverified skills to match a job description keyword.\n"
            "3. If the question asks for a private/missing fact not in `profile.yaml` (e.g., salary, notice period, "
            "or a completely unrelated domain), abstain with `confidence: 0.0` and `value: \"\"`.\n\n"
            "DIRECT NATURAL HUMAN ENGINEER WRITING STYLE:\n"
            "- Write in clear, straightforward, everyday professional English.\n"
            "- Keep the response concise: 2 to 4 sentences (40 to 75 words maximum).\n"
            "- Lead directly with concrete technical experience.\n"
            f"- NEVER use any of these banned cliches: {banned_str}.\n"
            "- SIMPLE PUNCTUATION: Do NOT use em-dashes (—), semicolons (;), or bulleted lists. "
            "Use standard periods and commas only."
        )

        user_prompt = (
            f"FORM QUESTION: {control.label}\n"
            f"EXTRA USER NOTES: {extra_notes or 'None'}\n"
            f"ROLE DESCRIPTION CONTEXT (for relevance selection only; never invent facts from this):\n"
            f"{role_context[:3000] if role_context else 'Not provided'}\n\n"
            f"CLOSED-WORLD PROFILE.YAML:\n{profile_json}\n"
        )

        suggestion = await self._call_ollama_chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.35,
        )

        if suggestion.confidence < self.HIGH_CONFIDENCE_THRESHOLD or not suggestion.value.strip():
            return abstain_suggestion(suggestion.reasoning or ABSTENTION_REASONING)

        suggestion.value = sanitize_natural_engineer_prose(suggestion.value, max_words=75)
        suggestion.should_cache = False
        return suggestion

    async def _call_ollama_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> FieldSuggestion:
        """Execute structured JSON-schema `/api/chat` call against local Ollama."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": FieldSuggestion.model_json_schema(),
            "options": {
                "temperature": temperature,
            },
        }

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
            ) as client:
                response = await client.post("/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.warning("Local Ollama request failed (%s): %s", self.base_url, exc)
            return abstain_suggestion(
                f"Local Ollama unavailable ({exc.__class__.__name__}) — flagged for manual user input"
            )

        raw_content = ""
        if isinstance(data, dict):
            msg = data.get("message")
            if isinstance(msg, dict):
                raw_content = str(msg.get("content") or "").strip()
            elif "response" in data:
                raw_content = str(data.get("response") or "").strip()

        if not raw_content:
            return abstain_suggestion("Empty response from local Ollama")

        try:
            return FieldSuggestion.model_validate_json(raw_content)
        except Exception as exc:
            logger.warning("Failed to validate Ollama JSON against FieldSuggestion: %s", exc)
            return abstain_suggestion("Invalid structured output from local Ollama")
