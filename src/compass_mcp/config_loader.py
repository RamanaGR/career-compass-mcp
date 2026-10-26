"""YAML profile loader and fuzzy-capable JSON QA cache manager."""

from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Any

import yaml

from compass_mcp.models import ControlType, FieldSuggestion, QACacheEntry, UserProfile

logger = logging.getLogger("compass_mcp.config_loader")

_PUNCTUATION_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
_COMMON_FILLER_PREFIXES = (
    "please enter your ",
    "please provide your ",
    "please select your ",
    "please state your ",
    "tell us your ",
    "what is your ",
    "what are your ",
)


def resolve_project_root() -> Path:
    """Resolve the repository root directory reliably regardless of current working directory."""
    env_root = os.getenv("CAREER_COMPASS_ROOT")
    if env_root:
        candidate = Path(env_root).expanduser().resolve()
        if candidate.exists():
            return candidate

    # Walk upwards from this file (`src/compass_mcp/config_loader.py` -> repo root)
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / "config" / "profile.yaml").exists() or (parent / "pyproject.toml").exists():
            return parent

    return Path.cwd().resolve()


def resolve_config_path(env_var: str, default_relative: str) -> Path:
    """Resolve a configuration or asset path relative to the project root."""
    raw = os.getenv(env_var, default_relative)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = resolve_project_root() / path
    return path.resolve()


