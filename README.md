# Career Compass MCP (`career-compass-mcp`)

A privacy-first **Interactive Web Form Accessibility & Prefill Assistant** built as a Python **Model Context Protocol (MCP) Server** (`FastMCP`, `playwright`, `pydantic` v2, `pyyaml`, and `httpx`).

Like a local password manager or autofill utility, `career-compass-mcp` helps you pre-stage your local YAML profile (`config/profile.yaml`) and PDF documents (`assets/`) into web forms across **Greenhouse** (classic `#grnhse_iframe` and modern React-Select `job-boards.greenhouse.io`), **Lever**, **Ashby**, and **Personio** portals while keeping you in 100% control of reviewing and manually submitting the form.

---

## Core Design Principles

1. **Strict Zero-Hallucination & Closed-World Grounding:**
   - `config/profile.yaml` and `config/qa_cache.json` are the **only** source of truth.
   - If a form asks about any skill, certification, clearance, phone number, notice period, availability date, salary expectation, or demographic question not explicitly present in `profile.yaml` or `qa_cache.json`, the assistant **never guesses**. It abstains (`confidence: 0.0`, `value: ""`) and highlights the field in **Amber** (`#d97706`) for your manual input.
2. **Strict Human-in-the-Loop (HITL) Control:**
   - The assistant **never** clicks `input[type="submit"]`, `button[type="submit"]`, or any final submission button.
   - It leaves the persistent headed Chromium window open and scrolled to the bottom review area so you can inspect every field and click submit yourself.
3. **Human-Cadence Interaction Pacing (`PacingController`):**
   - Text inputs are scrolled into view, focused, and typed sequentially (`35–85ms` per keystroke with word-boundary pauses and `0.8–1.8s` inter-field blur settle delays) so React/Vue state listeners never drop events.
   - Enforces a configurable minimum session floor (`CAREER_COMPASS_MIN_SESSION_SECONDS=25`, randomized `22.0s–32.0s` window) with smooth viewport scrolling.
4. **Non-Intrusive Visual Highlighting:**
   - Uses inline CSS outlines (`element.style.outline = "2px solid <hex>"`, `element.style.outlineOffset = "2px"`) and native `title` tooltips on existing controls without injecting foreign DOM banners:
     - **Blue (`#2563eb`):** Standard profile data from `config/profile.yaml`
     - **Green (`#16a34a`):** High-confidence (`>= 0.75`) cached or local Ollama suggestion
     - **Amber (`#d97706`):** Low-confidence (`< 0.75`) or missing profile fact left blank for manual input

---

## 1. What to Change Before Running (Pre-Flight Checklist)

Before running the MCP server for the first time, review and update these 3 items:

### A. Match Your PDF Filename in `config/profile.yaml`
Check the `documents:` section in [`config/profile.yaml`](config/profile.yaml) (lines 18–20).
- Since your CV is saved at `assets/Ramana_Gangarao_CV.pdf`, either update `resume_path` in `config/profile.yaml`:
  ```yaml
  documents:
    resume_path: "assets/Ramana_Gangarao_CV.pdf"
    cover_letter_path: "assets/cover_letter.pdf" # Optional: leave as-is if you don't attach a cover letter
  ```
  **or** copy/rename your file to `assets/resume.pdf`:
  ```bash
  cp assets/Ramana_Gangarao_CV.pdf assets/resume.pdf
  ```

### B. Review Optional Fields in `config/profile.yaml`
In [`config/profile.yaml`](config/profile.yaml), the following fields are currently set to `""` (empty string) on purpose so the tool never invents values:
- `personal.phone`: `""` — Add your phone number (e.g., `"+91-XXXXXXXXXX"`) if you want it auto-filled in **Blue** on every form, or leave it `""` to fill/cache it via `update_form_control`.
- `links.portfolio` / `links.website`: `""` — Add a URL if applicable, or leave `""`.
- `preferences.notice_period`: `""` — Leave `""` to answer once via `update_form_control(..., remember_in_cache=True)` (which saves to `config/qa_cache.json`), or set a default such as `"30 days"` / `"Immediate"`.
- `preferences.availability_date`: `""` — Leave `""` to answer/cache interactively, or set a date (e.g., `"2026-11-15"`).
- `preferences.salary_expectations`: `""` — Leave `""` so compensation fields are highlighted in **Amber** for role-specific manual entry or caching.

### C. Configure `.env` (Optional)
If you want to customize the local Ollama model (for example `llama3.2` instead of `qwen2.5-coder:7b`) or adjust the minimum session pacing floor, copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Key environment variables:
- `OLLAMA_BASE_URL=http://localhost:11434`
- `OLLAMA_MODEL=qwen2.5-coder:7b` (change to any local model shown in `ollama list`)
- `CAREER_COMPASS_MIN_SESSION_SECONDS=25` (minimum natural pacing window; randomized `22–32s`)
- `CAREER_COMPASS_USER_DATA_DIR=.compass_browser_profile` (persistent Chromium profile directory)

