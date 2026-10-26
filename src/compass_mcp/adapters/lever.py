"""Lever portal layout adapter with resume-upload-first parser settle handling."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from compass_mcp.adapters.base import BasePortalAdapter
from compass_mcp.models import PortalType, UserProfile

if TYPE_CHECKING:
    from playwright.async_api import Frame, Page

    from compass_mcp.browser.pacing import PacingController

logger = logging.getLogger("compass_mcp.adapters.lever")

_LEVER_URL_RE = re.compile(r"jobs\.lever\.co/", re.IGNORECASE)


class LeverAdapter(BasePortalAdapter):
    """Adapter for Lever forms (`jobs.lever.co`). Attaches resume first and waits for parser settle."""

    portal_type: PortalType = "lever"

    @classmethod
    async def matches(cls, url: str, page: Page | None = None) -> bool:
        if _LEVER_URL_RE.search(url or ""):
            return True
        if page is not None:
            try:
                count = await page.locator("#resume-upload-input, .application-form[action*='lever']").count()
                if count > 0:
                    return True
            except Exception:
                pass
        return False

    def standard_field_selectors(self) -> dict[str, list[str]]:
        base = super().standard_field_selectors()
        base["full_name"] = [
            "input[name='name']",
            *base["full_name"],
        ]
        base["email"] = [
            "input[name='email']",
            *base["email"],
        ]
        base["phone"] = [
            "input[name='phone']",
            *base["phone"],
        ]
        base["current_company"] = [
            "input[name='org']",
            *base["current_company"],
        ]
        base["location"] = [
            "input#location-input",
            "input[name='location']",
            *base["location"],
        ]
        base["linkedin"] = [
            "input[name='urls[LinkedIn]']",
            "input[name*='urls[LinkedIn' i]",
            *base["linkedin"],
        ]
        base["github"] = [
            "input[name='urls[GitHub]']",
            "input[name*='urls[GitHub' i]",
            "input[name='urls[Github]']",
            *base["github"],
        ]
        base["portfolio"] = [
            "input[name='urls[Portfolio]']",
            *base["portfolio"],
        ]
        base["website"] = [
            "input[name='urls[Other]']",
            "input[name='urls[Personal Website]']",
            *base["website"],
        ]
        return base

    def file_field_selectors(self) -> dict[str, list[str]]:
        return {
            "resume": [
                "#resume-upload-input",
                "input[name='resume'][type='file']",
                "input[type='file']",
            ],
            "cover_letter": [
                "input[name='coverLetter'][type='file']",
                "input[type='file'][id*='cover' i]",
            ],
        }

    async def stage_standard_profile(
        self,
        target: Page | Frame,
        profile: UserProfile,
        pacer: PacingController,
    ) -> tuple[list[str], list[str], list[str]]:
        """Attach `#resume-upload-input` FIRST and wait for Lever's resume parser before filling text fields."""
        attached_docs, warnings = await self.attach_documents(target, profile, pacer)

        if attached_docs:
            logger.info("Waiting for Lever resume parser to settle before populating profile fields.")
            try:
                await target.wait_for_load_state("networkidle", timeout=7000)
            except Exception:
                pass
            # Wait for any visible resume-upload parsing indicator to finish
            try:
                parsing_spinner = target.locator(".resume-upload-working, .resume-upload-Dropzone-loading")
                if await parsing_spinner.count() > 0:
                    await parsing_spinner.first.wait_for(state="hidden", timeout=6000)
            except Exception:
                pass
            await pacer.sleep_range(1.2, 2.0)

        staged_fields: list[str] = []
        field_values = self.extract_profile_field_values(profile)
        selector_map = self.standard_field_selectors()

        for field_name, selectors in selector_map.items():
            if field_name in ("first_name", "last_name"):
                # Lever uses unified `input[name="name"]`
                continue

            found = await self.find_first_visible_locator(target, selectors)
            if found is None:
                continue
            matched_selector, locator = found
            value = field_values.get(field_name, "")

            if not value:
                await self.apply_highlight(
                    target,
                    locator,
                    "amber",
                    f"Career Compass [Amber]: '{field_name}' is empty in profile.yaml — manual input or QA cache check needed.",
                )
                continue

            filled = await self.fill_standard_locator(target, field_name, matched_selector, locator, value, pacer)
            if filled:
                await self.apply_highlight(
                    target,
                    locator,
                    "blue",
                    f"Career Compass [Blue]: Populated '{field_name}' from config/profile.yaml",
                )
                staged_fields.append(field_name)

        return staged_fields, attached_docs, warnings
