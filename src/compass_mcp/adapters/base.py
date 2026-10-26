"""Abstract BasePortalAdapter with inline CSS outline highlighting and submit denylist guards."""

from __future__ import annotations

from abc import ABC, abstractmethod
from difflib import SequenceMatcher
import logging
import re
from typing import TYPE_CHECKING

from compass_mcp.browser.dom_inspector import is_submit_text
from compass_mcp.browser.session import BrowserSession
from compass_mcp.config_loader import resolve_document_path
from compass_mcp.models import FormControl, HighlightColor, PortalType, UserProfile

if TYPE_CHECKING:
    from playwright.async_api import Frame, Locator, Page

    from compass_mcp.browser.pacing import PacingController

logger = logging.getLogger("compass_mcp.adapters.base")

HIGHLIGHT_HEX: dict[HighlightColor, str] = {
    "blue": "#2563eb",
    "green": "#16a34a",
    "amber": "#d97706",
    "none": "transparent",
}

_SUBMIT_SELECTOR_RE = re.compile(
    r"(type\s*=\s*['\"]?submit['\"]?|button\[type=['\"]?submit|input\[type=['\"]?submit)",
    re.IGNORECASE,
)


class BasePortalAdapter(ABC):
    """Base portal layout adapter implementing standard profile staging and inline HITL highlights."""

    portal_type: PortalType = "generic"

    @classmethod
    @abstractmethod
    async def matches(cls, url: str, page: Page | None = None) -> bool:
        """Return True if `url` or `page` DOM matches this portal layout."""

    async def resolve_target(self, page: Page) -> Page | Frame:
        """Return the interaction target (Page or embedded iframe)."""
        return await BrowserSession.resolve_target_frame(page)

    def standard_field_selectors(self) -> dict[str, list[str]]:
        """Map canonical profile field names to ordered candidate CSS/ARIA selectors."""
        return {
            "first_name": [
                "#first_name",
                "#firstName",
                "input[name='first_name']",
                "input[name='firstName']",
                "input[autocomplete='given-name']",
                "input[aria-label*='First Name' i]:not([id*='preferred' i]):not([aria-label*='Preferred' i])",
            ],
            "last_name": [
                "#last_name",
                "#lastName",
                "input[name='last_name']",
                "input[name='lastName']",
                "input[autocomplete='family-name']",
                "input[aria-label*='Last Name' i]",
            ],
            "full_name": [
                "input[name='name']",
                "input[name='full_name']",
                "input[name='fullName']",
                "input[autocomplete='name']",
                "input[aria-label*='Full Name' i]",
            ],
            "email": [
                "#email",
                "input[type='email']",
                "input[name='email']",
                "input[autocomplete='email']",
                "input[aria-label*='Email' i]",
            ],
            "phone": [
                "#phone",
                "#phoneNumber",
                "input[type='tel']",
                "input[name='phone']",
                "input[name='phoneNumber']",
                "input[autocomplete='tel']",
                "input[autocomplete='tel-national']",
                "input[aria-label*='Phone' i]",
            ],
            "location": [
                "#candidate-location",
                "#job_application_location",
                "input[name='location']",
                "input[aria-label*='Location' i]",
                "input[placeholder*='Location' i]",
                "input[aria-label*='City' i]",
            ],
            "current_company": [
                "input[name='org']",
                "input[name='company']",
                "input[name='current_company']",
                "input[aria-label*='Current Company' i]",
            ],
            "linkedin": [
                "input[name*='linkedin' i]",
                "input[id*='linkedin' i]",
                "input[aria-label*='LinkedIn' i]",
                "input[placeholder*='linkedin.com' i]",
            ],
            "github": [
                "input[name*='github' i]",
                "input[id*='github' i]",
                "input[aria-label*='GitHub' i]",
                "input[placeholder*='github.com' i]",
            ],
            "portfolio": [
                "input[name*='portfolio' i]",
                "input[id*='portfolio' i]",
                "input[aria-label*='Portfolio' i]",
            ],
            "website": [
                "input[name*='website' i]",
                "input[id*='website' i]",
                "input[aria-label*='Website' i]",
            ],
        }

    def file_field_selectors(self) -> dict[str, list[str]]:
        """Map document categories ('resume', 'cover_letter') to file input selectors."""
        return {
            "resume": [
                ".resume-upload-wrapper input[type='file']",
                ".resume-group input[type='file']",
                "#resume-upload-input",
                "input#resume[type='file']",
                "input[name='resume'][type='file']",
                "input[type='file'][id*='resume' i]",
                "input[type='file'][name*='resume' i]",
                "input[type='file'][aria-label*='Resume' i]",
                "input[type='file'][aria-label*='CV' i]",
                "input[type='file']",
            ],
            "cover_letter": [
                "#cover_letter input[type='file']",
                ".additional-attachment-v2 input[type='file']",
                "input#cover_letter[type='file']",
                "input[name='cover_letter'][type='file']",
                "input[type='file'][id*='cover' i]",
                "input[type='file'][name*='cover' i]",
                "input[type='file'][aria-label*='Cover Letter' i]",
            ],
        }

    @staticmethod
    def extract_profile_field_values(profile: UserProfile) -> dict[str, str]:
        """Return canonical standard field values from `UserProfile`."""
        return {
            "first_name": profile.personal.first_name.strip(),
            "last_name": profile.personal.last_name.strip(),
            "full_name": profile.personal.full_name.strip(),
            "email": profile.personal.email.strip(),
            "phone": profile.personal.phone.strip(),
            "location": (profile.personal.location or profile.personal.city).strip(),
            "current_company": profile.experience.current_company.strip(),
            "linkedin": profile.links.linkedin.strip(),
            "github": profile.links.github.strip(),
            "portfolio": profile.links.portfolio.strip(),
            "website": (profile.links.website or profile.links.portfolio).strip(),
        }

    @staticmethod
    async def is_submit_locator(locator: Locator, selector: str = "") -> bool:
        """Strictly guard against clicking any final submit or final application button."""
        try:
            is_sub = await locator.evaluate(
                """(el) => {
                    if (!el) return false;
                    const tag = (el.tagName || '').toLowerCase();
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    const id = (el.id || '').toLowerCase();
                    const cls = String(el.className || '').toLowerCase();
                    const atm = (el.getAttribute('atm-id') || '').toLowerCase();
                    const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
                    // Multi-step wizard Continue/Next navigation buttons are NOT final submit buttons
                    if (
                        /^(continue|next|next step|save & continue|save and continue|proceed|weiter)$/i.test(text) &&
                        !cls.includes('btn-submit') &&
                        atm !== 'submit-button'
                    ) {
                        return false;
                    }
                    if (type === 'submit' || cls.includes('btn-submit') || atm === 'submit-button') return true;
                    if (tag === 'button' || (tag === 'input' && type === 'button')) {
                        return /^(submit|apply|send application|finish application|bewerbung absenden|absenden)/i.test(text);
                    }
                    return false;
                }"""
            )
            return bool(is_sub)
        except Exception:
            return bool(_SUBMIT_SELECTOR_RE.search(selector or ""))

    async def dismiss_popups(self, page: Page | Frame) -> int:
        """Close visible cookie consent banners, chat widgets, and overlay modals."""
        dismiss_selectors = [
            "button[data-ph-at-id='cookie-close-link']",
            "#onetrust-accept-btn-handler",
            "#onetrust-reject-all-handler",
            ".onetrust-close-btn-handler",
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
            "#CybotCookiebotDialogBodyButtonAccept",
            "[data-testid='uc-accept-all-button']",
            "[data-testid='uc-deny-all-button']",
            ".cc-btn.cc-allow",
            ".cc-btn.cc-dismiss",
            ".phs-cookie-popup-area button",
            "[id*='cookie' i] button[class*='accept' i]",
            "[id*='cookie' i] button[class*='close' i]",
            "[class*='cookie-popup' i] button",
            "[class*='cookie-banner' i] button",
            "[role='dialog'] button[aria-label='Close' i]",
            "[aria-modal='true'] button[aria-label='Close' i]",
        ]
        dismissed = 0
        for sel in dismiss_selectors:
            try:
                loc = page.locator(sel)
                count = await loc.count()
                for idx in range(min(count, 4)):
                    btn = loc.nth(idx)
                    if not await btn.is_visible():
                        continue
                    # Never click anything inside the application form itself
                    in_form = await btn.evaluate(
                        """(el) => Boolean(el.closest('form#applyForm, form#application_form, form#application-form, .apply-form-section'))"""
                    )
                    if in_form:
                        continue
                    await btn.click(timeout=2000)
                    dismissed += 1
                    break
            except Exception as exc:
                logger.debug("Popup check skipped for %s: %s", sel, exc)
        return dismissed

    async def apply_highlight(
        self,
        target: Page | Frame,
        selector_or_locator: str | Locator,
        color: HighlightColor,
        tooltip: str,
    ) -> None:
        """Apply non-intrusive inline CSS outline (`2px solid`) and native `title` attribute."""
        hex_color = HIGHLIGHT_HEX.get(color, "#d97706")
        locator = (
            target.locator(selector_or_locator).first
            if isinstance(selector_or_locator, str)
            else selector_or_locator.first
        )
        try:
            await locator.evaluate(
                """(el, payload) => {
                    if (!el) return;
                    const { colorName, hex, titleText } = payload;
                    const applyStyle = (node) => {
                        if (!node || !node.style) return;
                        node.style.outline = `2px solid ${hex}`;
                        node.style.outlineOffset = '2px';
                        node.setAttribute('data-compass-staged', colorName);
                        if (titleText) {
                            node.setAttribute('title', titleText);
                        }
                    };
                    applyStyle(el);

                    // If element is hidden (e.g., <input type="file" style="display:none">), also outline its visible trigger/wrapper
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    if (rect.width < 2 || rect.height < 2 || style.display === 'none' || style.visibility === 'hidden') {
                        const wrapper = el.closest('.resume-group, .resume-upload-wrapper, .attachment-wrapper, label, .file-upload, .form-group');
                        if (wrapper) {
                            const visibleTargets = wrapper.querySelectorAll(
                                '.after-upload, .uploadedFileName, .upload-resume-btn, [atm-id="resume-button"], label.file-label, button'
                            );
                            if (visibleTargets.length > 0) {
                                visibleTargets.forEach((vt) => applyStyle(vt));
                            } else {
                                applyStyle(wrapper);
                            }
                        } else if (el.parentElement) {
                            applyStyle(el.parentElement);
                        }
                    }
                }""",
                {
                    "colorName": color,
                    "hex": hex_color,
                    "titleText": tooltip,
                },
            )
        except Exception as exc:
            logger.debug("Inline highlight skipped for %s: %s", selector_or_locator, exc)

    async def find_first_visible_locator(
        self,
        target: Page | Frame,
        selectors: list[str],
    ) -> tuple[str, Locator] | None:
        """Return the first visible, enabled locator matching any selector in `selectors` outside site search bars."""
        for sel in selectors:
            try:
                loc = target.locator(sel)
                count = await loc.count()
                for idx in range(min(count, 4)):
                    candidate = loc.nth(idx)
                    if not (await candidate.is_visible() and await candidate.is_enabled()):
                        continue
                    is_global_search = await candidate.evaluate(
                        """(el) => {
                            if (!el) return true;
                            const token = `${el.id || ''} ${el.name || ''} ${el.className || ''} ${el.getAttribute('data-ph-at-id') || ''}`.toLowerCase();
                            if (
                                token.includes('typehead') ||
                                token.includes('globalsearch') ||
                                token.includes('phs-keywords') ||
                                token.includes('ph-input-search')
                            ) {
                                return true;
                            }
                            return Boolean(el.closest('header, nav, [role="search"], .ph-search-group, .job-filter'));
                        }"""
                    )
                    if is_global_search:
                        continue
                    return sel, candidate
            except Exception:
                continue
        return None

    async def attach_documents(
        self,
        target: Page | Frame,
        profile: UserProfile,
        pacer: PacingController,
    ) -> tuple[list[str], list[str]]:
        """Attach local PDF files (`resume_path`, `cover_letter_path`) if present on disk."""
        attached: list[str] = []
        warnings: list[str] = []
        selectors_map = self.file_field_selectors()

        doc_specs = [
            ("resume", profile.documents.resume_path, True),
            ("cover_letter", profile.documents.cover_letter_path, False),
        ]

        for doc_key, configured_path, is_primary in doc_specs:
            if not configured_path:
                continue
            resolved = resolve_document_path(configured_path)
            if resolved is None:
                if is_primary:
                    warnings.append(
                        f"Configured {doc_key} file not found at '{configured_path}'. "
                        "Place your PDF in assets/ or attach it manually before submitting."
                    )
                continue

            for sel in selectors_map.get(doc_key, []):
                try:
                    loc = target.locator(sel)
                    if await loc.count() == 0:
                        continue
                    file_input = loc.first
                    # Ensure we don't accidentally attach resume into cover_letter input
                    if doc_key == "resume":
                        is_cover_input = await file_input.evaluate(
                            """(el) => Boolean(el.closest('#cover_letter, .additional-attachment-v2'))"""
                        )
                        if is_cover_input:
                            continue

                    logger.info("Attaching %s (%s) via selector '%s'", doc_key, resolved.name, sel)
                    await file_input.set_input_files(str(resolved))
                    try:
                        await file_input.evaluate(
                            """(el) => {
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                            }"""
                        )
                    except Exception:
                        pass

                    # Wait for portal upload/resume parser (e.g. Phenom .after-upload / #resumeName) to settle
                    if doc_key == "resume":
                        await pacer.sleep_range(2.2, 3.2)
                    else:
                        await pacer.sleep_range(0.8, 1.4)

                    await self.apply_highlight(
                        target,
                        file_input,
                        "blue",
                        f"Career Compass [Blue]: Attached {resolved.name} from profile.yaml",
                    )
                    attached.append(f"{doc_key}:{resolved.name}")
                    break
                except Exception as exc:
                    logger.debug("Could not attach %s on selector %s: %s", doc_key, sel, exc)

        return attached, warnings

    async def stage_standard_profile(
        self,
        target: Page | Frame,
        profile: UserProfile,
        pacer: PacingController,
    ) -> tuple[list[str], list[str], list[str]]:
        """Attach local PDF documents first, then populate standard identity/contact/link fields."""
        staged_fields: list[str] = []
        warnings: list[str] = []

        # Attach documents first so any portal resume parser finishes before we set canonical profile values
        attached_docs, doc_warnings = await self.attach_documents(target, profile, pacer)
        warnings.extend(doc_warnings)

        field_values = self.extract_profile_field_values(profile)
        selector_map = self.standard_field_selectors()

        # Check if separate first_name / last_name inputs exist so we don't double-fill full_name
        has_split_name = (
            await self.find_first_visible_locator(target, selector_map["first_name"]) is not None
            and await self.find_first_visible_locator(target, selector_map["last_name"]) is not None
        )

        for field_name, selectors in selector_map.items():
            if field_name == "full_name" and has_split_name:
                continue

            found = await self.find_first_visible_locator(target, selectors)
            if found is None:
                continue
            matched_selector, locator = found

            # Skip if already staged by us in this step
            try:
                already_staged = await locator.get_attribute("data-compass-staged")
                if already_staged in ("blue", "green"):
                    continue
            except Exception:
                pass

            value = field_values.get(field_name, "")
            if not value:
                # Fact is intentionally blank in profile.yaml (e.g. phone, portfolio).
                # Highlight in Amber if present on form so DOMInspector / user sees it.
                await self.apply_highlight(
                    target,
                    locator,
                    "amber",
                    f"Career Compass [Amber]: '{field_name}' is empty in profile.yaml — manual input or QA cache check needed.",
                )
                continue

            # If resume upload already auto-populated the exact value (or phone with country prefix), avoid redundant re-typing if identical
            current_val = ""
            try:
                current_val = (await locator.input_value(timeout=1500) or "").strip()
            except Exception:
                pass

            if current_val and (
                current_val.lower() == value.strip().lower()
                or (field_name == "phone" and current_val.endswith(value.strip()))
            ):
                await self.apply_highlight(
                    target,
                    locator,
                    "blue",
                    f"Career Compass [Blue]: Verified '{field_name}' from config/profile.yaml",
                )
                staged_fields.append(field_name)
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

    async def advance_to_next_step_if_available(
        self,
        page: Page,
        target: Page | Frame,
        pacer: PacingController,
    ) -> bool:
        """Click a multi-step 'Continue' / 'Next' button if present, strictly never clicking final Submit."""
        next_selectors = [
            "button#next",
            "button.btn-next",
            "button[aria-label='Continue' i]",
            "button[aria-label='Next' i]",
            "button:has-text('Continue')",
            "button:has-text('Next')",
            "button:has-text('Save & Continue')",
            "input[type='button'][value='Continue' i]",
            "input[type='submit'][value='Continue' i]",
        ]
        for sel in next_selectors:
            try:
                loc = target.locator(sel)
                count = await loc.count()
                for idx in range(min(count, 4)):
                    btn = loc.nth(idx)
                    if not (await btn.is_visible() and await btn.is_enabled()):
                        continue
                    is_valid_next = await btn.evaluate(
                        """(el) => {
                            if (!el) return false;
                            const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
                            const cls = String(el.className || '').toLowerCase();
                            const atm = (el.getAttribute('atm-id') || '').toLowerCase();
                            if (cls.includes('btn-submit') || atm === 'submit-button') return false;
                            if (/submit|apply|finish|send application|absenden/i.test(text)) return false;
                            return /^(continue|next|next step|save & continue|save and continue|proceed|weiter)$/i.test(text);
                        }"""
                    )
                    if not is_valid_next:
                        continue

                    prev_url = page.url
                    prev_sig = await target.evaluate(
                        """() => Array.from(document.querySelectorAll('input:not([type="hidden"]), select, textarea')).map(e => e.id || e.name).join(',')"""
                    )

                    logger.info("Clicking multi-step navigation button ('%s') to advance form step.", sel)
                    await pacer.smooth_scroll_into_view(btn)
                    await btn.click(timeout=4000)
                    await pacer.sleep_range(2.2, 3.4)
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass

                    await self.dismiss_popups(page)

                    new_url = page.url
                    new_sig = await target.evaluate(
                        """() => Array.from(document.querySelectorAll('input:not([type="hidden"]), select, textarea')).map(e => e.id || e.name).join(',')"""
                    )
                    if new_url != prev_url or new_sig != prev_sig:
                        logger.info("Advanced to next form step: %s", new_url)
                        return True
            except Exception as exc:
                logger.debug("Next-step check skipped for %s: %s", sel, exc)
        return False

    async def fill_standard_locator(
        self,
        target: Page | Frame,
        field_name: str,
        selector: str,
        locator: Locator,
        value: str,
        pacer: PacingController,
    ) -> bool:
        """Fill a standard profile field locator, handling comboboxes when encountered."""
        try:
            role = (await locator.get_attribute("role") or "").lower()
            aria_auto = (await locator.get_attribute("aria-autocomplete") or "").lower()
            if role == "combobox" or aria_auto == "list":
                return await self.fill_combobox_locator(target, locator, value, pacer)
        except Exception:
            pass
        return await pacer.paced_fill(locator, value)

    async def fill_combobox_locator(
        self,
        target: Page | Frame,
        locator: Locator,
        value: str,
        pacer: PacingController,
        wait_ms: int = 600,
    ) -> bool:
        """Populate an `input[role='combobox']` / React-Select control and click the matching option."""
        typed = await pacer.paced_fill(locator, value, apply_inter_field_delay=False)
        if not typed:
            return False

        await pacer.sleep_range(wait_ms / 1000.0, (wait_ms + 250) / 1000.0)

        option_selectors = [
            "[role='listbox'] [role='option']",
            "[id*='react-select'][id*='-option-']",
            ".select__option",
            "[role='option']",
        ]
        value_lower = value.strip().lower()
        primary_token = value_lower.split(",")[0].strip()

        for opt_sel in option_selectors:
            try:
                opts = target.locator(opt_sel)
                count = await opts.count()
                if count == 0:
                    continue

                best_idx = -1
                best_score = 0.0
                for idx in range(min(count, 15)):
                    opt_loc = opts.nth(idx)
                    if not await opt_loc.is_visible():
                        continue
                    opt_text = (await opt_loc.inner_text() or "").strip()
                    opt_lower = opt_text.lower()
                    if not opt_lower:
                        continue
                    if opt_lower == value_lower or (primary_token and primary_token in opt_lower):
                        best_idx = idx
                        best_score = 1.0
                        break
                    score = SequenceMatcher(None, value_lower, opt_lower).ratio()
                    if score > best_score:
                        best_score = score
                        best_idx = idx

                if best_idx >= 0 and best_score >= 0.55:
                    candidate_opt = opts.nth(best_idx)
                    if not await self.is_submit_locator(candidate_opt):
                        await candidate_opt.click(timeout=3000)
                        await pacer.inter_field_delay()
                        return True
            except Exception as exc:
                logger.debug("Combobox option lookup on %s failed: %s", opt_sel, exc)

        await pacer.inter_field_delay()
        return True

    async def apply_value(
        self,
        target: Page | Frame,
        control: FormControl,
        value: str,
        pacer: PacingController,
    ) -> bool:
        """Populate any `FormControl` using human-cadence pacing while blocking submit clicks."""
        if not value.strip():
            return False
        if is_submit_text(control.label):
            logger.warning("Blocked attempt to interact with submit control '%s'", control.label)
            return False

        locator = target.locator(control.selector).first
        if await self.is_submit_locator(locator, control.selector):
            logger.warning("Blocked interaction with submit selector '%s'", control.selector)
            return False

        ctype = control.control_type
        try:
            if ctype == "select":
                await pacer.smooth_scroll_into_view(locator)
                try:
                    await locator.select_option(label=value.strip(), timeout=3500)
                except Exception:
                    await locator.select_option(value=value.strip(), timeout=3500)
                await pacer.inter_field_delay()
                return True

            if ctype == "combobox":
                return await self.fill_combobox_locator(target, locator, value, pacer)

            if ctype == "radio":
                return await self._select_radio_option(target, locator, control, value, pacer)

            if ctype == "checkbox":
                normalized = value.strip().lower()
                if normalized in {"yes", "true", "1", "check", "checked"}:
                    await pacer.smooth_scroll_into_view(locator)
                    if not await locator.is_checked():
                        await locator.check(timeout=3500)
                    await pacer.inter_field_delay()
                    return True
                return False

            return await pacer.paced_fill(locator, value)
        except Exception as exc:
            logger.warning(
                "Failed to apply value to control '%s' (%s): %s",
                control.label,
                control.selector,
                exc,
            )
            return False

    async def _select_radio_option(
        self,
        target: Page | Frame,
        locator: Locator,
        control: FormControl,
        value: str,
        pacer: PacingController,
    ) -> bool:
        """Find and check the matching radio button option within a radio group."""
        await pacer.smooth_scroll_into_view(locator)
        desired = value.strip().lower()
        clicked = await target.evaluate(
            """(payload) => {
                const { selector, desiredValue } = payload;
                const base = document.querySelector(selector);
                if (!base) return false;
                const name = base.getAttribute('name');
                const radios = name
                    ? Array.from(document.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`))
                    : [base];
                for (const r of radios) {
                    const id = r.id;
                    const explicit = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                    const parentLabel = r.closest('label');
                    const labelText = (
                        (explicit && explicit.innerText) ||
                        (parentLabel && parentLabel.innerText) ||
                        r.value ||
                        ''
                    ).trim().toLowerCase();
                    if (
                        labelText === desiredValue ||
                        (desiredValue === 'yes' && labelText.startsWith('yes')) ||
                        (desiredValue === 'no' && labelText.startsWith('no')) ||
                        (r.value || '').trim().toLowerCase() === desiredValue
                    ) {
                        r.click();
                        r.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                }
                return false;
            }""",
            {"selector": control.selector, "desiredValue": desired},
        )
        if clicked:
            await pacer.inter_field_delay()
        return bool(clicked)

    async def scroll_to_action_area(self, page: Page | Frame, pacer: PacingController) -> None:
        """Scroll smoothly to the bottom action area of the form without clicking submit."""
        await pacer.enforce_session_floor(page)
        try:
            await page.evaluate(
                """() => {
                    const submitCandidate = document.querySelector(
                        'button[type="submit"], input[type="submit"], form button:last-of-type'
                    );
                    if (submitCandidate && typeof submitCandidate.scrollIntoView === 'function') {
                        submitCandidate.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        return;
                    }
                    const doc = document.scrollingElement || document.documentElement || document.body;
                    if (doc) {
                        window.scrollTo({ top: Math.max(0, doc.scrollHeight - window.innerHeight), behavior: 'smooth' });
                    }
                }"""
            )
        except Exception as exc:
            logger.debug("Final scroll to action area skipped: %s", exc)
