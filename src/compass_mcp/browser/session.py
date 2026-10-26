"""Singleton persistent async Playwright BrowserContext and Page session manager."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from compass_mcp.config_loader import resolve_config_path

if TYPE_CHECKING:
    from playwright.async_api import (
        BrowserContext,
        Frame,
        Page,
        Playwright,
    )

logger = logging.getLogger("compass_mcp.browser.session")


class BrowserSession:
    """Manages a persistent headed Chromium profile across MCP tool invocations."""

    _instance: ClassVar[BrowserSession | None] = None
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._op_lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> BrowserSession:
        """Return the process-wide BrowserSession singleton."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def op_lock(self) -> asyncio.Lock:
        """Operation lock serializing concurrent MCP tool calls against the open browser."""
        return self._op_lock

    @property
    def is_active(self) -> bool:
        """Return True if a browser context and open page are currently active."""
        if self._context is None or self._page is None:
            return False
        return not self._page.is_closed()

    @staticmethod
    def resolve_user_data_dir() -> Path:
        """Resolve the persistent Chromium profile directory (`.compass_browser_profile`)."""
        user_data_dir = resolve_config_path(
            "CAREER_COMPASS_USER_DATA_DIR",
            ".compass_browser_profile",
        )
        user_data_dir.mkdir(parents=True, exist_ok=True)
        return user_data_dir

    @staticmethod
    def _is_headless() -> bool:
        raw = os.getenv("CAREER_COMPASS_HEADLESS", "false").strip().lower()
        return raw in {"1", "true", "yes"}

    async def ensure_page(self) -> Page:
        """Ensure the persistent Chromium context and an active tab are open."""
        async with self._lock:
            if self._playwright is None:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()

            if self._context is not None:
                try:
                    # Probe whether the user manually closed the browser window
                    _ = self._context.pages
                except Exception:
                    logger.info("Persistent browser context was closed externally; relaunching.")
                    self._context = None
                    self._page = None

            if self._context is None:
                user_data_dir = self.resolve_user_data_dir()
                headless = self._is_headless()
                logger.info(
                    "Launching persistent Chromium context at %s (headless=%s)",
                    user_data_dir,
                    headless,
                )
                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(user_data_dir),
                    headless=headless,
                    no_viewport=not headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )

            if self._page is None or self._page.is_closed():
                existing_pages = [p for p in self._context.pages if not p.is_closed()]
                if existing_pages:
                    self._page = existing_pages[-1]
                else:
                    self._page = await self._context.new_page()

            return self._page

    async def get_active_page(self) -> Page:
        """Return the currently open Page or raise RuntimeError if no session is active."""
        if not self.is_active or self._page is None:
            raise RuntimeError(
                "No active browser session. Run stage_form_session(url) first to open a form."
            )
        return self._page

    async def navigate(self, url: str, wait_ms: int = 1200) -> Page:
        """Open `url` in the persistent headed browser window and wait for initial hydration."""
        page = await self.ensure_page()
        logger.info("Navigating active tab to %s", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            logger.debug("Network idle timeout reached for %s; proceeding with DOM ready.", url)
        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000.0)
        try:
            await page.bring_to_front()
        except Exception as exc:
            logger.debug("bring_to_front skipped: %s", exc)
        return page

    @staticmethod
    async def resolve_target_frame(page: Page) -> Page | Frame:
        """Return `#grnhse_iframe` content frame if present on the page, otherwise `page`."""
        try:
            iframe_locator = page.locator("#grnhse_iframe, iframe[src*='greenhouse.io']")
            if await iframe_locator.count() > 0:
                element_handle = await iframe_locator.first.element_handle(timeout=3000)
                if element_handle is not None:
                    frame = await element_handle.content_frame()
                    if frame is not None:
                        try:
                            await frame.wait_for_load_state("domcontentloaded", timeout=5000)
                        except Exception:
                            pass
                        return frame
        except Exception as exc:
            logger.debug("Greenhouse iframe check returned page root: %s", exc)
        return page

    async def close(self) -> None:
        """Cleanly close the persistent browser context and stop Playwright."""
        async with self._lock:
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception as exc:
                    logger.debug("Error while closing BrowserContext: %s", exc)
                finally:
                    self._context = None
                    self._page = None

            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception as exc:
                    logger.debug("Error while stopping Playwright: %s", exc)
                finally:
                    self._playwright = None
