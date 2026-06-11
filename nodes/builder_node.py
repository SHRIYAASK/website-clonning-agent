"""
nodes/builder_node.py — LangGraph Node 6

Runs `npm install && npm run build` inside nextjs-output/.
On failure, parses the TypeScript / Next.js compiler errors,
groups them by file, calls DeepSeek to patch each broken component,
then retries the build.

Retries up to MAX_FIX_ROUNDS rounds before giving up.

Fixes applied (issues #4, #5, #6, #10):
  - Pre-flight integrity check: verifies project structure, all TSX files
    present, no placeholder components, page.tsx imports complete.
    Builder aborts early rather than running npm on a broken project.
  - nextjs_output_dir verified to exist and contain package.json before use.
  - npm install and npm run build always executed with cwd=nextjs_output_dir.
  - cwd is printed before every npm command for traceability.
  - Builder will NOT run if any placeholder component is detected.

State keys read:
  nextjs_output_dir – absolute path to the Next.js project
  sections          – original section list (for context / integrity check)
  components        – generated component map (for integrity check)

State keys written:
  build_success     – bool
  build_output      – final stdout/stderr string
  fix_rounds        – how many fix iterations were needed
  logs              – appended
"""

import os
import re
import subprocess
import time
from pathlib import Path

import requests

from state import ClonerState
from nodes.validation import (
    validate_tsx,
    verify_project_structure,
    verify_page_imports,
    MIN_TSX_LENGTH,
)


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

DEEPSEEK_URL     = "https://api.deepseek.com/chat/completions"
DEEPSEEK_KEY     = os.getenv("DEEPSEEK_API_KEY", "")
FIXER_MODEL      = "deepseek-chat"
FIXER_MAX_TOKENS = 16_000
FIXER_TEMP       = 0.1

MAX_FIX_ROUNDS   = 5
BUILD_TIMEOUT    = 300
INSTALL_TIMEOUT  = 300
MAX_FILE_CONTEXT = 12_000


# ─────────────────────────────────────────────────────────────────────────────
# PRE-FLIGHT INTEGRITY CHECK  (issue #4, #10)
# ─────────────────────────────────────────────────────────────────────────────

def _preflight_check(
    project_dir: str,
    sections: list[dict],
    components: dict[str, str],
) -> tuple[bool, list[str]]:
    """
    Run all integrity checks before attempting any npm command.

    Returns (passed, list_of_failure_reasons).
    """
    failures: list[str] = []

    # 1. Project structure
    ok, reason = verify_project_structure(project_dir)
    if not ok:
        failures.append(f"Project structure: {reason}")

    # 2. Every section must have a component file on disk
    comp_dir = os.path.join(project_dir, "app", "components")
    for section in sections:
        name = section["name"]
        parts = re.split(r"[-_]", name)
        cname = "".join(p.capitalize() for p in parts if p)
        tsx_path = os.path.join(comp_dir, f"{cname}.tsx")

        if not os.path.exists(tsx_path):
            failures.append(f"Missing TSX file on disk: components/{cname}.tsx")
            continue

        with open(tsx_path, "r", encoding="utf-8", errors="ignore") as f:
            tsx_content = f.read()

        ok, reason = validate_tsx(name, tsx_content)
        if not ok:
            failures.append(f"Invalid TSX '{cname}.tsx': {reason}")

    # 3. page.tsx imports all expected components
    section_names = [s["name"] for s in sections if s["name"] in (components or {})]
    if section_names:
        all_present, missing = verify_page_imports(project_dir, section_names)
        if not all_present:
            failures.append(f"page.tsx missing imports for: {missing}")

    return len(failures) == 0, failures


# ─────────────────────────────────────────────────────────────────────────────
# NPM HELPERS  (issue #6 — always use cwd=project_dir)
# ─────────────────────────────────────────────────────────────────────────────

def _run(cmd: list[str], cwd: str, timeout: int) -> tuple[int, str]:
    """Run a subprocess with explicit cwd, capture combined stdout+stderr."""
    print(f"    📂 cwd: {cwd}")
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    combined = (result.stdout or "") + (result.stderr or "")
    return result.returncode, combined


def _npm_install(project_dir: str) -> tuple[int, str]:
    print(f"  📦 Running npm install in: {project_dir}")
    return _run(["npm", "install", "--legacy-peer-deps"], project_dir, INSTALL_TIMEOUT)


