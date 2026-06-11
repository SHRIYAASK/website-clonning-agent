"""
nodes/splitter_node.py — LangGraph Node 3

Responsibilities:
  1. Split the localised HTML into named sections with sensible granularity.
     Target: 5–12 sections per page, none trivially small (< MIN_SECTION_HTML),
     none so large the coder gets truncated (> MAX_SECTION_HTML).
  2. Scope CSS per section.
  3. Separate global CSS (@font-face, :root, body resets).
  4. Attach relevant asset entries.

Output added to state:
  sections   – list of {name, html_slice, scoped_css, assets}
  global_css – font/variable/reset rules shared by everything
"""

import os
import re
from bs4 import BeautifulSoup, Tag, NavigableString
from state import ClonerState


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

OUTPUT_DIR = "cloned-site"
ASSETS_DIR = os.path.join(OUTPUT_DIR, "assets")

MIN_SECTION_HTML = 200    # chars — anything smaller gets merged with a neighbour
MAX_SECTION_HTML = 10_000 # chars — anything larger gets subdivided
SECTION_CSS_LIMIT = 20_000


# ─────────────────────────────────────────────────────────────────────────────
# SECTION NAMING RULES
# (name, class/id/aria keywords, heading-text keywords)
# ─────────────────────────────────────────────────────────────────────────────

NAMING_RULES = [
    ("navbar",       ["navbar","nav","navigation","topbar","header","menu","menubar"],
                     []),
    ("hero",         ["hero","banner","jumbotron","splash","intro","landing","masthead","above-fold"],
                     ["get started","sign up","try for free","watch demo","learn more","hero"]),
    ("features",     ["feature","features","benefit","benefits","why","highlight","capabilities","services","service"],
                     ["feature","benefit","why us","what we","how it","capabilities"]),
    ("pricing",      ["pric","plan","plans","tier","tiers","package","billing"],
                     ["price","pricing","plan","month","year","per user","free trial"]),
    ("testimonials", ["testimonial","review","reviews","quote","feedback","trust","social-proof","customers"],
                     ["said","testimonial","review","customer","client","loved by"]),
    ("team",         ["team","people","staff","member","about-us","crew","founders","leadership"],
                     ["team","founder","ceo","cto","our people","meet the"]),
    ("faq",          ["faq","accordion","question","answer","help","support"],
                     ["faq","frequently","question","answer","how do","what is","can i"]),
    ("cta",          ["cta","call-to-action","callout","signup","subscribe","waitlist","get-started","conversion"],
                     ["get started","sign up","start free","try free","contact us","book a demo","request"]),
    ("gallery",      ["gallery","portfolio","showcase","work","projects","case-study"],
                     ["portfolio","our work","projects","case study","gallery"]),
    ("stats",        ["stat","stats","metric","metrics","counter","numbers","achievement","social-proof"],
                     ["users","customers","projects","years","revenue","million","billion","%","+"]),
    ("logos",        ["logo","logos","brand","brands","partner","partners","client","clients","trust-bar","as-seen"],
                     ["trusted by","as seen in","our clients","partners","used by"]),
    ("content",      ["content","article","post","blog","text","about","story","mission"],
                     ["about us","our story","mission","vision","we are","we help"]),
    ("footer",       ["footer","foot","bottom","copyright","sitemap"],
                     []),
]

# Tags that are always their own section boundary regardless of size
HARD_BOUNDARY_TAGS = {"header", "nav", "footer"}

# Tags that can be section boundaries if they have keyword signals
SOFT_BOUNDARY_TAGS = {"section", "article", "aside", "main"}


# ─────────────────────────────────────────────────────────────────────────────
# CSS UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _load_all_css(asset_manifest: list) -> str:
    chunks = []
    for entry in asset_manifest:
        local = entry.get("local", "")
        if not local.endswith(".css"):
            continue
        path = os.path.join(OUTPUT_DIR, local.lstrip("./"))
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    chunks.append(f.read())
            except Exception:
                pass
    return "\n".join(chunks)


def _split_css_rules(css: str) -> list[str]:
    rules, depth, current = [], 0, []
    i = 0
    while i < len(css):
        if css[i:i+2] == "/*":
            end = css.find("*/", i + 2)
            i = (end + 2) if end != -1 else len(css)
            continue
        ch = css[i]
        current.append(ch)
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                rule = "".join(current).strip()
                if rule:
                    rules.append(rule)
                current = []
        i += 1
    return rules


def _extract_global_css(rules: list[str]) -> tuple[list[str], list[str]]:
    global_pat = re.compile(
        r"^(@font-face|@import|@keyframes|:root|\*\s*\{|html\s*\{|body\s*\{)",
        re.IGNORECASE,
    )
    global_rules = [r for r in rules if global_pat.match(r.strip())]
    other_rules  = [r for r in rules if not global_pat.match(r.strip())]
    return global_rules, other_rules


