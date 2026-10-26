"""Natural interaction pacing and minimum session duration controller."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Frame, Locator, Page

logger = logging.getLogger("compass_mcp.browser.pacing")

_TOKEN_SPLIT_RE = re.compile(r"(\s+|[,.;:!?/-])")


class PacingController:
    """Paces browser interactions to ensure React/Vue state listeners and validators settle cleanly."""

    def __init__(
        self,
        min_session_seconds: float | None = None,
        enabled: bool = True,
    ) -> None:
        self.session_start: float = time.monotonic()
        self.enabled: bool = enabled

        env_floor_raw = os.getenv("CAREER_COMPASS_MIN_SESSION_SECONDS", "25")
        try:
            configured_floor = (
                float(min_session_seconds)
                if min_session_seconds is not None
                else float(env_floor_raw)
            )
        except ValueError:
            configured_floor = 25.0

        self.configured_floor: float = max(0.0, configured_floor)
        if not self.enabled or self.configured_floor <= 0.0:
            self.target_session_seconds: float = 0.0
        elif 22.0 <= self.configured_floor <= 32.0:
            self.target_session_seconds = random.uniform(22.0, 32.0)
        else:
            low = max(1.0, self.configured_floor * 0.88)
            high = self.configured_floor * 1.28
            self.target_session_seconds = random.uniform(low, high)

    @property
    def elapsed_seconds(self) -> float:
        """Elapsed monotonic seconds since this session started."""
        return time.monotonic() - self.session_start

    @property
    def remaining_floor_seconds(self) -> float:
        """Remaining seconds required to reach the natural session duration floor."""
        if not self.enabled:
            return 0.0
        return max(0.0, self.target_session_seconds - self.elapsed_seconds)

    async def sleep_range(self, min_s: float, max_s: float) -> float:
        """Pause for a randomized interval within [min_s, max_s]."""
        if not self.enabled:
            return 0.0
        duration = random.uniform(min_s, max_s)
        await asyncio.sleep(duration)
        return duration

    async def inter_field_delay(self) -> float:
        """Pause 0.8s–1.8s between consecutive form fields so onBlur validation hooks settle."""
        return await self.sleep_range(0.8, 1.8)

    async def smooth_scroll_into_view(self, locator: Locator) -> None:
        """Smoothly scroll an element into the center of the viewport and pause 0.3s–0.7s."""
        try:
            await locator.evaluate(
                """(el) => {
                    if (el && typeof el.scrollIntoView === 'function') {
                        el.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
                    }
                }"""
            )
        except Exception:
            try:
                await locator.scroll_into_view_if_needed(timeout=2500)
            except Exception as exc:
                logger.debug("Could not scroll locator into view: %s", exc)
        await self.sleep_range(0.3, 0.7)

    async def paced_fill(
        self,
        locator: Locator,
        text: str,
        *,
        clear_first: bool = True,
        apply_inter_field_delay: bool = True,
    ) -> bool:
        """Populate a text/textarea/combobox control using human-cadence keystrokes.

        Steps:
        1. Smoothly scroll element into view and pause 0.3s–0.7s.
        2. Click to trigger focus and pause 0.2s–0.4s.
        3. Type tokens sequentially with 35–85ms keystroke delay and 0.15s pauses at spaces/punctuation.
        4. Trigger blur / inter-field settle pause (0.8s–1.8s).
        """
        value = str(text or "")
        if not value:
            return False

        try:
            await self.smooth_scroll_into_view(locator)

            # Focus element via click
            await locator.click(timeout=4000)
            await self.sleep_range(0.2, 0.4)

            if clear_first:
                try:
                    await locator.fill("")
                except Exception:
                    await locator.press("ControlOrMeta+a")
                    await locator.press("Backspace")
                await self.sleep_range(0.08, 0.16)

            if not self.enabled:
                await locator.fill(value)
                return True

            tokens = [tok for tok in _TOKEN_SPLIT_RE.split(value) if tok]
            for token in tokens:
                delay_ms = random.randint(35, 85)
                await locator.press_sequentially(token, delay=delay_ms)
                if token.isspace() or token in {",", ".", ";", ":", "!", "?", "/", "-"}:
                    await asyncio.sleep(0.15)

            # Ensure React/Vue controlled state listeners observe input & change events
            try:
                await locator.evaluate(
                    """(el) => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }"""
                )
            except Exception as exc:
                logger.debug("Event dispatch after paced_fill skipped: %s", exc)

            if apply_inter_field_delay:
                await self.inter_field_delay()
            return True
        except Exception as exc:
            logger.warning("paced_fill failed on locator (%s): %s", locator, exc)
            return False

    async def enforce_session_floor(self, page: Page | Frame) -> float:
        """Ensure total staging duration meets the 22.0s–32.0s floor using smooth viewport scrolling."""
        remaining = self.remaining_floor_seconds
        if remaining <= 0.0:
            return self.elapsed_seconds

        logger.info(
            "Pacing remaining %.1fs (elapsed=%.1fs, target=%.1fs) with incremental viewport review.",
            remaining,
            self.elapsed_seconds,
            self.target_session_seconds,
        )

        # Perform smooth incremental viewport scrolling across the form while waiting
        steps = max(2, min(6, int(remaining // 3.5) + 1))
        step_pause = remaining / steps

        for idx in range(steps):
            fraction = (idx + 1) / steps
            try:
                await page.evaluate(
                    """(ratio) => {
                        const doc = document.scrollingElement || document.documentElement || document.body;
                        if (!doc) return;
                        const maxScroll = Math.max(0, doc.scrollHeight - window.innerHeight);
                        const targetY = Math.round(maxScroll * ratio);
                        window.scrollTo({ top: targetY, behavior: 'smooth' });
                    }""",
                    fraction,
                )
            except Exception as exc:
                logger.debug("Incremental scroll step skipped: %s", exc)

            sleep_chunk = min(step_pause, self.remaining_floor_seconds)
            if sleep_chunk > 0:
                await asyncio.sleep(sleep_chunk)

        final_remaining = self.remaining_floor_seconds
        if final_remaining > 0:
            await asyncio.sleep(final_remaining)

        return self.elapsed_seconds
