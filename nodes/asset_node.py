# nodes/asset_node.py

"""
LangGraph Node 2 — Asset Processing + DOM Compression

Features:
- Parses rendered DOM
- Downloads/localizes:
    • images
    • CSS
    • fonts
    • favicons
    • background images
- Rewrites all asset URLs
- Extracts meaningful sections
- Preserves CSS/layout responsiveness
- Builds asset manifest for DeepSeek
"""

import os
import re
import hashlib
import requests

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed
)

from state import ClonerState


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

OUTPUT_DIR = "cloned-site"
ASSETS_DIR = os.path.join(
    OUTPUT_DIR,
    "assets"
)

TIMEOUT_SEC = 20
MAX_WORKERS = 8
MAX_HTML_CHARS =20000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64)"
    )
}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _safe_filename(
    url: str,
    fallback_ext=".bin"
):

    parsed = urlparse(url)

    path = parsed.path

    base = os.path.basename(path)

    name, ext = os.path.splitext(base)

    if not ext:
        ext = fallback_ext

    name = re.sub(
        r"[^a-zA-Z0-9_-]",
        "_",
        name
    )[:50]

    h = hashlib.md5(
        url.encode()
    ).hexdigest()[:10]

    return f"{name}_{h}{ext}"


def _resolve(
    url: str,
    base_url: str
):

    try:

        full = urljoin(
            base_url,
            url
        )

        if full.startswith("http"):
            return full

        return None

    except:
        return None


def _download(
    url: str,
    base_url: str
):

    full = _resolve(
        url,
        base_url
    )

    if not full:
        return None

    try:

        r = requests.get(
            full,
            timeout=TIMEOUT_SEC,
            headers=HEADERS,
        )

        if r.status_code != 200:
            return None

        filename = _safe_filename(full)

        save_path = os.path.join(
            ASSETS_DIR,
            filename
        )

        with open(save_path, "wb") as f:
            f.write(r.content)

        return (
            url,
            f"./assets/{filename}"
        )

    except Exception as e:

        print(
            "❌ Download failed:",
            url,
            e
        )

        return None


# ─────────────────────────────────────────────
# EXTRACT IMPORTANT SECTIONS
# ─────────────────────────────────────────────

def extract_main_sections(soup):

    important = []
    seen = set()

    selectors = [
        "header",
        "nav",
        "main",
        "section",
        "footer",
    ]

    keywords = [
        "hero",
        "footer",
        "about",
        "contact",
        "pricing",
        "testimonial",
        "feature",
        "services",
        "team",
        "cta",
        "faq",
        "banner",
        "grid",
        "cards",
    ]

    def add_html(html):

        key = hash(html)

        if key not in seen:

            seen.add(key)

            if len(html) > 50000:
                html = html[:50000]

            important.append(html)

    # semantic tags
    for selector in selectors:

        for tag in soup.find_all(selector):

            add_html(str(tag))

    # important divs
    for div in soup.find_all("div"):

        classes = " ".join(
            div.get("class", [])
        )

        text = div.get_text(
            strip=True
        )

        if (
            any(
                k in classes.lower()
                for k in keywords
            )
            or len(text) > 300
        ):

            add_html(str(div))

    return "\n".join(important)


# ─────────────────────────────────────────────
# EXTRACT STRUCTURE
# ─────────────────────────────────────────────

def extract_structure(soup):

    structure = {
        "title": "",
        "headings": [],
        "buttons": [],
        "paragraphs": [],
        "sections": 0,
        "images": [],
    }

    if soup.title:

        structure["title"] = (
            soup.title.get_text(
                strip=True
            )
        )

    for h in soup.find_all([
        "h1",
        "h2",
        "h3"
    ]):

        txt = h.get_text(
            strip=True
        )

        if txt:
            structure["headings"].append(
                txt
            )

    for b in soup.find_all([
        "button",
        "a"
    ]):

        txt = b.get_text(
            strip=True
        )

        if txt and len(txt) < 50:

            structure["buttons"].append(
                txt
            )

    for p in soup.find_all("p"):

        txt = p.get_text(
            strip=True
        )

        if txt and len(txt) < 400:

            structure["paragraphs"].append(
                txt
            )

    for img in soup.find_all("img"):

        src = img.get("src")

        if src:

            structure["images"].append(
                src
            )

    structure["sections"] = len(
        soup.find_all("section")
    )

    return structure


# ─────────────────────────────────────────────
# MAIN NODE
# ─────────────────────────────────────────────