def _npm_build(project_dir: str) -> tuple[int, str]:
    print(f"  🔨 Running npm run build in: {project_dir}")
    env = {**os.environ, "CI": "false"}
    result = subprocess.run(
        ["npm", "run", "build"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT,
        env=env,
    )
    combined = (result.stdout or "") + (result.stderr or "")
    return result.returncode, combined


# ─────────────────────────────────────────────────────────────────────────────
# ERROR PARSER
# ─────────────────────────────────────────────────────────────────────────────

_FILE_LINE_RE = re.compile(
    r"(?:\./)?(app/[^\s:(\n]+\.tsx)[:\s(](\d+)"
)


def _parse_errors(build_output: str, project_dir: str) -> dict[str, list[str]]:
    """
    Parse compiler output → dict: relative_tsx_path → [error messages].
    Only files that actually exist in the project are included.
    """
    errors_by_file: dict[str, list[str]] = {}

    for match in _FILE_LINE_RE.finditer(build_output):
        rel_path = match.group(1)
        abs_path = os.path.join(project_dir, rel_path)
        if os.path.exists(abs_path):
            errors_by_file.setdefault(rel_path, [])

    lines = build_output.splitlines()
    current_file = None
    for line in lines:
        m = _FILE_LINE_RE.search(line)
        if m:
            current_file = m.group(1)
            errors_by_file.setdefault(current_file, [])
        if current_file and ("error" in line.lower() or "Error" in line):
            errors_by_file[current_file].append(line.strip())

    return {f: list(dict.fromkeys(msgs)) for f, msgs in errors_by_file.items()}


# ─────────────────────────────────────────────────────────────────────────────
# LLM FIXER
# ─────────────────────────────────────────────────────────────────────────────

_FIXER_SYSTEM = """\
You are an expert Next.js / TypeScript engineer performing surgical bug fixes.

You will receive:
  - A broken TSX component file
  - The exact TypeScript / Next.js build errors for that file

Your task:
  1. Fix EVERY error listed.
  2. Keep ALL existing logic, layout, and content exactly as-is.
  3. Do NOT add features, refactor, or change anything not related to the errors.
  4. Output ONLY the corrected TSX file contents — no markdown fences, no explanation.
  5. The very first line must always be: 'use client';
  6. All styling must remain as inline style={{ }} — never add className or Tailwind.
"""

_COMMON_FIXES_HINT = """\
Common patterns to watch for and fix:
  - Missing React import → add: import React from 'react';
  - 'any' type on event handlers → use React.MouseEvent, React.ChangeEvent etc.
  - Unknown DOM property → use camelCase (className, htmlFor, readOnly …)
  - useEffect / useState not imported → add to React import
  - 'children' prop missing → add children?: React.ReactNode to props interface
  - Module not found (e.g. next/image) → replace with plain <img>
  - CSS property values that are numbers (not strings) missing 'px'
  - href on <a> that is undefined (guard with || '#')
  - Unused imports triggering noUnusedLocals (remove them)
"""


def _call_fixer(rel_path: str, file_content: str, errors: list[str]) -> str | None:
    """Ask the LLM to fix a single file. Returns corrected content or None."""
    error_block = "\n".join(errors[:40])

    user = f"""Fix this TypeScript/Next.js component.

=== FILE: {rel_path} ===
{file_content[:MAX_FILE_CONTEXT]}

=== BUILD ERRORS ===
{error_block}

{_COMMON_FIXES_HINT}

Output ONLY the corrected TSX file contents starting with:
'use client';
"""

    payload = {
        "model":       FIXER_MODEL,
        "messages":    [
            {"role": "system", "content": _FIXER_SYSTEM},
            {"role": "user",   "content": user},
        ],
        "temperature": FIXER_TEMP,
        "max_tokens":  FIXER_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type":  "application/json",
    }

    for attempt in range(3):
        try:
            r = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=180)
            if r.status_code == 429:
                time.sleep(30 * (attempt + 1))
                continue
            if r.status_code >= 500:
                time.sleep(20)
                continue
            if r.status_code != 200:
                print(f"    ⚠️  Fixer HTTP {r.status_code}: {r.text[:200]}")
                return None

            content = r.json()["choices"][0]["message"]["content"]
            content = re.sub(r"```[a-zA-Z]*", "", content)
            content = content.replace("```", "").strip()

            lines = content.splitlines()
            lines = [l for l in lines if l.strip() not in (
                "'use client';", '"use client";', "'use client'", '"use client"'
            )]
            content = "'use client';\n" + "\n".join(lines)
            return content

        except requests.Timeout:
            print(f"    ⏱️  Fixer timeout attempt {attempt+1}")
            time.sleep(20)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# QUICK FIXES  (deterministic, no LLM needed)
