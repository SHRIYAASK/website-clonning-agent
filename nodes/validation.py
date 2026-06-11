"""
nodes/validation.py — TSX and project integrity checks shared across nodes.

Centralises all validation so every node applies the same rules.
"""

import os
import re


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# A generated component must be at least this long to be considered real output.
MIN_TSX_LENGTH = 1_000

# Strings that indicate the model returned a placeholder / error stub.
PLACEHOLDER_MARKERS = [
    "component failed to generate",
    "generation failed",
    "// todo",
    "todo:",
    "placeholder",
    "not implemented",
    "{/* error",
    "{/* generation",
]


# ─────────────────────────────────────────────────────────────────────────────
# TSX VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def validate_tsx(name: str, tsx: str) -> tuple[bool, str]:
    """
    Check whether a TSX string is a valid, non-placeholder component.

    Returns (is_valid, reason).
    reason is empty when valid.
    """
    if not tsx or not tsx.strip():
        return False, "empty output"

    if len(tsx) < MIN_TSX_LENGTH:
        return False, f"too short ({len(tsx)} < {MIN_TSX_LENGTH} chars)"

    lower = tsx.lower()
    for marker in PLACEHOLDER_MARKERS:
        if marker in lower:
            return False, f"placeholder marker found: {marker!r}"

    if "export default" not in tsx:
        return False, "missing 'export default'"

    # Must contain at least one JSX element
    if not re.search(r"<[A-Za-z][A-Za-z0-9]*[\s/>]", tsx):
        return False, "no JSX elements found"

    # Must start with 'use client' (within first 5 non-empty lines)
    non_empty = [l.strip() for l in tsx.splitlines() if l.strip()]
    use_client_found = any(
        l in ("'use client';", '"use client";', "'use client'", '"use client"')
        for l in non_empty[:5]
    )
    if not use_client_found:
        return False, "missing 'use client' directive"

    return True, ""


def validate_components(
    sections: list[dict],
    components: dict[str, str],
) -> tuple[list[str], list[str]]:
    """
    Compare detected sections against generated components.

    Returns:
        valid_names   – section names with good TSX
        missing_names – section names that are absent or invalid
    """
    valid:   list[str] = []
    missing: list[str] = []

    for section in sections:
        name = section["name"]
        tsx  = components.get(name, "")
        ok, reason = validate_tsx(name, tsx)
        if ok:
            valid.append(name)
        else:
            missing.append(name)
            print(f"  ⚠️  Section '{name}' INVALID: {reason}")

    return valid, missing


# ─────────────────────────────────────────────────────────────────────────────
# PROJECT INTEGRITY CHECK
# ─────────────────────────────────────────────────────────────────────────────

def verify_project_structure(project_dir: str) -> tuple[bool, str]:
    """
    Confirm the Next.js output directory looks healthy before running npm.

    Returns (ok, reason).
    """
    if not project_dir:
        return False, "nextjs_output_dir is empty"

    if not os.path.isdir(project_dir):
        return False, f"directory does not exist: {project_dir!r}"

    required = [
        "package.json",
        os.path.join("app", "page.tsx"),
        os.path.join("app", "layout.tsx"),
        os.path.join("app", "globals.css"),
    ]
    for rel in required:
        full = os.path.join(project_dir, rel)
        if not os.path.exists(full):
            return False, f"missing required file: {rel}"

    return True, ""


def verify_page_imports(project_dir: str, section_names: list[str]) -> tuple[bool, list[str]]:
    """
    Check that page.tsx actually imports every expected component.

    Returns (all_present, missing_component_names).
    """
    page_path = os.path.join(project_dir, "app", "page.tsx")
    if not os.path.exists(page_path):
        return False, list(section_names)

    with open(page_path, "r", encoding="utf-8") as f:
        page_src = f.read()

    missing = []
    for name in section_names:
        # component name is PascalCase version of section name
        parts = re.split(r"[-_]", name)
        cname = "".join(p.capitalize() for p in parts if p)
        if cname not in page_src:
            missing.append(name)

    return len(missing) == 0, missing