def asset_node(state: ClonerState):

    try:

        os.makedirs(
            ASSETS_DIR,
            exist_ok=True
        )

        html = state["rendered_html"]

        base_url = state["target_url"]

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        # ─────────────────────────
        # REMOVE HEAVY TAGS
        # ─────────────────────────

        for tag in soup([
            "script",
            "svg",
            "noscript",
        ]):
            tag.decompose()

        # remove comments
        for comment in soup.find_all(
            string=lambda text:
            isinstance(text, str)
            and "<!--" in text
        ):
            comment.extract()

        # ─────────────────────────
        # COLLECT ASSETS
        # ─────────────────────────

        asset_urls = set()

        # images
        for img in soup.find_all("img"):

            src = img.get("src")

            if src:
                asset_urls.add(src)

        # CSS + icons
        for link in soup.find_all("link"):

            rel = link.get("rel", [])

            href = link.get("href")

            if href and (
                "stylesheet" in rel
                or "icon" in rel
            ):

                asset_urls.add(href)

        print(
            f"📦 Found "
            f"{len(asset_urls)} assets"
        )

        # ─────────────────────────
        # DOWNLOAD ASSETS
        # ─────────────────────────

        downloaded = {}

        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        ) as executor:

            futures = [

                executor.submit(
                    _download,
                    url,
                    base_url,
                )

                for url in asset_urls
            ]

            for future in as_completed(
                futures
            ):

                result = future.result()

                if result:

                    original, local = result

                    downloaded[
                        original
                    ] = local

        print(
            f"✅ Downloaded "
            f"{len(downloaded)} assets"
        )

        # ─────────────────────────
        # REWRITE IMG PATHS
        # ─────────────────────────

        for img in soup.find_all("img"):

            src = img.get("src")

            if src in downloaded:

                img["src"] = downloaded[
                    src
                ]

        # ─────────────────────────
        # REWRITE LINK PATHS
        # ─────────────────────────

        for link in soup.find_all("link"):

            href = link.get("href")

            if href in downloaded:

                link["href"] = downloaded[
                    href
                ]

        # ─────────────────────────
        # REWRITE INLINE STYLE URLS
        # ─────────────────────────

        for tag in soup.find_all(
            style=True
        ):

            style = tag.get("style", "")

            urls = re.findall(
                r'url\(["\']?(.*?)["\']?\)',
                style
            )

            for asset in urls:

                if asset.startswith(
                    "data:"
                ):
                    continue

                downloaded_asset = _download(
                    asset,
                    base_url
                )

                if downloaded_asset:

                    original, local = (
                        downloaded_asset
                    )

                    style = style.replace(
                        asset,
                        local
                    )

            tag["style"] = style

        # ─────────────────────────
        # PROCESS CSS FILES
        # ─────────────────────────

        css_files = []

        for original, local in downloaded.items():

            if local.endswith(".css"):

                css_files.append(local)

        for css_path in css_files:

            full_css_path = os.path.join(
                OUTPUT_DIR,
                css_path.replace(
                    "./",
                    ""
                )
            )

            if not os.path.exists(
                full_css_path
            ):
                continue

            try:

                with open(
                    full_css_path,
                    "r",
                    encoding="utf-8",
                    errors="ignore"
                ) as f:

                    css_content = f.read()

                urls = re.findall(
                    r'url\(["\']?(.*?)["\']?\)',
                    css_content
                )

                for asset in urls:

                    if asset.startswith(
                        "data:"
                    ):
                        continue

                    downloaded_asset = _download(
                        asset,
                        base_url,
                    )

                    if downloaded_asset:

                        (
                            original_asset,
                            local_asset
                        ) = downloaded_asset

                        css_content = (
                            css_content.replace(
                                asset,
                                local_asset.replace(
                                    "./",
                                    "../"
                                )
                            )
                        )

                with open(
                    full_css_path,
                    "w",
                    encoding="utf-8"
                ) as f:

                    f.write(css_content)

            except Exception as e:

                print(
                    "❌ CSS rewrite failed:",
                    e
                )

        # ─────────────────────────
        # EXTRACT STRUCTURE
        # ─────────────────────────

        structure = extract_structure(
            soup
        )

        # ─────────────────────────
        # EXTRACT IMPORTANT HTML
        # ─────────────────────────

        cleaned_html = (
            extract_main_sections(
                soup
            )
        )

        cleaned_html = re.sub(
            r"\s+",
            " ",
            cleaned_html
        )

        if len(cleaned_html) > MAX_HTML_CHARS:

            cleaned_html = cleaned_html[
                :MAX_HTML_CHARS
            ]

        print(
            f"✅ Cleaned HTML size:"
            f" {len(cleaned_html)} chars"
        )

        # ─────────────────────────
        # BUILD ASSET MANIFEST
        # ─────────────────────────

        asset_manifest = []

        for original, local in downloaded.items():

            asset_manifest.append({
                "original": original,
                "local": local,
            })

        # ─────────────────────────
        # RETURN STATE
        # ─────────────────────────

        return {

            "localised_html":
                cleaned_html,

            "compressed_structure":
                structure,

            "asset_manifest":
                asset_manifest,

            "asset_count":
                len(downloaded),

            "logs": [
                f"Downloaded "
                f"{len(downloaded)} assets"
            ],
        }

    except Exception as e:

        return {
            "error": str(e),
            "logs": [
                f"Asset node failed: {e}"
            ],
        }