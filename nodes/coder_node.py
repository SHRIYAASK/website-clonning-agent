"""
nodes/coder_node.py — LangGraph Node 4

Calls DeepSeek directly via api.deepseek.com using DEEPSEEK_API_KEY.

Two-step pipeline per section:

  Step 1 — deepseek-reasoner
    Analyses HTML + CSS → structured layout plan
    (exact colors, spacing, fonts, hover states, breakpoints)

  Step 2 — deepseek-chat
    Receives HTML + CSS + layout plan → pixel-perfect TSX component

Robustness improvements (fixes issues #1, 2, 8, 9):
  - Every generated TSX is validated before being accepted.
  - Failed or invalid sections are re-queued and retried automatically.
  - Exponential backoff with jitter handles DNS, timeout, and rate-limit errors.
  - A section is only marked done when valid TSX exceeding MIN_TSX_LENGTH is returned.
  - After MAX_SECTION_RETRIES exhausted attempts, the pipeline aborts rather
    than silently accepting a placeholder.

Input  (from state):
  sections   – list of {name, html_slice, scoped_css, assets}
  global_css – @font-face / :root / body resets
  page_title – original page title

Output (added to state):
  components      – dict[section_name → tsx_string]  (only valid components)
  failed_sections – list of section names that could not be generated
  coder_retry_rounds – how many re-queue passes were needed
"""

import os
import re
import time
import random
import socket
import requests

from state import ClonerState
from nodes.validation import validate_tsx, MIN_TSX_LENGTH


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")

REASONING_MODEL = "deepseek-reasoner"
CODER_MODEL     = "deepseek-chat"

REASONING_MAX_TOKENS = 4096
CODER_MAX_TOKENS     = 16_000

REASONING_TEMP = 0.6
CODER_TEMP     = 0.15

# Per-call retry settings (exponential backoff)
MAX_CALL_RETRIES  = 5
BASE_RETRY_WAIT   = 10   # seconds — doubles each attempt
MAX_RETRY_WAIT    = 120  # seconds cap
JITTER_RANGE      = 5    # ± seconds of random jitter

# Per-section retry settings (re-queue after validation failure)
MAX_SECTION_RETRIES = 3

MIN_CALL_INTERVAL = 5    # seconds between section starts

# ── Budget limits per section ────────────────────────────────────────────────
MAX_HTML_PER_SECTION = 24_000
MAX_CSS_PER_SECTION  = 20_000

# Output dir for intermediate caching
COMP_CACHE_DIR = os.path.join("nextjs-output", "app", "components")


# ─────────────────────────────────────────────────────────────────────────────
# EXPONENTIAL BACKOFF HTTP HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _wait(attempt: int, reason: str = ""):
    """Sleep with exponential backoff + jitter."""
    wait = min(BASE_RETRY_WAIT * (2 ** attempt), MAX_RETRY_WAIT)
    wait += random.uniform(-JITTER_RANGE, JITTER_RANGE)
    wait = max(wait, 1)
    msg = f"    ⏳ Waiting {wait:.1f}s"
    if reason:
        msg += f" ({reason})"
    print(msg)
    time.sleep(wait)


