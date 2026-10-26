"""FastMCP server entrypoint exposing the 4 interactive form accessibility tools."""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from compass_mcp import configure_stderr_logging
from compass_mcp.adapters import detect_portal_adapter
from compass_mcp.browser.dom_inspector import DOMInspector, is_submit_text
from compass_mcp.browser.pacing import PacingController
from compass_mcp.browser.session import BrowserSession
from compass_mcp.config_loader import QACacheManager, load_user_profile
from compass_mcp.llm.assistant import OllamaAssistant
from compass_mcp.models import FormControl, SessionReport

logger = configure_stderr_logging(logging.INFO)

mcp = FastMCP("career-compass-mcp")


@mcp.tool()
async def stage_form_session(url: str, extra_notes: str = "") -> SessionReport:
    """Open `url` in a persistent headed browser and run the 4-stage assistive prefill workflow.

    Stages:
    1. Standard Profile Staging (`config/profile.yaml` + PDF attachment) -> Blue inline outline.
    2. Accessible DOM Schema & Role Description Extraction.
    3. Local QA Cache (`config/qa_cache.json`) & Closed-World Ollama Mapping.
    4. HITL Visual Review Outlines (Blue / Green / Amber), 22-32s natural pacing floor,
       and smooth scroll to the bottom action area while keeping the browser open.
    """
    session = BrowserSession.get_instance()
    async with session.op_lock:
        pacer = PacingController()
        profile = load_user_profile()
        cache = QACacheManager()
        assistant = OllamaAssistant()

        page = await session.navigate(url)
        adapter = await detect_portal_adapter(url, page)
        await adapter.dismiss_popups(page)

        logger.info("Detected portal layout '%s' for URL %s", adapter.portal_type, url)

        all_staged_standard: list[str] = []
        all_attached_docs: list[str] = []
        all_warnings: list[str] = []
        llm_staged_controls: list[FormControl] = []
        unfilled_controls: list[FormControl] = []
        cache_hits = 0
        ollama_calls = 0

        max_steps = 5
        for step_idx in range(max_steps):
            target = await adapter.resolve_target(page)
            await adapter.dismiss_popups(page)

            # Stage 1: Standard Profile Staging & PDF attachments
            staged_standard, attached_docs, warnings = await adapter.stage_standard_profile(
                target=target,
                profile=profile,
                pacer=pacer,
            )
            for f in staged_standard:
                if f not in all_staged_standard:
                    all_staged_standard.append(f)
            for d in attached_docs:
                if d not in all_attached_docs:
                    all_attached_docs.append(d)
            for w in warnings:
                if w not in all_warnings:
                    all_warnings.append(w)

            # Stage 2: Accessible DOM Schema & Role Description Extraction
            role_context = await DOMInspector.extract_role_context(target, fallback_page=page)
            remaining_controls = await DOMInspector.inspect_unfilled_controls(
                target,
                include_amber=True,
            )

            # Stage 3 & 4: Local QA Cache + Closed-World Ollama Mapping + Inline Visual Outlines
            step_unfilled: list[FormControl] = []
            for control in remaining_controls:
                suggestion, from_cache, called_ollama = await assistant.suggest_for_control(
                    control=control,
                    profile=profile,
                    cache=cache,
                    role_context=role_context,
                    extra_notes=extra_notes,
                )
                if from_cache:
                    cache_hits += 1
                if called_ollama:
                    ollama_calls += 1

                if (
                    suggestion.confidence >= OllamaAssistant.HIGH_CONFIDENCE_THRESHOLD
                    and suggestion.value.strip()
                ):
                    applied = await adapter.apply_value(
                        target=target,
                        control=control,
                        value=suggestion.value,
                        pacer=pacer,
                    )
                    if applied:
                        source_tag = "qa_cache.json" if from_cache else "local Ollama / profile.yaml"
                        tooltip = (
                            f"Career Compass [Green - {source_tag}, confidence={suggestion.confidence:.2f}]: "
                            f"{suggestion.reasoning}"
                        )
                        await adapter.apply_highlight(target, control.selector, "green", tooltip)
                        control.current_value = suggestion.value
                        control.highlight = "green"
                        control.confidence = suggestion.confidence
                        control.rationale = suggestion.reasoning
                        llm_staged_controls.append(control)
                        continue

                # Abstention or low confidence (< 0.75) -> Leave blank and highlight Amber
                amber_reason = (
                    suggestion.reasoning
                    or "Fact not explicitly present in profile.yaml — flagged for manual user input"
                )
                tooltip = f"Career Compass [Amber - Manual Input Required]: {amber_reason}"
                await adapter.apply_highlight(target, control.selector, "amber", tooltip)
                control.highlight = "amber"
                control.confidence = suggestion.confidence
                control.rationale = amber_reason
                step_unfilled.append(control)

            unfilled_controls = step_unfilled

            # If there is a multi-step Continue/Next button (not final Submit), advance to next step
            advanced = await adapter.advance_to_next_step_if_available(page, target, pacer)
            if not advanced:
                break
            logger.info("Proceeding to stage step %d at URL %s", step_idx + 2, page.url)

        # Enforce 22-32s minimum session floor and scroll to the final review area
        await adapter.scroll_to_action_area(page, pacer)

        return SessionReport(
            url=page.url,
            portal_type=adapter.portal_type,
            standard_fields_staged=all_staged_standard,
            documents_attached=all_attached_docs,
            llm_staged_controls=llm_staged_controls,
            unfilled_controls=unfilled_controls,
            cache_hits=cache_hits,
            ollama_calls=ollama_calls,
            elapsed_seconds=round(pacer.elapsed_seconds, 2),
            warnings=all_warnings,
            browser_open=session.is_active,
        )


