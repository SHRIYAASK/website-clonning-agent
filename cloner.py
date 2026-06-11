"""
cloner.py — CLI entry point

Usage:
    python cloner.py <url>               # full pipeline (pauses after splitter)
    python cloner.py <url> --retry       # skip scraping, reuse cached HTML
    python cloner.py <url> --yes         # skip confirmation prompt (non-interactive)

Pipeline flow:
    Phase 1: playwright → assets → splitter   (scrape + split, no API cost)
    [pause]  Show detected sections + estimated API calls, ask user to confirm
    Phase 2: coder → assembler → builder      (expensive LLM calls)
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

OUTPUT_DIR = "cloned-site"
NEXTJS_DIR = "nextjs-output"
CACHE_FILE = os.path.join(OUTPUT_DIR, ".cache.json")


# ─────────────────────────────────────────────────────────────────────────────
# CACHE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def save_cache(state: dict):
    Path(OUTPUT_DIR).mkdir(exist_ok=True)
    cache = {
        "target_url":     state.get("target_url"),
        "rendered_html":  state.get("rendered_html"),
        "screenshot_b64": state.get("screenshot_b64"),
        "page_title":     state.get("page_title"),
        "localised_html": state.get("localised_html"),
        "asset_count":    state.get("asset_count"),
        "asset_manifest": state.get("asset_manifest"),
        "css_content":    state.get("css_content"),
    }
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def load_cache(url: str) -> dict | None:
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("target_url") == url:
            return cache
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 GRAPH  (no LLM API calls — scrape + split only)
# ─────────────────────────────────────────────────────────────────────────────

def build_phase1_graph(skip_scrape: bool):
    from langgraph.graph import StateGraph, START, END
    from state import ClonerState
    from nodes.playwright_node import playwright_node
    from nodes.asset_node      import asset_node
    from nodes.splitter_node   import splitter_node

    def should_continue(state: ClonerState) -> str:
        return "end" if state.get("error") else "continue"

    graph = StateGraph(ClonerState)
    graph.add_node("splitter", splitter_node)

    if skip_scrape:
        graph.add_edge(START, "splitter")
    else:
        graph.add_node("playwright", playwright_node)
        graph.add_node("assets",     asset_node)
        graph.add_edge(START, "playwright")
        graph.add_conditional_edges(
            "playwright", should_continue,
            {"continue": "assets", "end": END},
        )
        graph.add_conditional_edges(
            "assets", should_continue,
            {"continue": "splitter", "end": END},
        )

    graph.add_edge("splitter", END)
    return graph.compile()


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2 GRAPH  (coder → assembler → builder)
# ─────────────────────────────────────────────────────────────────────────────

def build_phase2_graph():
    from langgraph.graph import StateGraph, START, END
    from state import ClonerState
    from nodes.coder_node     import coder_node
    from nodes.assembler_node import assembler_node
    from nodes.builder_node   import builder_node

    def should_continue(state: ClonerState) -> str:
        return "end" if state.get("error") else "continue"

    graph = StateGraph(ClonerState)
    graph.add_node("coder",     coder_node)
    graph.add_node("assembler", assembler_node)
    graph.add_node("builder",   builder_node)

    graph.add_edge(START, "coder")
    graph.add_conditional_edges(
        "coder", should_continue,
        {"continue": "assembler", "end": END},
    )
    graph.add_conditional_edges(
        "assembler", should_continue,
        {"continue": "builder", "end": END},
    )
    graph.add_edge("builder", END)
    return graph.compile()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION PREVIEW
# ─────────────────────────────────────────────────────────────────────────────

def _print_sections_preview(sections: list, asset_count: int):
    total_html = sum(len(s.get("html_slice", "")) for s in sections)
    total_css  = sum(len(s.get("scoped_css",  "")) for s in sections)

    print("\n" + "─" * 55)
    print(f"  🗂️   Sections detected: {len(sections)}")
    print("─" * 55)

    for i, s in enumerate(sections, 1):
        html_kb  = len(s.get("html_slice", "")) / 1024
        css_kb   = len(s.get("scoped_css",  "")) / 1024
        n_assets = len(s.get("assets", []))
        flag     = "  ⚠️ LARGE" if html_kb > 9 else ""
        print(
            f"  {i:>2}.  {s['name']:<22}  "
            f"html={html_kb:5.1f}KB  css={css_kb:4.1f}KB  "
            f"assets={n_assets}{flag}"
        )

    print("─" * 55)
    print(f"  📦  Assets downloaded:  {asset_count}")
    print(f"  📝  Total HTML to code: {total_html / 1024:.1f}KB")
    print(f"  🎨  Total scoped CSS:   {total_css  / 1024:.1f}KB")
    print()
    n_calls = len(sections) * 2
    print(f"  💸  Estimated API calls: ~{n_calls}  "
          f"({len(sections)} sections × 2 steps)")
    print(f"      deepseek-reasoner (layout plan) + deepseek-chat (TSX) per section")
    print("─" * 55)


# ─────────────────────────────────────────────────────────────────────────────
# USER PROMPT
# ─────────────────────────────────────────────────────────────────────────────

def _ask_proceed() -> bool:
    print()
    while True:
        try:
            ans = input("  ▶  Proceed with LLM coding? [y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            return False
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  Please enter y or n.")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("\nUsage:  python cloner.py <url>")
        print("        python cloner.py <url> --retry")
        print("        python cloner.py <url> --yes    # skip confirmation\n")
        sys.exit(1)

    url = sys.argv[1]
    if not url.startswith("http"):
        url = "https://" + url

    retry_mode = "--retry" in sys.argv
    auto_yes   = "--yes"   in sys.argv

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("\n❌  Missing DEEPSEEK_API_KEY")
        print("    Set it at: https://platform.deepseek.com/api_keys\n")
        sys.exit(1)

    print("\n" + "─" * 55)
    print("  🕸️  Webpage Cloner  (LangGraph + DeepSeek)")
    print("─" * 55)

    initial_state = {
        "target_url":           url,
        "rendered_html":        None,
        "screenshot_b64":       None,
        "page_title":           None,
        "localised_html":       None,
        "compressed_structure": None,
        "asset_count":          None,
        "asset_manifest":       None,
        "css_content":          None,
        "asset_map":            None,
        "video_map":            None,
        "resolved_asset_map":   None,
        "sections":             None,
        "global_css":           None,
        "components":           None,
        "failed_sections":      None,
        "coder_retry_rounds":   None,
        "nextjs_output_dir":    None,
        "build_success":        None,
        "build_output":         None,
        "fix_rounds":           None,
        "final_html":           None,
        "error":                None,
        "logs":                 [],
        "skip_scrape":          False,
    }

    skip_scrape = False

    if retry_mode:
        cache = load_cache(url)
        if cache:
            print(f"\n♻️   Retry mode — reusing cached scrape for: {url}")
            initial_state.update(cache)
            initial_state["skip_scrape"] = True
            skip_scrape = True
        else:
            print(f"\n⚠️   No cache found for {url} — running full pipeline.")

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 1
    # ─────────────────────────────────────────────────────────────────────────
    print(f"\n🚀  Phase 1 — scraping & splitting: {url}\n")

    phase1 = build_phase1_graph(skip_scrape)
    state  = phase1.invoke(initial_state)

    if state.get("localised_html") and not retry_mode:
        save_cache(state)
        print("\n💾  Scrape cached — use --retry to skip this step next time.")

    if state.get("error"):
        print(f"\n❌  Phase 1 failed: {state['error']}")
        sys.exit(1)

    sections    = state.get("sections", [])
    asset_count = state.get("asset_count", 0)

    if not sections:
        print("\n❌  Splitter produced no sections — aborting.")
        sys.exit(1)

    _print_sections_preview(sections, asset_count)

    if auto_yes:
        print("  ⚡  --yes flag set — skipping confirmation.")
    else:
        if not _ask_proceed():
            print("\n  ℹ️   Stopped after splitting. No API calls were made.")
            print(f"       --retry  reuses the cached scrape next run.")
            print(f"       --yes    skips this prompt entirely.\n")
            sys.exit(0)

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 2
    # ─────────────────────────────────────────────────────────────────────────
    print(f"\n🚀  Phase 2 — coding {len(sections)} sections …\n")

    phase2 = build_phase2_graph()
    result = phase2.invoke(state)

    if result.get("error"):
        print(f"\n❌  Phase 2 failed: {result['error']}")
        failed = result.get("failed_sections", [])
        if failed:
            print(f"     Failed sections: {failed}")
        print(f"\n💡  Scrape is cached — retry from coding step with:")
        print(f"         python cloner.py {url} --retry\n")
        sys.exit(1)

    # ── Summary ───────────────────────────────────────────────────────────────
    components       = result.get("components", {})
    failed_sections  = result.get("failed_sections", [])
    build_success    = result.get("build_success")
    fix_rounds       = result.get("fix_rounds", 0)
    coder_retries    = result.get("coder_retry_rounds", 0)

    print("\n" + "─" * 55)
    print("  ✅  Done!")
    print(f"  📁  Next.js project:    ./{NEXTJS_DIR}/")
    print(f"  🗂️   Sections detected:  {len(sections)}")
    print(f"  🧩  Components coded:   {len(components)}")

    if failed_sections:
        print(f"  ❌  Failed sections:    {failed_sections}")
    if coder_retries:
        print(f"  🔁  Coder retry rounds: {coder_retries}")

    print(f"  🧩  Components:         {list(components.keys())}")
    print(f"  📦  Assets:             {asset_count} files → public/assets/")

    if build_success is True:
        rounds_str = f" (auto-fixed in {fix_rounds} round(s))" if fix_rounds else " (clean ✨)"
        print(f"  🏗️   Build:             ✅ Passed{rounds_str}")
    elif build_success is False:
        print(f"  🏗️   Build:             ❌ Failed after {fix_rounds} fix round(s)")
        print("       See build output above for remaining errors.")
        print("       You can still use `npm run dev` for development.")
    else:
        print("  🏗️   Build:             ⚠️  Not run")

    print("─" * 55)
    print(f"\n  cd {NEXTJS_DIR} && npm install")
    if build_success:
        print("  npm run start    # production (build already done)")
    else:
        print("  npm run dev      # development mode")
    print("  → http://localhost:3000\n")


if __name__ == "__main__":
    main()