def normalize_prompt_key(prompt: str) -> str:
    """Normalize a form label or question prompt for deterministic cache comparison."""
    cleaned = prompt.strip().lower()
    cleaned = cleaned.replace("*", " ").replace("(required)", " ").replace("required", " ")
    cleaned = _PUNCTUATION_RE.sub(" ", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned


def _strip_conversational_prefix(normalized_key: str) -> str:
    """Strip common conversational prefixes ('what is your notice period' -> 'notice period') for fuzzy matching."""
    for prefix in _COMMON_FILLER_PREFIXES:
        if normalized_key.startswith(prefix) and len(normalized_key) > len(prefix) + 3:
            return normalized_key[len(prefix) :].strip()
    return normalized_key


def load_user_profile(profile_path: str | Path | None = None) -> UserProfile:
    """Load and validate the local YAML user profile."""
    target_path = (
        Path(profile_path).expanduser().resolve()
        if profile_path is not None
        else resolve_config_path("CAREER_COMPASS_PROFILE", "config/profile.yaml")
    )
    if not target_path.exists():
        logger.warning("Profile YAML not found at %s; using empty default UserProfile.", target_path)
        return UserProfile()

    with target_path.open("r", encoding="utf-8") as fh:
        raw_data = yaml.safe_load(fh) or {}

    if not isinstance(raw_data, dict):
        raise ValueError(f"Expected mapping at root of {target_path}, got {type(raw_data).__name__}")

    return UserProfile.model_validate(raw_data)


def resolve_document_path(relative_or_abs: str | Path) -> Path | None:
    """Resolve a local PDF path from profile.yaml and return it only if the file exists."""
    if not relative_or_abs:
        return None
    candidate = Path(relative_or_abs).expanduser()
    if not candidate.is_absolute():
        candidate = (resolve_project_root() / candidate).resolve()
    if candidate.is_file():
        return candidate

    # Fallback: if looking for a resume/CV in assets/ and default path doesn't match, find any CV/resume PDF
    lower_name = candidate.name.lower()
    if "resume" in lower_name or "cv" in lower_name:
        assets_dir = resolve_project_root() / "assets"
        if assets_dir.is_dir():
            pdfs = sorted(
                p
                for p in assets_dir.glob("*.pdf")
                if p.is_file() and "cover" not in p.name.lower()
            )
            if pdfs:
                logger.info("Resolved fallback resume PDF at %s", pdfs[0])
                return pdfs[0]
    return None


class QACacheManager:
    """Manages local prompt-to-answer caching with exact and fuzzy SequenceMatcher lookup."""

    FUZZY_THRESHOLD: float = 0.88

    def __init__(
        self,
        cache_path: str | Path | None = None,
        fuzzy_threshold: float = FUZZY_THRESHOLD,
    ) -> None:
        self.cache_path: Path = (
            Path(cache_path).expanduser().resolve()
            if cache_path is not None
            else resolve_config_path("CAREER_COMPASS_CACHE", "config/qa_cache.json")
        )
        self.fuzzy_threshold = fuzzy_threshold
        self._entries: dict[str, QACacheEntry] = {}
        self.reload()

    @property
    def entries(self) -> dict[str, QACacheEntry]:
        return dict(self._entries)

    def reload(self) -> None:
        """Load cached QA entries from disk."""
        self._entries.clear()
        if not self.cache_path.exists():
            return

        try:
            with self.cache_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:
            logger.warning("Failed to read QA cache at %s: %s", self.cache_path, exc)
            return

        raw_entries: Any = payload.get("entries", payload) if isinstance(payload, dict) else {}
        if isinstance(raw_entries, dict):
            for key, item in raw_entries.items():
                if isinstance(item, dict):
                    norm_key = normalize_prompt_key(item.get("normalized_key") or item.get("prompt") or key)
                    if not norm_key:
                        continue
                    entry = QACacheEntry.model_validate(
                        {
                            "prompt": item.get("prompt", key),
                            "normalized_key": norm_key,
                            "value": str(item.get("value", "")),
                            "control_type": item.get("control_type", "text"),
                            "confidence": float(item.get("confidence", 1.0)),
                            "source": item.get("source", "cache"),
                            "updated_at": item.get("updated_at", ""),
                        }
                    )
                    self._entries[norm_key] = entry
        elif isinstance(raw_entries, list):
            for item in raw_entries:
                if isinstance(item, dict) and item.get("prompt"):
                    norm_key = normalize_prompt_key(item.get("normalized_key") or item["prompt"])
                    if not norm_key:
                        continue
                    entry = QACacheEntry.model_validate(
                        {
                            **item,
                            "normalized_key": norm_key,
                            "value": str(item.get("value", "")),
                        }
                    )
                    self._entries[norm_key] = entry

    def lookup(
        self,
        prompt: str,
        options: list[str] | None = None,
    ) -> FieldSuggestion | None:
        """Look up a prompt using exact normalized match first, then SequenceMatcher >= 0.88."""
        norm_query = normalize_prompt_key(prompt)
        if not norm_query:
            return None

        # 1. Exact normalized match
        exact_entry = self._entries.get(norm_query)
        if exact_entry is not None:
            matched_val = self._align_with_options(exact_entry.value, options)
            if matched_val is not None:
                return FieldSuggestion(
                    value=matched_val,
                    confidence=max(exact_entry.confidence, 0.92),
                    reasoning=f"Matched exact local QA cache entry '{exact_entry.prompt}'.",
                    matched_option=matched_val if options else None,
                    should_cache=False,
                )

        # 2. Fuzzy SequenceMatcher lookup (threshold >= 0.88)
        best_ratio = 0.0
        best_entry: QACacheEntry | None = None
        stripped_query = _strip_conversational_prefix(norm_query)

        for key, entry in self._entries.items():
            direct_ratio = SequenceMatcher(None, norm_query, key).ratio()
            stripped_key = _strip_conversational_prefix(key)
            core_ratio = SequenceMatcher(None, stripped_query, stripped_key).ratio()
            ratio = max(direct_ratio, core_ratio)
            if ratio > best_ratio:
                best_ratio = ratio
                best_entry = entry

        if best_entry is not None and best_ratio >= self.fuzzy_threshold:
            matched_val = self._align_with_options(best_entry.value, options)
            if matched_val is not None:
                logger.info(
                    "Fuzzy QA cache hit (similarity=%.2f) for prompt '%s' -> '%s'",
                    best_ratio,
                    prompt,
                    best_entry.prompt,
                )
                return FieldSuggestion(
                    value=matched_val,
                    confidence=round(min(best_entry.confidence, best_ratio), 3),
                    reasoning=(
                        f"Fuzzy matched local QA cache entry '{best_entry.prompt}' "
                        f"(similarity={best_ratio:.2f})."
                    ),
                    matched_option=matched_val if options else None,
                    should_cache=False,
                )

        return None

    @staticmethod
    def _align_with_options(cached_value: str, options: list[str] | None) -> str | None:
        """Ensure cached values for select/radio/combobox controls match an available option string."""
        if not options:
            return cached_value

        cleaned_cached = cached_value.strip().lower()
        for opt in options:
            if opt.strip().lower() == cleaned_cached:
                return opt

        # Substring or high-similarity option alignment (e.g. "Yes" -> "Yes, I am authorized")
        for opt in options:
            opt_lower = opt.strip().lower()
            if cleaned_cached in ("yes", "no") and (
                opt_lower.startswith(f"{cleaned_cached},")
                or opt_lower.startswith(f"{cleaned_cached} ")
                or opt_lower == cleaned_cached
            ):
                return opt

        best_opt: str | None = None
        best_score = 0.0
        for opt in options:
            score = SequenceMatcher(None, cleaned_cached, opt.strip().lower()).ratio()
            if score > best_score:
                best_score = score
                best_opt = opt

        if best_opt is not None and best_score >= 0.85:
            return best_opt

        return None

    def store(
        self,
        prompt: str,
        value: str,
        control_type: ControlType = "text",
        confidence: float = 1.0,
        source: str = "user",
    ) -> QACacheEntry | None:
        """Save or update a prompt/answer mapping atomically in config/qa_cache.json."""
        norm_key = normalize_prompt_key(prompt)
        if not norm_key or not value.strip():
            return None

        entry = QACacheEntry(
            prompt=prompt.strip(),
            normalized_key=norm_key,
            value=value.strip(),
            control_type=control_type,
            confidence=round(float(confidence), 3),
            source=source,
            updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self._entries[norm_key] = entry
        self._write_atomic()
        return entry

    def _write_atomic(self) -> None:
        """Atomically persist the QA cache to disk."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = {
            "version": "1.0",
            "entries": {key: entry.model_dump() for key, entry in sorted(self._entries.items())},
        }
        fd, tmp_path = tempfile.mkstemp(
            prefix=".qa_cache_",
            suffix=".json",
            dir=str(self.cache_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                json.dump(serialized, tmp_file, indent=2, ensure_ascii=False)
                tmp_file.write("\n")
            os.replace(tmp_path, self.cache_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
