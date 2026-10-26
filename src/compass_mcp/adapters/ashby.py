"""Ashby portal layout adapter with React-controlled input and aria-combobox helpers."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from compass_mcp.adapters.base import BasePortalAdapter
from compass_mcp.models import PortalType

if TYPE_CHECKING:
    from playwright.async_api import Frame, Locator, Page

    from compass_mcp.browser.pacing import PacingController

logger = logging.getLogger("compass_mcp.adapters.ashby")

_ASHBY_URL_RE = re.compile(r"jobs\.ashbyhq\.com/", re.IGNORECASE)


class AshbyAdapter(BasePortalAdapter):
    """Adapter for Ashby React forms (`jobs.ashbyhq.com`)."""

    portal_type: PortalType = "ashby"

    @classmethod
    async def matches(cls, url: str, page: Page | None = None) -> bool:
        if _ASHBY_URL_RE.search(url or ""):
            return True
        if page is not None:
            try:
                count = await page.locator(
                    "[id^='_systemfield_'], [class*='ashby-application-form'], meta[content*='ashbyhq']"
                ).count()
                if count > 0:
                    return True
            except Exception:
                pass
        return False

    def standard_field_selectors(self) -> dict[str, list[str]]:
        base = super().standard_field_selectors()
        base["full_name"] = [
            "#_systemfield_name",
            "input[name='_systemfield_name']",
            *base["full_name"],
        ]
        base["email"] = [
            "#_systemfield_email",
            "input[name='_systemfield_email']",
            *base["email"],
        ]
        base["phone"] = [
            "#_systemfield_phone",
            "input[name='_systemfield_phone']",
            *base["phone"],
        ]
        base["location"] = [
            "#_systemfield_location",
            "input[name='_systemfield_location']",
            "input[role='combobox'][aria-label*='Location' i]",
            *base["location"],
        ]
        return base

    def file_field_selectors(self) -> dict[str, list[str]]:
        return {
            "resume": [
                "#_systemfield_resume",
                "input[name='_systemfield_resume'][type='file']",
                "input[type='file']",
            ],
            "cover_letter": [
                "input[type='file'][name*='cover' i]",
                "input[type='file'][aria-label*='Cover' i]",
            ],
        }

    async def fill_standard_locator(
        self,
        target: Page | Frame,
        field_name: str,
        selector: str,
        locator: Locator,
        value: str,
        pacer: PacingController,
    ) -> bool:
        """Use React-friendly sequential typing and `aria-combobox` selection on Ashby forms."""
        try:
            role = (await locator.get_attribute("role") or "").lower()
            aria_auto = (await locator.get_attribute("aria-autocomplete") or "").lower()
            if role == "combobox" or aria_auto == "list" or field_name == "location":
                # Check if location input is an aria-combobox
                if role == "combobox" or aria_auto == "list":
                    return await self.fill_combobox_locator(target, locator, value, pacer, wait_ms=600)
        except Exception:
            pass

        filled = await pacer.paced_fill(locator, value)
        if filled:
            try:
                await locator.evaluate(
                    """(el) => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                    }"""
                )
            except Exception as exc:
                logger.debug("Ashby React event dispatch skipped: %s", exc)
        return filled
