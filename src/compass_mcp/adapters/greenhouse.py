"""Greenhouse adapter supporting both classic #grnhse_iframe and modern React-Select job-boards.greenhouse.io."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from compass_mcp.adapters.base import BasePortalAdapter
from compass_mcp.models import PortalType

if TYPE_CHECKING:
    from playwright.async_api import Frame, Locator, Page

    from compass_mcp.browser.pacing import PacingController

logger = logging.getLogger("compass_mcp.adapters.greenhouse")

_GREENHOUSE_URL_RE = re.compile(
    r"(?:boards\.greenhouse\.io|job-boards\.greenhouse\.io|greenhouse\.io/.*/jobs/|gh_jid=)",
    re.IGNORECASE,
)


class GreenhouseAdapter(BasePortalAdapter):
    """Adapter for classic (`boards.greenhouse.io` / `#grnhse_iframe`) and modern Remix/React Greenhouse."""

    portal_type: PortalType = "greenhouse"

    @classmethod
    async def matches(cls, url: str, page: Page | None = None) -> bool:
        if _GREENHOUSE_URL_RE.search(url or ""):
            return True
        if page is not None:
            try:
                count = await page.locator(
                    "#grnhse_iframe, iframe[src*='greenhouse.io'], form#application_form, #application-form"
                ).count()
                if count > 0:
                    return True
            except Exception:
                pass
        return False

    def standard_field_selectors(self) -> dict[str, list[str]]:
        base = super().standard_field_selectors()
        base["first_name"] = [
            "#first_name",
            "input[name='job_application[first_name]']",
            *base["first_name"],
        ]
        base["last_name"] = [
            "#last_name",
            "input[name='job_application[last_name]']",
            *base["last_name"],
        ]
        base["email"] = [
            "#email",
            "input[name='job_application[email]']",
            *base["email"],
        ]
        base["phone"] = [
            "#phone",
            "input[name='job_application[phone]']",
            *base["phone"],
        ]
        base["location"] = [
            "#candidate-location",
            "#job_application_location",
            "input[id*='location'][role='combobox']",
            "input[name='job_application[location]']",
            *base["location"],
        ]
        return base

    def file_field_selectors(self) -> dict[str, list[str]]:
        return {
            "resume": [
                "input#resume",
                "input[type='file'][id*='resume' i]",
                "div[data-field='resume'] input[type='file']",
                "input[type='file']:first-of-type",
            ],
            "cover_letter": [
                "input#cover_letter",
                "input[type='file'][id*='cover_letter' i]",
                "div[data-field='cover_letter'] input[type='file']",
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
        """Handle Modern Greenhouse React-Select location (`#candidate-location`) and `input[role='combobox']`."""
        try:
            el_id = (await locator.get_attribute("id") or "").lower()
            role = (await locator.get_attribute("role") or "").lower()
            if (
                field_name == "location"
                or el_id == "candidate-location"
                or role == "combobox"
            ):
                return await self.fill_combobox_locator(
                    target,
                    locator,
                    value,
                    pacer,
                    wait_ms=600,
                )
        except Exception as exc:
            logger.debug("Greenhouse combobox check fallback on %s: %s", selector, exc)

        return await super().fill_standard_locator(
            target,
            field_name,
            selector,
            locator,
            value,
            pacer,
        )
