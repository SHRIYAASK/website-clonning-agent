"""
cloner.py — CLI entry point

Usage:
    python cloner.py <url>            # full pipeline
    python cloner.py <url> --retry    # skip scraping, reuse cached HTML → call Gemini only
"""

import sys
import os
import json
import base64

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

OUTPUT_DIR = "cloned-site"
CACHE_FILE = os.path.join(OUTPUT_DIR, ".cache.json")


def save_cache(state: dict):
    """Save scrape results so --retry can skip playwright+assets."""
    Path(OUTPUT_DIR).mkdir(exist_ok=True)
    cache = {
        "target_url":     state.get("target_url"),
        "rendered_html":  state.get("rendered_html"),
        "screenshot_b64": state.get("screenshot_b64"),
        "page_title":     state.get("page_title"),
        "localised_html": state.get("localised_html"),
        "asset_count":    state.get("asset_count"),
    }
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def load_cache(url: str) -> dict | None:
    """Load cached scrape data if it exists and matches the URL."""
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


def main():
    if len(sys.argv) < 2:
        print("\nUsage:   python cloner.py <url>")
        print("         python cloner.py <url> --retry   # reuse cached scrape\n")
        sys.exit(1)

    url = sys.argv[1]
    if not url.startswith("http"):
        url = "https://" + url

    retry_mode = "--retry" in sys.argv

    if not os.environ.get("OPENROUTER_API_KEY") or not os.environ.get("DEEPSEEK_API_KEY"):
        if not os.environ.get("OPENROUTER_API_KEY"):
            print("\n❌  OPENROUTER_API_KEY is not set.")
            print("   Get a free key → https://openrouter.ai/keys")
            print("   Windows : $env:OPENROUTER_API_KEY = 'sk-or-...'")
            print("   Mac/Linux: export OPENROUTER_API_KEY=sk-or-...\n")
        if not os.environ.get("DEEPSEEK_API_KEY"):
            print("\n❌  DEEPSEEK_API_KEY is not set.")
            print("   Get a key → https://platform.deepseek.com")
            print("   Windows : $env:DEEPSEEK_API_KEY = 'sk-...'")
            print("   Mac/Linux: export DEEPSEEK_API_KEY=sk-...\n")
        sys.exit(1)

    print("\n" + "─" * 50)
    print("  🕸️  Webpage Cloner  (LangGraph + deepseek)")
    print("─" * 50)

    from graph import cloner_graph

    # ── Build initial state ───────────────────────────────────────────────────
    initial_state = {
        "target_url":     url,
        "rendered_html":  None,
        "screenshot_b64": None,
        "page_title":     None,
        "localised_html": None,
        "asset_count":    None,
        "final_html":     None,
        "error":          None,
        "logs":           [],
        "skip_scrape":    False,
    }

    # ── Retry mode: load cached scrape, skip playwright+assets ───────────────
    if retry_mode:
        cache = load_cache(url)
        if cache:
            print(f"\n♻️   Retry mode — reusing cached scrape for: {url}")
            initial_state.update(cache)
            initial_state["skip_scrape"] = True
        else:
            print(f"\n⚠️   No cache found for {url} — running full pipeline instead.")

    print(f"\n🚀  Starting pipeline for: {url}\n")

    # ── Run the graph ─────────────────────────────────────────────────────────
    result = cloner_graph.invoke(initial_state)

    # ── Save cache after successful scrape (even if Gemini failed) ────────────
    if result.get("localised_html") and not retry_mode:
        save_cache(result)

    # ── Handle errors ─────────────────────────────────────────────────────────
    if result.get("error"):
        print(f"\n❌  Pipeline failed: {result['error']}")
        if "rate limit" in result["error"].lower():
            print("\n💡  Tip: Run with --retry to skip scraping and call Gemini only:")
            print(f"        python cloner.py {url} --retry\n")
        sys.exit(1)

    # ── Save final HTML ───────────────────────────────────────────────────────
    Path(OUTPUT_DIR).mkdir(exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, "index.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result["final_html"])

    print("\n" + "─" * 50)
    print("  ✅  Done!")
    print(f"  📄  {output_path}")
    print(f"  📸  {OUTPUT_DIR}/screenshot.jpg")
    print(f"  📦  {OUTPUT_DIR}/assets/  ({result.get('asset_count', 0)} files)")
    print("─" * 50)
    print("\n  Run the server:")
    print("      python server.py")
    print("  Then open: http://localhost:5000\n")


if __name__ == "__main__":
    main()