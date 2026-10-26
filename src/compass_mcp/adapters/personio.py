"""Personio EU portal layout adapter with multi-tab and categorized document dropzone support."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from compass_mcp.adapters.base import BasePortalAdapter
from compass_mcp.models import PortalType, UserProfile

if TYPE_CHECKING:
    from playwright.async_api import Frame, Page

    from compass_mcp.browser.pacing import PacingController

logger = logging.getLogger("compass_mcp.adapters.personio")

_PERSONIO_URL_RE = re.compile(r"\.jobs\.personio\.(?:de|com)/", re.IGNORECASE)


class PersonioAdapter(BasePortalAdapter):
    """Adapter for Personio EU career portals (`*.jobs.personio.de` / `*.jobs.personio.com`)."""

    portal_type: PortalType = "personio"

    @classmethod
    async def matches(cls, url: str, page: Page | None = None) -> bool:
        if _PERSONIO_URL_RE.search(url or ""):
            return True
        if page is not None:
            try:
                count = await page.locator(
                    "#career-application-form, form[action*='personio'], [data-test-id*='personio']"
                ).count()
                if count > 0:
                    return True
            except Exception:
                pass
        return False

    def standard_field_selectors(self) -> dict[str, list[str]]:
        base = super().standard_field_selectors()
        base["first_name"] = [
            "input[name='first_name']",
            *base["first_name"],
        ]
        base["last_name"] = [
            "input[name='last_name']",
            *base["last_name"],
        ]
        base["email"] = [
            "input[name='email']",
            *base["email"],
        ]
        base["phone"] = [
            "input[name='phone']",
            *base["phone"],
        ]
        base["location"] = [
            "input[name='city']",
            "input[name='location']",
            *base["location"],
        ]
        return base

    def file_field_selectors(self) -> dict[str, list[str]]:
        return {
            "resume": [
                "input[type='file'][name*='cv' i]",
                "input[type='file'][data-category*='cv' i]",
                "input[type='file'][id*='cv' i]",
                "input[type='file'][name*='resume' i]",
                "input[type='file']",
            ],
            "cover_letter": [
                "input[type='file'][name*='cover' i]",
                "input[type='file'][data-category*='cover-letter' i]",
                "input[type='file'][id*='cover' i]",
            ],
        }

    async def activate_application_tab_if_needed(
        self,
        target: Page | Frame,
        pacer: PacingController,
    ) -> None:
        """Switch from Personio's 'Job Description' tab to the 'Apply' tab if the form is hidden behind a tab."""
        tab_selectors = [
            "[role='tab'][href*='apply']",
            "a[href='#apply']",
            "button[data-action='open-application-form']",
            "[role='tab']:has-text('Apply')",
            "[role='tab']:has-text('Bewerben')",
        ]
        for sel in tab_selectors:
            try:
                loc = target.locator(sel)
                if await loc.count() == 0:
                    continue
                tab = loc.first
                if await tab.is_visible() and not await self.is_submit_locator(tab, sel):
                    await pacer.smooth_scroll_into_view(tab)
                    await tab.click(timeout=3000)
                    await pacer.sleep_range(0.6, 1.2)
                    break
            except Exception as exc:
                logger.debug("Personio tab activation check skipped for %s: %s", sel, exc)

    async def stage_standard_profile(
        self,
        target: Page | Frame,
        profile: UserProfile,
        pacer: PacingController,
    ) -> tuple[list[str], list[str], list[str]]:
        """Activate application tab if collapsed, then stage standard profile and categorized EU fields."""
        await self.activate_application_tab_if_needed(target, pacer)
        staged_fields, attached_docs, warnings = await super().stage_standard_profile(
            target,
            profile,
            pacer,
        )

        # Stage Personio-common EU fields (notice period / availability / salary) ONLY if explicitly set in profile.yaml
        eu_optional_map = {
            "notice_period": (
                profile.preferences.notice_period.strip(),
                [
                    "input[name*='notice_period' i]",
                    "input[name*='kündigungsfrist' i]",
                    "input[aria-label*='Notice period' i]",
                ],
            ),
            "availability_date": (
                profile.preferences.availability_date.strip(),
                [
                    "input[name*='available_from' i]",
                    "input[name*='availability' i]",
                    "input[aria-label*='Available from' i]",
                    "input[aria-label*='Earliest start' i]",
                ],
            ),
            "salary_expectations": (
                profile.preferences.salary_expectations.strip(),
                [
                    "input[name*='salary' i]",
                    "input[name*='gehaltsvorstellung' i]",
                    "input[aria-label*='Salary' i]",
                ],
            ),
        }

        for field_key, (val, selectors) in eu_optional_map.items():
            found = await self.find_first_visible_locator(target, selectors)
            if found is None:
                continue
            matched_sel, locator = found
            if not val:
                # Leave blank so Stage 3 checks qa_cache.json first or marks Amber for the user
                continue
            if await self.fill_standard_locator(target, field_key, matched_sel, locator, val, pacer):
                await self.apply_highlight(
                    target,
                    locator,
                    "blue",
                    f"Career Compass [Blue]: Populated '{field_key}' from config/profile.yaml",
                )
                staged_fields.append(field_key)

        return staged_fields, attached_docs, warnings
