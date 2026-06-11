"""
graph.py — LangGraph pipeline definition

Normal flow:
  START → playwright → assets → splitter → coder → assembler → builder → END

Cached flow (--retry):
  START → (skip playwright+assets) → splitter → coder → assembler → builder → END

Error short-circuit: any node that sets state["error"] jumps to END.

Note: builder_node does NOT set state["error"] on build failure — it sets
state["build_success"] = False instead, so the user always gets the output
directory even when the build could not be fully auto-fixed.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from langgraph.graph import StateGraph, START, END

from state import ClonerState
from nodes.playwright_node  import playwright_node
from nodes.asset_node       import asset_node
from nodes.splitter_node    import splitter_node
from nodes.coder_node       import coder_node
from nodes.assembler_node   import assembler_node
from nodes.builder_node     import builder_node


# ─────────────────────────────────────────────────────────────────────────────
# EDGE CONDITIONS
# ─────────────────────────────────────────────────────────────────────────────

def should_continue(state: ClonerState) -> str:
    return "end" if state.get("error") else "continue"


def should_scrape(state: ClonerState) -> str:
    """Skip playwright+assets if cached data is already in state."""
    if (
        state.get("skip_scrape")
        and state.get("localised_html")
        and state.get("screenshot_b64")
    ):
        print("⚡  Cache hit — skipping scrape, going straight to splitter…\n")
        return "skip"
    return "scrape"


# ─────────────────────────────────────────────────────────────────────────────
# GRAPH BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_graph():
    graph = StateGraph(ClonerState)

    # ── Register nodes ────────────────────────────────────────────────────────
    graph.add_node("playwright", playwright_node)
    graph.add_node("assets",     asset_node)
    graph.add_node("splitter",   splitter_node)
    graph.add_node("coder",      coder_node)
    graph.add_node("assembler",  assembler_node)
    graph.add_node("builder",    builder_node)

    # ── Edges ─────────────────────────────────────────────────────────────────

    # START: decide scrape vs cache
    graph.add_conditional_edges(
        START,
        should_scrape,
        {"scrape": "playwright", "skip": "splitter"},
    )

    # playwright → assets  (or END on error)
    graph.add_conditional_edges(
        "playwright",
        should_continue,
        {"continue": "assets", "end": END},
    )

    # assets → splitter  (or END on error)
    graph.add_conditional_edges(
        "assets",
        should_continue,
        {"continue": "splitter", "end": END},
    )

    # splitter → coder  (or END on error)
    graph.add_conditional_edges(
        "splitter",
        should_continue,
        {"continue": "coder", "end": END},
    )

    # coder → assembler  (or END on error)
    graph.add_conditional_edges(
        "coder",
        should_continue,
        {"continue": "assembler", "end": END},
    )

    # assembler → builder  (or END on error)
    graph.add_conditional_edges(
        "assembler",
        should_continue,
        {"continue": "builder", "end": END},
    )

    # builder → END  (always — build_success flag tells the story)
    graph.add_edge("builder", END)

    return graph.compile()


cloner_graph = build_graph()