# ─────────────────────────────────────────────────────────────────────────────

def _quick_fix(content: str) -> str:
    """Apply fast regex-based patches for the most common TSX issues."""
    # 1. Ensure 'use client' is first
    lines = content.splitlines()
    non_empty = [l for l in lines if l.strip()]
    if non_empty and non_empty[0].strip() not in ("'use client';", '"use client";'):
        content = "'use client';\n" + content

    # 2. Ensure React is imported
    if "import React" not in content:
        content = re.sub(
            r"('use client';?\n)",
            r"\1import React from 'react';\n",
            content, count=1,
        )

    # 3. Replace next/image with plain img
    content = re.sub(r"import\s+Image\s+from\s+'next/image';?\n?", "", content)
    content = re.sub(r"<Image\b", "<img", content)
    content = re.sub(r"</Image>", "", content)

    # 4. Replace next/link with plain <a>
    content = re.sub(r"import\s+Link\s+from\s+'next/link';?\n?", "", content)
    content = re.sub(r"<Link\b([^>]*)href=(['\"][^'\"]*['\"])", r"<a href=\2", content)
    content = re.sub(r"</Link>", "</a>", content)

    # 5. Fix bare numeric CSS values that need 'px'
    px_props = (
        "fontSize|lineHeight|letterSpacing|margin(?:Top|Bottom|Left|Right)?|"
        "padding(?:Top|Bottom|Left|Right)?|width|height|max(?:Width|Height)|"
        "min(?:Width|Height)|borderRadius|borderWidth|gap|top|left|right|bottom"
    )

    def _fix_numeric_css(m: re.Match) -> str:
        prop = m.group(1)
        val  = m.group(2)
        needs_px = {
            "fontSize", "lineHeight", "letterSpacing",
            "marginTop", "marginBottom", "marginLeft", "marginRight",
            "paddingTop", "paddingBottom", "paddingLeft", "paddingRight",
            "width", "height", "maxWidth", "minWidth", "maxHeight", "minHeight",
            "borderRadius", "borderWidth", "gap", "top", "left", "right", "bottom",
        }
        if prop in needs_px:
            return f"{prop}: '{val}px'"
        return m.group(0)

    content = re.sub(
        rf"\b({px_props}):\s*(\d+)(?=[,\s}}])",
        _fix_numeric_css,
        content,
    )

    # 6. Remove duplicate 'use client' lines
    seen_uc  = False
    new_lines = []
    for line in content.splitlines():
        if line.strip() in ("'use client';", '"use client";'):
            if not seen_uc:
                new_lines.append(line)
                seen_uc = True
        else:
            new_lines.append(line)
    content = "\n".join(new_lines)

    return content


# ─────────────────────────────────────────────────────────────────────────────
# FIX ROUND
# ─────────────────────────────────────────────────────────────────────────────

def _apply_fixes(errors_by_file: dict[str, list[str]], project_dir: str) -> int:
    """Quick-fix then LLM-fix each broken file. Returns count of patched files."""
    patched = 0

    for rel_path, errors in errors_by_file.items():
        abs_path = os.path.join(project_dir, rel_path)
        if not os.path.exists(abs_path):
            print(f"  ⚠️  File not found: {rel_path} — skipping")
            continue

        print(f"\n  🔧 Fixing: {rel_path}  ({len(errors)} errors)")

        with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
            original = f.read()

        patched_content = _quick_fix(original)

        if DEEPSEEK_KEY:
            fixed = _call_fixer(rel_path, patched_content, errors)
            if fixed:
                patched_content = fixed
                print(f"    ✅ LLM fix applied ({len(fixed)} chars)")
            else:
                print("    ⚠️  LLM fix returned nothing — using quick-fix only")
        else:
            print("    ℹ️  No DEEPSEEK_API_KEY — quick-fix only")

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(patched_content)

        patched += 1

    return patched


# ─────────────────────────────────────────────────────────────────────────────
# MAIN NODE
# ─────────────────────────────────────────────────────────────────────────────

