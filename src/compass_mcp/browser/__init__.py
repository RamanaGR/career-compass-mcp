"""Browser session, human-cadence pacing, and accessible DOM inspection modules."""

from compass_mcp.browser.dom_inspector import DOMInspector
from compass_mcp.browser.pacing import PacingController
from compass_mcp.browser.session import BrowserSession

__all__ = ["BrowserSession", "DOMInspector", "PacingController"]