def _collect_identifiers(fragment: Tag) -> set[str]:
    ids = set()
    for tag in fragment.find_all(True):
        ids.add(tag.name.lower())
        for cls in tag.get("class", []):
            ids.add(f".{cls}")
        tid = tag.get("id")
        if tid:
            ids.add(f"#{tid}")
    return ids


def _rule_matches(rule: str, identifiers: set[str]) -> bool:
    brace = rule.find("{")
    if brace == -1:
        return False
    for selector in rule[:brace].split(","):
        for token in re.split(r"[\s>+~\[\]:()]+", selector.strip()):
            token = token.strip()
            if not token:
                continue
            if token.lower() in identifiers:
                return True
            if token.startswith((".", "#")) and token in identifiers:
                return True
            for ident in identifiers:
                if ident.startswith(".") and token.startswith("."):
                    if ident in token or token in ident:
                        return True
    return False


def _scope_css(html_fragment: str, all_rules: list[str]) -> str:
    soup = BeautifulSoup(html_fragment, "html.parser")
    ids  = _collect_identifiers(soup)
    css  = "\n".join(r for r in all_rules if _rule_matches(r, ids))
    if len(css) > SECTION_CSS_LIMIT:
        css = css[:SECTION_CSS_LIMIT] + "\n/* [CSS truncated] */"
    return css


# ─────────────────────────────────────────────────────────────────────────────
# SECTION NAMING  (content-aware)
# ─────────────────────────────────────────────────────────────────────────────

def _element_signals(element: Tag) -> str:
    """
    Build a single lowercase string of all naming signals for an element:
    tag name, class names, id, aria-label, role, plus visible heading text.
    """
    parts = [
        element.name,
        " ".join(element.get("class", [])),
        element.get("id", ""),
        element.get("aria-label", ""),
        element.get("role", ""),
        element.get("data-section", ""),
    ]
    # Add text from headings (h1–h3) inside the element — strong naming signal
    for h in element.find_all(["h1", "h2", "h3"], limit=3):
        parts.append(h.get_text(" ", strip=True))
    # Add text from buttons / CTAs
    for b in element.find_all(["button", "a"], limit=4):
        parts.append(b.get_text(" ", strip=True))
    return " ".join(parts).lower()


def _name_element(element: Tag) -> str:
    signals  = _element_signals(element)
    best_name  = None
    best_score = 0

    for name, class_kws, text_kws in NAMING_RULES:
        score = 0
        for kw in class_kws:
            if kw in signals:
                score += 3   # class/id match is strongest
        for kw in text_kws:
            if kw in signals:
                score += 1   # text match is weaker but still useful
        if score > best_score:
            best_score = score
            best_name  = name

    # Fallback: use the element's own id, or its tag name
    if best_name is None or best_score == 0:
        best_name = element.get("id") or element.name or "section"

    return best_name


# ─────────────────────────────────────────────────────────────────────────────
# CANDIDATE EXTRACTION
# Get a flat list of candidate block elements from the page.
# ─────────────────────────────────────────────────────────────────────────────

def _unwrap_single_children(tag: Tag, max_depth: int = 5) -> Tag:
    """Walk down single-child wrapper divs until we find real content."""
    depth = 0
    while depth < max_depth:
        children = [c for c in tag.children if isinstance(c, Tag)]
        if len(children) == 1 and children[0].name not in HARD_BOUNDARY_TAGS:
            tag = children[0]
            depth += 1
        else:
            break
    return tag


def _get_candidates(soup: BeautifulSoup) -> list[Tag]:
    """
    Return a flat list of block-level elements that are good section candidates.
    Strategy:
      1. Start at body (unwrapping single-child wrappers).
      2. Walk direct children.
      3. If a child is a hard boundary tag → always a candidate.
      4. If a child is a soft boundary tag (section/article) → candidate.
      5. If a child is a large div → recurse one level to find sub-sections.
      6. If a child is tiny → skip for now (will be merged later).
    """
    body = soup.find("body") or soup
    root = _unwrap_single_children(body)
    return _collect_candidates(root, depth=0)


def _collect_candidates(container: Tag, depth: int) -> list[Tag]:
    MAX_DEPTH = 3
    children  = [c for c in container.children if isinstance(c, Tag)]

    # Nothing to recurse into
    if not children:
        return [container]

    candidates = []

    for child in children:
        size = len(str(child))

        # Hard boundaries are always their own section
        if child.name in HARD_BOUNDARY_TAGS:
            candidates.append(child)
            continue

        # Semantic section/article tags → own section
        if child.name in SOFT_BOUNDARY_TAGS:
            candidates.append(child)
            continue

        # Large block: try to go one level deeper (but not too deep)
        if size > MAX_SECTION_HTML and depth < MAX_DEPTH:
            sub = _collect_candidates(child, depth + 1)
            # Only use the subdivision if it produces more than 1 meaningful chunk
            meaningful = [s for s in sub if len(str(s)) >= MIN_SECTION_HTML]
            if len(meaningful) > 1:
                candidates.extend(sub)
                continue

        # Default: use as-is
        candidates.append(child)

    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# MERGE TINY SECTIONS