def builder_node(state: ClonerState) -> dict:
    try:
        project_dir = state.get("nextjs_output_dir", "")
        sections    = state.get("sections", []) or []
        components  = state.get("components", {}) or {}

        print("\n" + "═" * 55)
        print("  🏗️   Builder Node")
        print("═" * 55)

        # ── Issue #5 / #6: verify project_dir before ANY npm command ─────
        if not project_dir:
            return {
                "error": "builder_node: nextjs_output_dir is empty or missing",
                "logs":  ["Builder failed: nextjs_output_dir not set"],
                "build_success": False,
            }

        if not os.path.isdir(project_dir):
            return {
                "error": f"builder_node: directory not found: {project_dir!r}",
                "logs":  [f"Builder failed: {project_dir!r} does not exist"],
                "build_success": False,
            }

        package_json = os.path.join(project_dir, "package.json")
        if not os.path.exists(package_json):
            return {
                "error": f"builder_node: package.json not found in {project_dir!r}",
                "logs":  ["Builder failed: package.json missing"],
                "build_success": False,
            }

        print(f"  📂 Project directory: {project_dir}")

        # ── Issue #4 / #10: pre-flight integrity check ────────────────────
        print("\n  🔍 Running pre-flight integrity checks …")
        ok, failures = _preflight_check(project_dir, sections, components)

        if not ok:
            print("  ❌ Pre-flight checks FAILED:")
            for f in failures:
                print(f"     • {f}")
            msg = (
                f"builder_node: pre-flight integrity check failed with "
                f"{len(failures)} issue(s). "
                f"Fix generation errors before building. "
                f"Issues: {failures}"
            )
            return {
                "error":         msg,
                "build_success": False,
                "logs":          [msg],
            }

        print("  ✅ Pre-flight checks passed.\n")

        logs: list[str] = []

        # ── npm install ───────────────────────────────────────────────────
        rc, install_out = _npm_install(project_dir)
        if rc != 0:
            print(f"  ⚠️  npm install exited with rc={rc} (may be non-fatal)")
            print(install_out[-1000:])
        logs.append(f"npm install rc={rc}")

        # ── Build → fix → retry loop ──────────────────────────────────────
        fix_rounds   = 0
        final_output = ""

        for round_num in range(MAX_FIX_ROUNDS + 1):
            print(f"\n  🔨 Build attempt {round_num + 1} / {MAX_FIX_ROUNDS + 1}")
            rc, build_out = _npm_build(project_dir)
            final_output  = build_out

            if rc == 0:
                print(f"\n  ✅ Build succeeded on attempt {round_num + 1}!")
                logs.append(f"Build succeeded (round {round_num})")
                return {
                    "build_success": True,
                    "build_output":  build_out,
                    "fix_rounds":    fix_rounds,
                    "logs":          logs + ["Build succeeded"],
                }

            print(f"  ❌ Build failed (rc={rc})")
            print(build_out[-3000:])

            if round_num == MAX_FIX_ROUNDS:
                break

            errors_by_file = _parse_errors(build_out, project_dir)
            print(f"\n  🗂️   Broken files: {list(errors_by_file.keys())}")

            if not errors_by_file:
                print("  ⚠️  Could not parse error locations — stopping auto-fix")
                logs.append("Could not parse error locations")
                break

            fix_rounds += 1
            patched = _apply_fixes(errors_by_file, project_dir)
            logs.append(f"Fix round {fix_rounds}: patched {patched} files")
            print(f"\n  🔁 Patched {patched} file(s) — retrying build …")
            time.sleep(2)

        print(f"\n  ❌ Build still failing after {fix_rounds} fix round(s).")
        print("  📋 Final build output (last 3000 chars):")
        print(final_output[-3000:])
        logs.append(f"Build failed after {fix_rounds} fix rounds")

        return {
            "build_success": False,
            "build_output":  final_output,
            "fix_rounds":    fix_rounds,
            "logs":          logs + [f"Build failed after {fix_rounds} rounds"],
        }

    except subprocess.TimeoutExpired:
        msg = "Builder node timed out during npm command"
        print(f"  ❌ {msg}")
        return {
            "error":         msg,
            "build_success": False,
            "logs":          [msg],
        }

    except Exception as e:
        print(f"  ❌ Builder node crashed: {e}")
        return {
            "error":         str(e),
            "build_success": False,
            "logs":          [f"Builder node crashed: {e}"],
        }