"""
graph.py — LangGraph pipeline definition

Normal flow:   START → playwright → assets → gemini → END
Cached flow:   START → maybe_scrape → gemini → END
                       (skips playwright+assets if cache exists)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from langgraph.graph import StateGraph, START, END
from state import ClonerState
from nodes.playwright_node import playwright_node
from nodes.asset_node      import asset_node
from nodes.gemini_node     import gemini_node


def should_continue(state: ClonerState) -> str:
    return "end" if state.get("error") else "continue"


def should_scrape(state: ClonerState) -> str:
    """Skip playwright+assets if cached HTML is already in state."""
    if state.get("skip_scrape") and state.get("localised_html") and state.get("screenshot_b64"):
        print("⚡  Cache hit — skipping scrape, going straight to Gemini…\n")
        return "skip"
    return "scrape"


def build_graph():
    graph = StateGraph(ClonerState)

    graph.add_node("playwright", playwright_node)
    graph.add_node("assets",     asset_node)
    graph.add_node("gemini",     gemini_node)

    # At START: decide whether to scrape or use cache
    graph.add_conditional_edges(
        START,
        should_scrape,
        {"scrape": "playwright", "skip": "gemini"},
    )

    graph.add_conditional_edges(
        "playwright",
        should_continue,
        {"continue": "assets", "end": END},
    )

    graph.add_conditional_edges(
        "assets",
        should_continue,
        {"continue": "gemini", "end": END},
    )

    graph.add_edge("gemini", END)

    return graph.compile()


cloner_graph = build_graph()