@mcp.tool()
async def list_unfilled_controls() -> list[FormControl]:
    """Return all remaining blank or Amber-highlighted form controls on the active browser page."""
    session = BrowserSession.get_instance()
    async with session.op_lock:
        page = await session.get_active_page()
        target = await session.resolve_target_frame(page)
        controls = await DOMInspector.inspect_unfilled_controls(target, include_amber=True)
        return controls


@mcp.tool()
async def update_form_control(
    selector: str,
    value: str,
    remember_in_cache: bool = True,
) -> str:
    """Populate a specific control in the open browser window and optionally save it to `config/qa_cache.json`."""
    if not selector.strip():
        raise ValueError("`selector` must be a non-empty CSS or attribute selector.")
    if is_submit_text(value):
        pass

    session = BrowserSession.get_instance()
    async with session.op_lock:
        page = await session.get_active_page()
        adapter = await detect_portal_adapter(page.url, page)
        target = await adapter.resolve_target(page)
        pacer = PacingController(min_session_seconds=0)

        locator = target.locator(selector).first
        if await adapter.is_submit_locator(locator, selector):
            raise ValueError(
                f"Refusing to interact with submit control '{selector}'. "
                "Final form submission must be performed manually by the user."
            )

        # Inspect current unfilled controls to resolve label and control_type if available
        unfilled = await DOMInspector.inspect_unfilled_controls(target, include_amber=True)
        matched_control: FormControl | None = next(
            (ctrl for ctrl in unfilled if ctrl.selector == selector),
            None,
        )

        if matched_control is None:
            # Build a lightweight FormControl descriptor directly from the DOM element
            meta = await locator.evaluate(
                """(el) => {
                    const tag = (el.tagName || '').toLowerCase();
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    let ctype = 'text';
                    if (tag === 'textarea') ctype = 'textarea';
                    else if (tag === 'select') ctype = 'select';
                    else if (role === 'combobox') ctype = 'combobox';
                    else if (type === 'radio') ctype = 'radio';
                    else if (type === 'checkbox') ctype = 'checkbox';
                    else if (type === 'number') ctype = 'numeric';
                    else if (type === 'date') ctype = 'date';
                    const id = el.id;
                    const explicitLabel = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                    const label = (
                        (explicitLabel && explicitLabel.innerText) ||
                        el.getAttribute('aria-label') ||
                        el.getAttribute('placeholder') ||
                        el.name ||
                        el.id ||
                        selector
                    ).trim();
                    return { label, control_type: ctype };
                }"""
            )
            matched_control = FormControl(
                selector=selector,
                label=str(meta.get("label") or selector),
                control_type=meta.get("control_type") or "text",
            )

        success = await adapter.apply_value(
            target=target,
            control=matched_control,
            value=value,
            pacer=pacer,
        )
        if not success:
            return f"Failed to populate control '{matched_control.label}' ({selector})."

        await adapter.apply_highlight(
            target=target,
            selector_or_locator=selector,
            color="green",
            tooltip=f"Career Compass [Green]: Updated via update_form_control ('{matched_control.label}')",
        )

        cached_msg = ""
        if remember_in_cache and matched_control.label:
            cache = QACacheManager()
            cache.store(
                prompt=matched_control.label,
                value=value,
                control_type=matched_control.control_type,
                confidence=1.0,
                source="user_update",
            )
            cached_msg = f" and saved '{matched_control.label}' to config/qa_cache.json"

        return (
            f"Populated '{matched_control.label}' ({selector}) with '{value}'{cached_msg}."
        )


@mcp.tool()
async def close_session() -> str:
    """Cleanly close the persistent Playwright browser window."""
    session = BrowserSession.get_instance()
    async with session.op_lock:
        await session.close()
    return "Closed the persistent Playwright browser session."


def main() -> None:
    """Run the Career Compass FastMCP server over stdio."""
    configure_stderr_logging(logging.INFO)
    logger.info("Starting Career Compass MCP server over stdio transport.")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
