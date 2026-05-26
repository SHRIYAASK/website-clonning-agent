"""
state.py — shared state that flows through every LangGraph node
"""
# state.py

from typing import TypedDict, Optional


class ClonerState(TypedDict):

    target_url: str

    # playwright
    rendered_html: Optional[str]
    screenshot_b64: Optional[str]
    page_title: Optional[str]

    # assets
    localised_html: Optional[str]
    compressed_structure: Optional[dict]
    asset_count: Optional[int]

    # final output
    final_html: Optional[str]

    # metadata
    error: Optional[str]
    logs: list[str]
    asset_manifest: Optional[list]
    # cache
    skip_scrape: Optional[bool]