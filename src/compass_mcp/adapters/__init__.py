"""Portal layout adapter registry and URL/DOM detection factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from compass_mcp.adapters.ashby import AshbyAdapter
from compass_mcp.adapters.base import BasePortalAdapter
from compass_mcp.adapters.greenhouse import GreenhouseAdapter
from compass_mcp.adapters.lever import LeverAdapter
from compass_mcp.adapters.personio import PersonioAdapter
from compass_mcp.models import PortalType

if TYPE_CHECKING:
    from playwright.async_api import Page


class GenericPortalAdapter(BasePortalAdapter):
    """Fallback adapter for arbitrary career portals using accessible label/attribute selectors."""

    portal_type: PortalType = "generic"

    @classmethod
    async def matches(cls, url: str, page: Page | None = None) -> bool:
        return True


ADAPTER_CLASSES: tuple[type[BasePortalAdapter], ...] = (
    GreenhouseAdapter,
    LeverAdapter,
    AshbyAdapter,
    PersonioAdapter,
)


async def detect_portal_adapter(url: str, page: Page | None = None) -> BasePortalAdapter:
    """Detect and instantiate the matching portal layout adapter for `url` and optional `page`."""
    for adapter_cls in ADAPTER_CLASSES:
        if await adapter_cls.matches(url, page):
            return adapter_cls()
    return GenericPortalAdapter()


__all__ = [
    "ADAPTER_CLASSES",
    "AshbyAdapter",
    "BasePortalAdapter",
    "GenericPortalAdapter",
    "GreenhouseAdapter",
    "LeverAdapter",
    "PersonioAdapter",
    "detect_portal_adapter",
]
