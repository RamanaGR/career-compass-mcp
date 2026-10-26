"""Accessible DOM schema and role description context inspector."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from compass_mcp.models import ControlType, FormControl, HighlightColor

if TYPE_CHECKING:
    from playwright.async_api import Frame, Page

logger = logging.getLogger("compass_mcp.browser.dom_inspector")

_CONSENT_KEYWORDS_RE = re.compile(
    r"\b("
    r"privacy\s+policy|data\s+protection|gdpr|consent|i\s+agree|i\s+acknowledge|"
    r"i\s+consent|i\s+do\s+not\s+consent|ai\s+notetaker|interview\s+recording|"
    r"terms\s+and\s+conditions|terms\s+of\s+service|datenschutz|einwilligung|"
    r"background\s+check\s+authorization|certify\s+that\s+the\s+information"
    r")\b",
    re.IGNORECASE,
)

_SUBMIT_TEXT_RE = re.compile(
    r"^\s*("
    r"submit(\s+application)?|apply(\s+now|\s+for\s+this\s+job)?|"
    r"send\s+application|finish\s+application|bewerbung\s+absenden|absenden"
    r")\s*$",
    re.IGNORECASE,
)


def is_consent_prompt(label: str, options: list[str] | None = None) -> bool:
    """Return True if a label or its options ask for legal/GDPR/privacy/recording consent that must remain human-checked."""
    if _CONSENT_KEYWORDS_RE.search(label or ""):
        return True
    for opt in options or []:
        if _CONSENT_KEYWORDS_RE.search(opt or ""):
            return True
    return False


def is_submit_text(label: str) -> bool:
    """Return True if a button/input label represents a final form submission control."""
    return bool(_SUBMIT_TEXT_RE.search(label or ""))


_EXTRACT_ROLE_CONTEXT_JS = """
() => {
    const prioritySelectors = [
        '[data-qa="job-description"]',
        '[data-testid*="job-description"]',
        '#job-description',
        '.job-description',
        '.posting-requirements',
        '.section-wrapper.page-full-width',
        '#content',
        'article',
        'main',
        'body'
    ];
    const cleanText = (root) => {
        if (!root) return '';
        const clone = root.cloneNode(true);
        const junk = clone.querySelectorAll('script, style, noscript, svg, nav, footer, form');
        junk.forEach((node) => node.remove());
        return (clone.innerText || clone.textContent || '')
            .replace(/\\s+/g, ' ')
            .trim();
    };

    const title = (document.title || '').trim();
    for (const sel of prioritySelectors) {
        const el = document.querySelector(sel);
        if (el) {
            const text = cleanText(el);
            if (text.length >= 160) {
                return (title ? title + ' — ' : '') + text.slice(0, 8000);
            }
        }
    }
    const fallback = cleanText(document.body);
    return ((title ? title + ' — ' : '') + fallback).slice(0, 8000);
}
"""


_INSPECT_CONTROLS_JS = """
(includeAmber) => {
    const isVisible = (el) => {
        if (!el) return false;
        if (el.disabled || el.readOnly) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) {
            return false;
        }
        const rect = el.getBoundingClientRect();
        return rect.width > 2 && rect.height > 2;
    };

    const cssEscape = (val) => {
        if (window.CSS && typeof window.CSS.escape === 'function') {
            return window.CSS.escape(val);
        }
        return String(val).replace(/([ #;?%&,.+*~\\':"!^$[\\]()=>|/@])/g, '\\\\$1');
    };

    const buildSelector = (el, indexHint) => {
        if (el.id) {
            return `#${cssEscape(el.id)}`;
        }
        const tag = el.tagName.toLowerCase();
        const dataTestId = el.getAttribute('data-testid') || el.getAttribute('data-qa');
        if (dataTestId) {
            const attrName = el.hasAttribute('data-testid') ? 'data-testid' : 'data-qa';
            const candidate = `${tag}[${attrName}="${cssEscape(dataTestId)}"]`;
            if (document.querySelectorAll(candidate).length === 1) {
                return candidate;
            }
        }
        if (el.name) {
            const byName = `${tag}[name="${cssEscape(el.name)}"]`;
            if (document.querySelectorAll(byName).length === 1) {
                return byName;
            }
        }
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel) {
            const byAria = `${tag}[aria-label="${cssEscape(ariaLabel)}"]`;
            if (document.querySelectorAll(byAria).length === 1) {
                return byAria;
            }
        }
        if (!el.getAttribute('data-compass-id')) {
            el.setAttribute('data-compass-id', `compass-control-${indexHint}`);
        }
        const compassId = el.getAttribute('data-compass-id');
        return `[data-compass-id="${cssEscape(compassId)}"]`;
    };

    const cleanLabel = (raw) => {
        if (!raw) return '';
        return raw.replace(/\\s+/g, ' ').replace(/\\*+$/g, '').trim();
    };

    const resolveLabel = (el) => {
        if (el.id) {
            const explicit = document.querySelector(`label[for="${cssEscape(el.id)}"]`);
            if (explicit && explicit.innerText) {
                return cleanLabel(explicit.innerText);
            }
        }
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel) {
            return cleanLabel(ariaLabel);
        }
        const labelledBy = el.getAttribute('aria-labelledby');
        if (labelledBy) {
            const parts = labelledBy
                .split(/\\s+/)
                .map((id) => document.getElementById(id))
                .filter(Boolean)
                .map((node) => node.innerText || node.textContent || '');
            const joined = cleanLabel(parts.join(' '));
            if (joined) return joined;
        }
        const parentLabel = el.closest('label');
        if (parentLabel) {
            const clone = parentLabel.cloneNode(true);
            clone.querySelectorAll('input, select, textarea, button').forEach((n) => n.remove());
            const text = cleanLabel(clone.innerText || clone.textContent || '');
            if (text) return text;
        }
        const container = el.closest(
            '.field, .application-question, .form-group, [data-qa*="question"], [class*="field"], fieldset'
        );
        if (container) {
            const labelEl = container.querySelector('legend, label, .application-label, [class*="label"]');
            if (labelEl) {
                const text = cleanLabel(labelEl.innerText || labelEl.textContent || '');
                if (text) return text;
            }
        }
        return cleanLabel(el.getAttribute('placeholder') || el.name || el.id || '');
    };

    const resolveSectionHeading = (el) => {
        const fieldset = el.closest('fieldset');
        if (fieldset) {
            const legend = fieldset.querySelector('legend');
            if (legend) return cleanLabel(legend.innerText || legend.textContent || '');
        }
        const section = el.closest('section, .application-question, .form-section, [ role="group" ]');
        if (section) {
            const heading = section.querySelector('h1, h2, h3, h4, legend');
            if (heading) return cleanLabel(heading.innerText || heading.textContent || '');
        }
        return '';
    };

    const candidates = Array.from(
        document.querySelectorAll(
            'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"]):not([type="password"]), textarea, select, [role="combobox"]'
        )
    );

    const seenRadioGroups = new Set();
    const results = [];
    let indexCounter = 1;

    for (const el of candidates) {
        if (!isVisible(el)) continue;

        const tag = el.tagName.toLowerCase();
        const rawType = (el.getAttribute('type') || '').toLowerCase();
        if (rawType === 'file') continue;

        // Skip honeypots or search/cookie banners outside application context
        const nameAndId = `${el.name || ''} ${el.id || ''} ${el.className || ''} ${el.getAttribute('data-ph-at-id') || ''}`.toLowerCase();
        if (
            nameAndId.includes('honeypot') ||
            nameAndId.includes('captcha') ||
            nameAndId.includes('recaptcha') ||
            nameAndId.includes('typehead') ||
            nameAndId.includes('globalsearch') ||
            nameAndId.includes('phs-keywords') ||
            nameAndId.includes('ph-input-search')
        ) {
            continue;
        }
        if (el.closest('header, nav, [role="search"], .ph-search-group, .job-filter, .phs-cookie-popup-area, #onetrust-banner-sdk')) {
            continue;
        }

        const stagedColor = el.getAttribute('data-compass-staged') || 'none';
        if (stagedColor === 'blue' || stagedColor === 'green') {
            continue;
        }
        if (!includeAmber && stagedColor === 'amber') {
            continue;
        }

        // Handle radio groups as a single logical control
        if (rawType === 'radio') {
            const groupKey = el.name || el.getAttribute('aria-labelledby') || buildSelector(el, indexCounter);
            if (seenRadioGroups.has(groupKey)) continue;
            seenRadioGroups.add(groupKey);

            const radios = el.name
                ? Array.from(document.querySelectorAll(`input[type="radio"][name="${cssEscape(el.name)}"]`))
                : [el];
            const alreadyChecked = radios.some((r) => r.checked);
            if (alreadyChecked) continue;

            const options = radios
                .map((r) => {
                    const rLabel = resolveLabel(r) || r.value;
                    return cleanLabel(rLabel);
                })
                .filter(Boolean);

            const groupContainer = el.closest('fieldset, .application-question, .field, .form-group');
            let groupPrompt = '';
            if (groupContainer) {
                const headerNode = groupContainer.querySelector('legend, .application-label, label, h3, h4');
                if (headerNode) {
                    groupPrompt = cleanLabel(headerNode.innerText || headerNode.textContent || '');
                }
            }
            if (!groupPrompt) {
                groupPrompt = resolveSectionHeading(el) || resolveLabel(el) || el.name || 'Selection';
            }

            results.push({
                selector: buildSelector(el, indexCounter++),
                label: groupPrompt,
                control_type: 'radio',
                required: radios.some((r) => r.required || r.getAttribute('aria-required') === 'true'),
                current_value: '',
                options: Array.from(new Set(options)),
                placeholder: '',
                section_heading: resolveSectionHeading(el),
                highlight: stagedColor
            });
            continue;
        }

        if (rawType === 'checkbox') {
            if (el.checked) continue;
            const label = resolveLabel(el);
            if (!label) continue;
            results.push({
                selector: buildSelector(el, indexCounter++),
                label,
                control_type: 'checkbox',
                required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
                current_value: '',
                options: ['Yes', 'No'],
                placeholder: '',
                section_heading: resolveSectionHeading(el),
                highlight: stagedColor
            });
            continue;
        }

        // Determine control_type & current value
        let controlType = 'text';
        let currentValue = (el.value || '').trim();
        let options = [];

        if (tag === 'textarea') {
            controlType = 'textarea';
        } else if (tag === 'select') {
            controlType = 'select';
            const selectedOpt = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
            const selectedText = selectedOpt ? cleanLabel(selectedOpt.text) : '';
            const isPlaceholderOpt =
                !currentValue ||
                /^(select|choose|please select|--|none)/i.test(selectedText);
            if (!isPlaceholderOpt) {
                continue;
            }
            currentValue = '';
            options = Array.from(el.options || [])
                .filter((opt) => !opt.disabled && opt.value !== '' && !/^(select|choose|please select|--)/i.test(cleanLabel(opt.text)))
                .map((opt) => cleanLabel(opt.text))
                .filter(Boolean);
        } else if (
            el.getAttribute('role') === 'combobox' ||
            el.getAttribute('aria-autocomplete') === 'list' ||
            (el.className && String(el.className).includes('select__input'))
        ) {
            controlType = 'combobox';
            // Check if a React-Select singleValue sibling is already populated
            const selectContainer = el.closest('[class*="select__control"], [class*="Select-control"], [role="combobox"]');
            if (selectContainer) {
                const singleVal = selectContainer.querySelector('[class*="singleValue"], [class*="single-value"]');
                if (singleVal && cleanLabel(singleVal.innerText)) {
                    continue;
                }
            }
            if (currentValue) continue;
        } else if (rawType === 'number') {
            controlType = 'numeric';
            if (currentValue) continue;
        } else if (rawType === 'date' || rawType === 'month') {
            controlType = 'date';
            if (currentValue) continue;
        } else {
            if (currentValue) continue;
        }

        const label = resolveLabel(el);
        if (!label) continue;

        const required = Boolean(
            el.required ||
            el.getAttribute('aria-required') === 'true' ||
            label.includes('*')
        );

        results.push({
            selector: buildSelector(el, indexCounter++),
            label,
            control_type: controlType,
            required,
            current_value: currentValue,
            options,
            placeholder: cleanLabel(el.getAttribute('placeholder') || ''),
            section_heading: resolveSectionHeading(el),
            highlight: stagedColor
        });
    }

    return results;
}
"""


class DOMInspector:
    """Inspects accessible labels, role descriptions, and unfilled form controls."""

    @staticmethod
    async def extract_role_context(target: Page | Frame, fallback_page: Page | None = None) -> str:
        """Extract visible job title and role description text from the active frame or outer page."""
        text = ""
        try:
            text = await target.evaluate(_EXTRACT_ROLE_CONTEXT_JS)
        except Exception as exc:
            logger.debug("Role context extraction on target frame failed: %s", exc)

        if len(text.strip()) < 160 and fallback_page is not None and fallback_page is not target:
            try:
                outer_text = await fallback_page.evaluate(_EXTRACT_ROLE_CONTEXT_JS)
                if len(outer_text.strip()) > len(text.strip()):
                    text = outer_text
            except Exception as exc:
                logger.debug("Role context extraction on outer page failed: %s", exc)

        return (text or "").strip()[:8000]

    @staticmethod
    async def inspect_unfilled_controls(
        target: Page | Frame,
        *,
        include_amber: bool = True,
    ) -> list[FormControl]:
        """Inspect all visible, enabled, unfilled form controls on `target`."""
        try:
            raw_controls: list[dict[str, Any]] = await target.evaluate(
                _INSPECT_CONTROLS_JS,
                include_amber,
            )
        except Exception as exc:
            logger.warning("DOM control inspection failed: %s", exc)
            return []

        controls: list[FormControl] = []
        for item in raw_controls:
            label = str(item.get("label") or "").strip()
            if not label or is_submit_text(label):
                continue

            raw_type = str(item.get("control_type") or "text")
            control_type: ControlType = (
                raw_type  # type: ignore[assignment]
                if raw_type
                in {
                    "text",
                    "textarea",
                    "select",
                    "radio",
                    "checkbox",
                    "combobox",
                    "file",
                    "numeric",
                    "date",
                }
                else "text"
            )
            raw_highlight = str(item.get("highlight") or "none")
            highlight: HighlightColor = (
                raw_highlight  # type: ignore[assignment]
                if raw_highlight in {"blue", "green", "amber", "none"}
                else "none"
            )

            options_list = [str(opt) for opt in item.get("options") or [] if str(opt).strip()]
            consent_flag = is_consent_prompt(label, options_list)
            controls.append(
                FormControl(
                    selector=str(item["selector"]),
                    label=label,
                    control_type=control_type,
                    required=bool(item.get("required", False)),
                    current_value=str(item.get("current_value") or ""),
                    options=options_list,
                    placeholder=str(item.get("placeholder") or ""),
                    section_heading=str(item.get("section_heading") or ""),
                    is_consent_control=consent_flag,
                    highlight=highlight,
                )
            )

        return controls