def _deepseek_call(
    messages: list[dict],
    model: str,
    max_tokens: int,
    temperature: float,
    label: str,
    return_reasoning: bool = False,
) -> str:
    """
    POST to DeepSeek with full exponential backoff.

    Handles:
      - 429 rate limits
      - 5xx server errors
      - DNS / socket failures
      - Request timeouts
    """
    payload: dict = {
        "model":       model,
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type":  "application/json",
    }

    last_exc: Exception | None = None

    for attempt in range(MAX_CALL_RETRIES):
        try:
            r = requests.post(
                DEEPSEEK_URL,
                json=payload,
                headers=headers,
                timeout=180,
            )
            print(f"    📡 [{label}] HTTP {r.status_code}  (attempt {attempt + 1})")

            if r.status_code == 429:
                _wait(attempt, "rate limited")
                continue

            if r.status_code >= 500:
                _wait(attempt, f"server error {r.status_code}")
                continue

            if r.status_code == 401:
                raise Exception(f"[{label}] Authentication failed — check DEEPSEEK_API_KEY")

            if r.status_code != 200:
                raise Exception(f"[{label}] DeepSeek {r.status_code}: {r.text[:400]}")

            data   = r.json()
            choice = data["choices"][0]
            msg    = choice["message"]

            finish_reason = choice.get("finish_reason", "?")
            usage         = data.get("usage", {})
            print(
                f"    ℹ️  finish_reason={finish_reason}  "
                f"tokens: prompt={usage.get('prompt_tokens','?')} "
                f"completion={usage.get('completion_tokens','?')}"
            )

            if finish_reason == "length":
                print(
                    f"    ⚠️  WARNING: output TRUNCATED — hit max_tokens={max_tokens}! "
                    f"Consider raising CODER_MAX_TOKENS or splitting this section further."
                )

            reasoning = msg.get("reasoning_content") or ""
            content   = msg.get("content") or ""
            content   = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

            return reasoning if (return_reasoning and reasoning) else content

        except (socket.gaierror, socket.timeout) as exc:
            last_exc = exc
            print(f"    🌐 DNS/socket error on attempt {attempt + 1}: {exc}")
            _wait(attempt, "network error")

        except requests.Timeout as exc:
            last_exc = exc
            print(f"    ⏱️  Request timeout on attempt {attempt + 1}")
            _wait(attempt, "timeout")

        except requests.ConnectionError as exc:
            last_exc = exc
            print(f"    🔌 Connection error on attempt {attempt + 1}: {exc}")
            _wait(attempt, "connection error")

    raise Exception(
        f"[{label}] All {MAX_CALL_RETRIES} attempts failed. Last error: {last_exc}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — REASONING PLAN
# ─────────────────────────────────────────────────────────────────────────────

_REASONING_SYSTEM = """\
You are a senior frontend layout analyst.
Study the HTML section and its CSS, then output a concise structured layout plan
that a React engineer will use verbatim to build the component.

Cover every point below. Use exact values from the CSS — never approximate.

1. LAYOUT      flex/grid direction · alignment · gap · padding · margin (px/rem)
2. TYPOGRAPHY  font-family · size · weight · line-height · color (hex) per element type
3. COLORS      background · border · box-shadow (hex / rgba) for each element
4. IMAGES      each image: local path · alt text · width · height · object-fit
5. HOVER       element selector → property → before/after values
6. RESPONSIVE  each breakpoint (px) → which layout/size/display rules change
7. SPECIAL     transitions · animations · z-index · position · overflow

Do NOT write any code. Output the plan only.
"""


def _step1_reasoning(section: dict, global_css: str) -> str:
    name       = section["name"]
    html_slice = section["html_slice"][:MAX_HTML_PER_SECTION]
    scoped_css = section["scoped_css"][:MAX_CSS_PER_SECTION]

    if len(section["html_slice"]) > MAX_HTML_PER_SECTION:
        print(
            f"    ⚠️  [{name}] HTML truncated: "
            f"{len(section['html_slice'])}ch → {MAX_HTML_PER_SECTION}ch"
        )
    if len(section["scoped_css"]) > MAX_CSS_PER_SECTION:
        print(
            f"    ⚠️  [{name}] CSS truncated: "
            f"{len(section['scoped_css'])}ch → {MAX_CSS_PER_SECTION}ch"
        )

    user = f"""
Section: {name}

=== GLOBAL CSS VARIABLES ===
{global_css[:1200]}

=== SCOPED CSS ===
{scoped_css or "(none — infer from HTML attributes)"}

=== HTML SLICE ===
{html_slice}
""".strip()

    messages = [
        {"role": "system", "content": _REASONING_SYSTEM},
        {"role": "user",   "content": user},
    ]

    return _deepseek_call(
        messages,
        model=REASONING_MODEL,
        max_tokens=REASONING_MAX_TOKENS,
        temperature=REASONING_TEMP,
        label=f"Reason/{name}",
        return_reasoning=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — TSX CODE GENERATION
# ─────────────────────────────────────────────────────────────────────────────

_CODER_SYSTEM = """\
You are an elite React / Next.js frontend engineer.
Convert the HTML section into a pixel-perfect TSX component using the layout plan provided.

STRICT RULES:
1. Output ONLY valid TSX — no explanation, no markdown fences.
2. First two lines must always be exactly:
   'use client';
   import React from 'react';
3. Named default export matching the component name exactly.
4. Preserve ALL text content verbatim — every word, every link, every label.
5. Reproduce EVERY element in the HTML — do NOT skip, collapse, summarise,
   or replace any part with a comment like "// more items here" or "...".
   If the HTML has 12 cards, your TSX must have 12 cards. No exceptions.
6. ALL styling via inline style={{ }} — no Tailwind, no className CSS.
   The layout plan gives you exact values — use them without modification.
7. Image src: use the exact local path given (e.g. ./assets/images/foo.jpg).
8. Never invent assets. Never use data: URIs or placeholder URLs.
9. Responsive breakpoints: implement via useEffect + useState(windowWidth).
10. Hover states: onMouseEnter / onMouseLeave + useState, exact values from plan.
11. All href links: copy exactly from the HTML — no changes.
12. Output the ENTIRE component in one response — never truncate mid-way.
"""


def _step2_coder(
    section: dict,
    global_css: str,
    page_title: str,
    reasoning_plan: str,
) -> str:
    name       = section["name"]
    html_slice = section["html_slice"][:MAX_HTML_PER_SECTION]
    scoped_css = section["scoped_css"][:MAX_CSS_PER_SECTION]
    assets     = section.get("assets", [])

    component_name = "".join(
        p.capitalize() for p in re.split(r"[-_]", name) if p
    )

    asset_list = "\n".join(
        f"  {a['local']}" for a in assets[:20]
    ) or "  (none)"

    user = f"""
Convert this HTML section into TSX component `{component_name}`.

=== COMPONENT NAME ===
{component_name}

=== PAGE TITLE ===
{page_title}

=== AVAILABLE ASSETS ===
{asset_list}

=== GLOBAL CSS (vars/fonts — do not re-declare) ===
{global_css[:1200]}

=== SCOPED CSS ===
{scoped_css or "(none)"}

=== LAYOUT PLAN (use these exact values) ===
{reasoning_plan}

=== HTML SLICE ===
{html_slice}

Output the complete TSX now. Start directly with:
'use client';
import React from 'react';
""".strip()

    messages = [
        {"role": "system", "content": _CODER_SYSTEM},
        {"role": "user",   "content": user},
    ]

    return _deepseek_call(
        messages,
        model=CODER_MODEL,
        max_tokens=CODER_MAX_TOKENS,
        temperature=CODER_TEMP,
        label=f"Code/{name}",
        return_reasoning=False,
    )


def _extract_tsx(text: str) -> str:
    """Strip markdown fences and ensure 'use client' is the first line."""
    text = re.sub(r"```[a-zA-Z]*", "", text)
    text = text.replace("```", "").strip()
    lines = text.splitlines()
    lines = [l for l in lines if l.strip() not in (
        "'use client';", '"use client";', "'use client'", '"use client"'
    )]
    return "'use client';\n" + "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# SINGLE-SECTION GENERATION  (with per-attempt validation)
# ─────────────────────────────────────────────────────────────────────────────

def _generate_section(
    section: dict,
    global_css: str,
    page_title: str,
    attempt_num: int,
) -> tuple[str | None, str]:
    """
    Run the two-step pipeline for one section.

    Returns (tsx_string, failure_reason).
    tsx_string is None when generation failed.
    """
    name = section["name"]

    try:
        print(f"  🧠 Step 1 — reasoning ({REASONING_MODEL})  [attempt {attempt_num}]")
        plan = _step1_reasoning(section, global_css)
        print(f"  ✅ Plan: {len(plan)} chars")

        time.sleep(3)

        print(f"  ✍️  Step 2 — coding ({CODER_MODEL})")
        raw = _step2_coder(section, global_css, page_title, plan)
        tsx = _extract_tsx(raw)

        # Validate immediately
        ok, reason = validate_tsx(name, tsx)
        if not ok:
            return None, f"validation failed: {reason}"

        return tsx, ""

    except Exception as exc:
        return None, str(exc)


# ─────────────────────────────────────────────────────────────────────────────
# CACHE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _save_component(name: str, tsx: str):
    """Persist a validated component to the on-disk cache."""
    os.makedirs(COMP_CACHE_DIR, exist_ok=True)
    cname = "".join(p.capitalize() for p in re.split(r"[-_]", name) if p)
    cache_path = os.path.join(COMP_CACHE_DIR, f"{cname}.tsx")
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write(tsx)
    print(f"  💾 Saved → {cache_path}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN NODE
# ─────────────────────────────────────────────────────────────────────────────

def coder_node(state: ClonerState) -> dict:
    try:
        sections   = state.get("sections", [])
        global_css = state.get("global_css", "")
        page_title = state.get("page_title", "Cloned Page")

        if not sections:
            return {
                "error": "coder_node: no sections in state",
                "logs":  ["Coder failed: no sections"],
            }

        if not DEEPSEEK_KEY:
            return {
                "error": "DEEPSEEK_API_KEY is not set",
                "logs":  ["Coder failed: DEEPSEEK_API_KEY missing"],
            }

        os.makedirs(COMP_CACHE_DIR, exist_ok=True)

        components:  dict[str, str] = {}
        logs:        list[str]      = []
        retry_rounds = 0

        # ── Build a queue of sections to process ─────────────────────────
        # Track (section_dict, attempts_so_far) for each pending item.
        pending: list[tuple[dict, int]] = [(s, 0) for s in sections]

        while pending:
            # Separate first-timers from retries for cleaner logging
            current_pass  = pending
            pending       = []

            for section, attempt in current_pass:
                name = section["name"]
                html_len = len(section.get("html_slice", ""))
                css_len  = len(section.get("scoped_css", ""))

                print(f"\n{'─' * 52}")
                print(
                    f"🔄  {name}  "
                    f"(html={html_len}ch, css={css_len}ch, attempt={attempt + 1})"
                )

                if html_len > MAX_HTML_PER_SECTION:
                    print(
                        f"  ⚠️  Section HTML exceeds limit "
                        f"({html_len}ch > {MAX_HTML_PER_SECTION}ch) — "
                        f"output may be incomplete."
                    )

                # Inter-section pacing (skip before very first section)
                if components or attempt > 0:
                    print(f"  ⏳ Pacing: waiting {MIN_CALL_INTERVAL}s …")
                    time.sleep(MIN_CALL_INTERVAL)

                tsx, failure_reason = _generate_section(
                    section, global_css, page_title, attempt + 1
                )

                if tsx is not None:
                    # ── SUCCESS ──────────────────────────────────────────
                    components[name] = tsx
                    _save_component(name, tsx)
                    log = f"✅ {name}  tsx={len(tsx)}ch  (attempt {attempt + 1})"
                    logs.append(log)
                    print(f"  {log}")

                else:
                    # ── FAILURE ───────────────────────────────────────────
                    print(f"  ❌ [{name}] failed: {failure_reason}")
                    logs.append(f"❌ {name} attempt {attempt + 1} failed: {failure_reason}")

                    next_attempt = attempt + 1
                    if next_attempt < MAX_SECTION_RETRIES:
                        print(
                            f"  🔁 Will retry '{name}' "
                            f"(attempt {next_attempt + 1}/{MAX_SECTION_RETRIES})"
                        )
                        pending.append((section, next_attempt))
                    else:
                        print(
                            f"  💀 '{name}' exhausted all {MAX_SECTION_RETRIES} "
                            f"retries — marking as permanently failed."
                        )
                        logs.append(
                            f"💀 {name} permanently failed after "
                            f"{MAX_SECTION_RETRIES} attempts"
                        )

            if pending:
                retry_rounds += 1
                print(
                    f"\n♻️   Retry round {retry_rounds}: "
                    f"{len(pending)} section(s) to regenerate: "
                    f"{[s['name'] for s, _ in pending]}"
                )

        # ── Determine which sections are still missing ────────────────────
        all_names     = [s["name"] for s in sections]
        failed_names  = [n for n in all_names if n not in components]
        valid_count   = len(components)

        print(f"\n{'═' * 52}")
        print(f"  📊 Coder summary:")
        print(f"     Sections detected:  {len(all_names)}")
        print(f"     Valid components:   {valid_count}")
        print(f"     Failed:             {len(failed_names)}")
        if failed_names:
            print(f"     Failed sections:    {failed_names}")
        print(f"     Retry rounds:       {retry_rounds}")
        print(f"{'═' * 52}\n")

        # ── Abort if no components at all ─────────────────────────────────
        if not components:
            return {
                "error":  "coder_node: all sections failed to generate",
                "logs":   logs + ["All sections failed — aborting pipeline"],
                "failed_sections":   failed_names,
                "coder_retry_rounds": retry_rounds,
            }

        # ── Partial failure: warn but do not abort ─────────────────────────
        # The assembler will verify completeness and decide whether to proceed.
        if failed_names:
            logs.append(
                f"WARNING: {len(failed_names)} section(s) failed permanently: "
                f"{failed_names}"
            )

        return {
            "components":         components,
            "failed_sections":    failed_names,
            "coder_retry_rounds": retry_rounds,
            "logs":               logs,
        }

    except Exception as e:
        print(f"❌ Coder node crashed: {e}")
        return {
            "error": str(e),
            "logs":  [f"Coder node crashed: {e}"],
            "failed_sections":    [],
            "coder_retry_rounds": 0,
        }