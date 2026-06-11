"""
state.py — shared state that flows through every LangGraph node
"""

from typing import TypedDict, Optional


class ClonerState(TypedDict):

    target_url: str

    # ── playwright ────────────────────────────
    rendered_html:  Optional[str]
    screenshot_b64: Optional[str]
    page_title:     Optional[str]

    # ── asset_node ────────────────────────────
    localised_html:       Optional[str]
    compressed_structure: Optional[dict]
    asset_count:          Optional[int]
    asset_manifest:       Optional[list]

    # Deterministic image map: original_url → ./assets/images/image_N.ext
    asset_map:            Optional[dict]
    # Video map: original_url → {video: ./assets/videos/..., poster: ...}
    video_map:            Optional[dict]
    # What the LLM sees — only local paths, never original URLs
    resolved_asset_map:   Optional[dict]
    # Full concatenated CSS from all downloaded stylesheets
    css_content:          Optional[str]

    # ── splitter_node ─────────────────────────
    # List of dicts, one per detected section:
    # {
    #   "name":       str,          e.g. "hero"
    #   "html_slice": str,          raw HTML for that section
    #   "scoped_css": str,          CSS rules that apply to this section
    #   "assets":     list[dict],   [{original, local}, ...]
    # }
    sections: Optional[list]

    # Global CSS (variables, fonts, resets) shared across all sections
    global_css: Optional[str]

    # ── coder_node ────────────────────────────
    # Dict keyed by section name → TSX string
    # e.g. {"hero": "export default function Hero() {...}"}
    components: Optional[dict]

    # Sections that failed generation and need retry
    failed_sections: Optional[list]

    # Number of coder retry rounds completed
    coder_retry_rounds: Optional[int]

    # ── assembler_node ────────────────────────
    # Absolute path to the scaffolded Next.js project
    nextjs_output_dir: Optional[str]

    # ── builder_node ──────────────────────────
    # Whether the final `npm run build` succeeded
    build_success: Optional[bool]
    # Raw stdout+stderr from the last build attempt
    build_output:  Optional[str]
    # How many auto-fix rounds were needed (0 = built clean first try)
    fix_rounds:    Optional[int]

    # ── legacy / final output (kept for compat) ──
    final_html: Optional[str]

    # ── metadata ──────────────────────────────
    error:       Optional[str]
    logs:        list[str]
    skip_scrape: Optional[bool]