---

## 2. How to Install & Run

### Step 1: Set Up the Python Environment & Playwright Chromium
From the repository root (`/Users/ram_surya/Cursor-Projects/career-compass-mcp`):

```bash
# 1. Create and activate a Python 3.11+ virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install the package in editable mode with dev dependencies
pip install -e ".[dev]"

# 3. Install the Playwright Chromium browser binary
playwright install chromium
```

### Step 2: Start Local Ollama & Pull Your Model
Make sure the Ollama daemon is running locally and has the configured model downloaded:

```bash
# Start Ollama if it is not already running in the background
ollama serve

# In another terminal, pull the default model (or the model you set in OLLAMA_MODEL)
ollama pull qwen2.5-coder:7b
```

### Step 3: Verify Setup with the Unit Test Suite
```bash
pytest -v
```

### Step 4: Connect to Cursor (or Claude Desktop) via MCP
Add `career-compass-mcp` to your Cursor MCP configuration at `.cursor/mcp.json` (project-level) or `~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "career-compass-mcp": {
      "command": "/Users/ram_surya/Cursor-Projects/career-compass-mcp/.venv/bin/python",
      "args": ["-m", "compass_mcp.server"],
      "env": {
        "OLLAMA_BASE_URL": "http://localhost:11434",
        "OLLAMA_MODEL": "qwen2.5-coder:7b",
        "CAREER_COMPASS_MIN_SESSION_SECONDS": "25"
      }
    }
  }
}
```
After saving `.cursor/mcp.json`, open **Cursor Settings -> MCP** and verify that `career-compass-mcp` shows a green status indicator with all 4 tools loaded.

### Optional: Run Standalone or Inspect with MCP Inspector
- **Run directly over stdio (for sanity check):**
  ```bash
  source .venv/bin/activate
  career-compass-mcp
  # or: python -m compass_mcp.server
  ```
- **Test interactively in the official MCP Inspector UI:**
  ```bash
  npx -y @modelcontextprotocol/inspector /Users/ram_surya/Cursor-Projects/career-compass-mcp/.venv/bin/python -m compass_mcp.server
  ```

---

## 3. How to Use the 4 Assistive MCP Tools in Chat

Once `career-compass-mcp` is enabled in Cursor, you can prompt the agent in chat:

1. **Stage a Job Application Form (`stage_form_session`):**
   > *"Use `stage_form_session` on `https://job-boards.greenhouse.io/<company>/jobs/<id>`"*
   - Opens a headed Chromium window (using `.compass_browser_profile`), detects the portal layout, attaches your resume PDF, types your `profile.yaml` fields with human cadence (**Blue** outline), fills high-confidence cached/Ollama fields (**Green** outline), and flags any missing or unverified fields in **Amber** (`#d97706`).
2. **Inspect Remaining Blank / Amber Controls (`list_unfilled_controls`):**
   > *"Call `list_unfilled_controls` and show me what needs my input."*
   - Returns the exact list of Amber/unfilled fields (for example: phone number, notice period, salary expectation, or a custom question) along with their CSS selectors and available dropdown options.
3. **Fill & Cache Specific Answers (`update_form_control`):**
   > *"Update `#phone` to `+91-...` and update the notice period field to `30 days`, and remember them in cache."*
   - Calls `update_form_control(selector, value, remember_in_cache=True)` to type the answer into the open browser window, turns the field outline **Green**, and saves the prompt/answer pair to `config/qa_cache.json` so future forms auto-fill it automatically.
4. **Manual Final Review & Submit (Human-in-the-Loop):**
   - Switch to the open Chromium window, verify all **Blue**, **Green**, and **Amber** outlined fields, check any required GDPR/privacy consent boxes, and **click the Submit button yourself**.
5. **Close Browser Session (`close_session`):**
   > *"Call `close_session` to close the browser window."*

---

## MCP Tools Reference

| Tool | Description |
| :--- | :--- |
| `stage_form_session(url: str, extra_notes: str = "") -> SessionReport` | Opens `url` in the persistent headed Chromium window, detects the portal layout, stages standard profile fields + PDFs, inspects remaining controls, maps answers via `qa_cache.json` and local Ollama, applies inline Blue/Green/Amber outlines, and scrolls to the bottom review area. |
| `list_unfilled_controls() -> list[FormControl]` | Lists all remaining blank or Amber-highlighted form controls on the active page. |
| `update_form_control(selector: str, value: str, remember_in_cache: bool = True) -> str` | Populates a specific control in the open browser window with human-cadence typing, highlights it Green, and saves the prompt/answer pair to `config/qa_cache.json`. |
| `close_session() -> str` | Cleanly closes the persistent Playwright browser session. |