# ─────────────────────────────────────────────────────────────────────────────

def _merge_tiny(candidates: list[Tag]) -> list[Tag]:
    """
    Merge any candidate smaller than MIN_SECTION_HTML into its nearest
    meaningful neighbour (prefer previous, fall back to next).
    Uses a wrapper <div> so the merge result is still a single Tag.

    Also merges bare inline/text-level tags like <h2>, <p>, <span>
    that slipped through as top-level candidates.
    """
    INLINE_TAGS = {"h1","h2","h3","h4","h5","h6","p","span","a","strong","em","br","hr","img"}

    merged: list[Tag] = []
    pending_html: list[str] = []   # accumulates small/inline chunks

    def flush_pending():
        if not pending_html:
            return
        wrapper = BeautifulSoup(
            "<div>" + "".join(pending_html) + "</div>",
            "html.parser"
        ).find("div")
        if wrapper:
            merged.append(wrapper)
        pending_html.clear()

    for cand in candidates:
        size   = len(str(cand))
        is_tiny   = size < MIN_SECTION_HTML
        is_inline = cand.name in INLINE_TAGS

        if is_tiny or is_inline:
            pending_html.append(str(cand))
        else:
            # Before appending a real section, attach any accumulated tiny chunks to it
            if pending_html:
                # Prepend pending to this section by wrapping both
                combo = BeautifulSoup(
                    "<div>" + "".join(pending_html) + str(cand) + "</div>",
                    "html.parser"
                ).find("div")
                pending_html.clear()
                merged.append(combo)
            else:
                merged.append(cand)

    flush_pending()  # trailing tiny elements go to their own block
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# DEDUP NAMES
# ─────────────────────────────────────────────────────────────────────────────

def _dedup_name(name: str, used: dict[str, int]) -> str:
    count = used.get(name, 0) + 1
    used[name] = count
    return name if count == 1 else f"{name}_{count}"


# ─────────────────────────────────────────────────────────────────────────────
# ASSET FILTERING
# ─────────────────────────────────────────────────────────────────────────────

def _assets_for_slice(html_slice: str, asset_manifest: list) -> list[dict]:
    return [
        e for e in asset_manifest
        if os.path.basename(e.get("local","")) in html_slice
        or e.get("local","") in html_slice
    ]


# ─────────────────────────────────────────────────────────────────────────────
# MAIN NODE
# ─────────────────────────────────────────────────────────────────────────────

def splitter_node(state: ClonerState) -> dict:
    try:
        localised_html = state.get("localised_html", "")
        asset_manifest = state.get("asset_manifest", [])

        if not localised_html:
            return {
                "error": "splitter_node: no localised_html in state",
                "logs":  ["Splitter failed: missing HTML"],
            }

        # ── 1. Parse ──────────────────────────────────────────────────────
        soup = BeautifulSoup(localised_html, "html.parser")

        # ── 2. CSS ────────────────────────────────────────────────────────
        raw_css   = state.get("css_content") or _load_all_css(asset_manifest)
        all_rules = _split_css_rules(raw_css)
        global_rule_list, other_rules = _extract_global_css(all_rules)
        global_css = "\n".join(global_rule_list)

        print(f"🎨 Total CSS rules: {len(all_rules)} "
              f"(global: {len(global_rule_list)}, scoped pool: {len(other_rules)})")

        # ── 3. Candidate extraction ───────────────────────────────────────
        raw_candidates = _get_candidates(soup)
        print(f"🔍 Raw candidates: {len(raw_candidates)}  "
              f"sizes: {[len(str(c)) for c in raw_candidates]}")

        # ── 4. Merge tiny/inline fragments ───────────────────────────────
        candidates = _merge_tiny(raw_candidates)
        print(f"🗂️  After merging tiny: {len(candidates)} candidates")

        # ── 5. Name + build section dicts ────────────────────────────────
        used_names: dict[str, int] = {}
        sections: list[dict] = []

        for element in candidates:
            raw_name   = _name_element(element)
            name       = _dedup_name(raw_name, used_names)
            html_slice = str(element)
            scoped_css = _scope_css(html_slice, other_rules)
            assets     = _assets_for_slice(html_slice, asset_manifest)

            sections.append({
                "name":       name,
                "html_slice": html_slice,
                "scoped_css": scoped_css,
                "assets":     assets,
            })

            size_flag = " ⚠️ LARGE" if len(html_slice) > MAX_SECTION_HTML else ""
            print(f"  ✂️  [{name}] html={len(html_slice)}ch  "
                  f"css={len(scoped_css)}ch  assets={len(assets)}{size_flag}")

        print(f"\n✅ Final sections ({len(sections)}): {[s['name'] for s in sections]}")

        return {
            "sections":   sections,
            "global_css": global_css,
            "logs":       [f"Splitter: {len(sections)} sections"],
        }

    except Exception as e:
        print(f"❌ Splitter node failed: {e}")
        return {
            "error": str(e),
            "logs":  [f"Splitter node failed: {e}"],
        }