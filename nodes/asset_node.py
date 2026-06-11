# nodes/asset_node.py

"""
LangGraph Node 2 — Asset Processing + DOM Compression

Merges:
  - Original asset_node (CSS / font download, HTML rewriting, structure extraction)
  - Improved image/video downloader (deterministic names, URL sanitization,
    video poster extraction via ffmpeg, resolved_asset_map)

Output added to state:
  localised_html       – HTML with all asset URLs rewritten to local paths
  compressed_structure – page structure dict
  asset_manifest       – [{original, local}, ...]
  asset_count          – total downloaded
  asset_map            – original URL → local /assets/images/... path
  video_map            – original URL → {video, poster}
  resolved_asset_map   – local path → local path (safe for LLM prompts)
  css_content          – concatenated content of all downloaded CSS files
"""

import os
import re
import subprocess
import hashlib
import urllib.parse
import urllib.request
import requests

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from state import ClonerState


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

OUTPUT_DIR  = "cloned-site"
ASSETS_DIR  = os.path.join(OUTPUT_DIR, "assets")

# Sub-dirs for improved downloader layout
IMAGES_DIR  = os.path.join(ASSETS_DIR, "images")
VIDEOS_DIR  = os.path.join(ASSETS_DIR, "videos")
POSTERS_DIR = os.path.join(ASSETS_DIR, "posters")

TIMEOUT_SEC    = 20
MAX_WORKERS    = 8
MAX_HTML_CHARS = 20_000

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}


# ─────────────────────────────────────────────────────────────────────────────
# URL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _sanitize_url(url: str) -> str:
    """Percent-encode illegal characters (spaces, unicode) while preserving URL structure."""
    try:
        parsed = urllib.parse.urlparse(url)
        safe_path = urllib.parse.quote(parsed.path, safe="/:@!$&'()*+,;=")
        sanitized = urllib.parse.urlunparse((
            parsed.scheme, parsed.netloc, safe_path,
            parsed.params, parsed.query, parsed.fragment,
        ))
        if sanitized != url:
            print(f"  URL sanitized: {url!r} → {sanitized!r}")
        return sanitized
    except Exception:
        return url


def _resolve(url: str, base_url: str) -> str | None:
    try:
        full = urljoin(base_url, url)
        return full if full.startswith("http") else None
    except Exception:
        return None


def _safe_filename_hash(url: str, fallback_ext=".bin") -> str:
    """Hash-based filename — used for CSS and fonts."""
    parsed = urlparse(url)
    path   = parsed.path
    base   = os.path.basename(path)
    name, ext = os.path.splitext(base)
    if not ext:
        ext = fallback_ext
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:50]
    h = hashlib.md5(url.encode()).hexdigest()[:10]
    return f"{name}_{h}{ext}"


# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _download_urllib(url: str, dest_path: str) -> bool:
    """Download with urllib (used for images/videos — handles more edge cases)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            with open(dest_path, "wb") as f:
                f.write(resp.read())
        return True
    except Exception as e:
        print(f"  Download failed {url}: {e}")
        return False


def _download_requests(url: str, base_url: str) -> tuple[str, str] | None:
    """Download with requests — used for CSS/fonts, returns (original_url, local_path)."""
    full = _resolve(url, base_url)
    if not full:
        return None
    try:
        r = requests.get(full, timeout=TIMEOUT_SEC, headers=HEADERS)
        if r.status_code != 200:
            return None
        filename  = _safe_filename_hash(full)
        save_path = os.path.join(ASSETS_DIR, filename)
        with open(save_path, "wb") as f:
            f.write(r.content)
        return (url, f"./assets/{filename}")
    except Exception as e:
        print(f"  ❌ CSS/font download failed: {url} — {e}")
        return None


def _extract_poster(video_path: str, poster_path: str) -> bool:
    """Extract first frame of a video as poster image using ffmpeg."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", video_path,
             "-ss", "00:00:01", "-frames:v", "1", "-q:v", "2", poster_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        if result.returncode == 0 and os.path.exists(poster_path):
            print(f"  ✓ Poster extracted: {poster_path}")
            return True
        return False
    except FileNotFoundError:
        print("  ffmpeg not found — skipping poster extraction")
        return False
    except Exception as e:
        print(f"  Poster extraction error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# IMAGE DOWNLOADER  (deterministic names)
# ─────────────────────────────────────────────────────────────────────────────

def _download_images(image_urls: list[str], base_url: str) -> dict[str, str]:
    """
    Download images preserving their original filenames.
    If two URLs produce the same filename, append a numeric suffix to avoid collisions.
    Returns asset_map: original_url → local /assets/images/... path
    """
    asset_map = {}
    used_filenames: dict[str, int] = {}  # filename → count, for dedup

    for url in image_urls:
        if not url.startswith("http"):
            url = _resolve(url, base_url) or url
        url = _sanitize_url(url)

        # Derive filename from the URL path, strip query string
        parsed    = urlparse(url)
        basename  = os.path.basename(parsed.path)

        # Fallback: if URL has no usable basename, hash it
        if not basename or '.' not in basename:
            h = hashlib.md5(url.encode()).hexdigest()[:10]
            basename = f"image_{h}.jpg"

        # Normalise extension to lowercase
        name, ext = os.path.splitext(basename)
        ext = ext.lower()
        if not ext:
            ext = ".jpg"
        basename = name + ext

        # Dedup: logo.png → logo_2.png → logo_3.png …
        if basename in used_filenames:
            used_filenames[basename] += 1
            dedup_basename = f"{name}_{used_filenames[basename]}{ext}"
        else:
            used_filenames[basename] = 1
            dedup_basename = basename

        dest_path = os.path.join(IMAGES_DIR, dedup_basename)
        local_ref = f"./assets/images/{dedup_basename}"

        if os.path.exists(dest_path):
            print(f"  Cached: {dedup_basename}")
        else:
            ok = _download_urllib(url, dest_path)
            if not ok:
                asset_map[url] = ""
                continue
            print(f"  ✓ Image: {dedup_basename}")

        asset_map[url]       = local_ref
        asset_map[local_ref] = local_ref  # self-map

    return asset_map


# ─────────────────────────────────────────────────────────────────────────────
# VIDEO DOWNLOADER  (deterministic names + poster extraction)
# ─────────────────────────────────────────────────────────────────────────────

def _download_videos(video_urls: list[str], base_url: str) -> dict[str, dict]:
    """
    Download videos preserving original filenames + extract poster frames.
    Returns video_map: original_url → {video, poster}
    """
    video_map = {}
    used_filenames: dict[str, int] = {}

    for url in video_urls:
        if not url.startswith("http"):
            url = _resolve(url, base_url) or url
        url = _sanitize_url(url)

        parsed   = urlparse(url)
        basename = os.path.basename(parsed.path)

        if not basename or '.' not in basename:
            h = hashlib.md5(url.encode()).hexdigest()[:10]
            basename = f"video_{h}.mp4"

        name, ext = os.path.splitext(basename)
        ext = ext.lower()
        if not ext:
            ext = ".mp4"
        basename = name + ext

        if basename in used_filenames:
            used_filenames[basename] += 1
            dedup_basename = f"{name}_{used_filenames[basename]}{ext}"
        else:
            used_filenames[basename] = 1
            dedup_basename = basename

        dest_path   = os.path.join(VIDEOS_DIR, dedup_basename)
        local_ref   = f"./assets/videos/{dedup_basename}"

        poster_name = f"{name}_poster.jpg"
        poster_path = os.path.join(POSTERS_DIR, poster_name)
        poster_ref  = f"./assets/posters/{poster_name}"

        if not os.path.exists(dest_path):
            ok = _download_urllib(url, dest_path)
            if not ok:
                video_map[url] = {"video": "", "poster": ""}
                continue
            print(f"  ✓ Video: {dedup_basename}")

        if not os.path.exists(poster_path):
            _extract_poster(dest_path, poster_path)

        video_map[url] = {
            "video":  local_ref,
            "poster": poster_ref if os.path.exists(poster_path) else "",
        }

    return video_map


# ─────────────────────────────────────────────────────────────────────────────
# CSS / FONT DOWNLOADER  (kept from original asset_node)
# ─────────────────────────────────────────────────────────────────────────────

def _download_css_and_fonts(soup: BeautifulSoup, base_url: str) -> dict[str, str]:
    """
    Download <link rel="stylesheet">, <link rel="icon">, and web fonts.
    Returns downloaded: original_url → local ./assets/... path
    """
    css_font_urls = set()

    for link in soup.find_all("link"):
        rel  = link.get("rel", [])
        href = link.get("href")
        if href and ("stylesheet" in rel or "icon" in rel):
            css_font_urls.add(href)

    downloaded = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_download_requests, url, base_url): url
            for url in css_font_urls
        }
        for future in as_completed(futures):
            result = future.result()
            if result:
                original, local = result
                downloaded[original] = local

    return downloaded


def _rewrite_css_asset_urls(
    downloaded: dict[str, str],
    base_url: str,
):
    """
    Inside each downloaded CSS file, rewrite url(...) references to local paths.
    Also downloads any referenced assets (fonts, bg images) that were missed.
    """
    for original, local in list(downloaded.items()):
        if not local.endswith(".css"):
            continue

        full_path = os.path.join(OUTPUT_DIR, local.lstrip("./"))
        if not os.path.exists(full_path):
            continue

        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                css = f.read()

            urls_in_css = re.findall(r'url\(["\']?(.*?)["\']?\)', css)

            for asset_url in urls_in_css:
                if asset_url.startswith("data:"):
                    continue
                result = _download_requests(asset_url, base_url)
                if result:
                    orig, loc = result
                    css = css.replace(asset_url, loc.replace("./", "../"))
                    downloaded[orig] = loc

            with open(full_path, "w", encoding="utf-8") as f:
                f.write(css)

        except Exception as e:
            print(f"  ❌ CSS rewrite failed: {e}")


def _read_all_css(downloaded: dict[str, str]) -> str:
    """Concatenate all downloaded CSS file contents into one string."""
    chunks = []
    for local in downloaded.values():
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


# ─────────────────────────────────────────────────────────────────────────────
# STRUCTURE EXTRACTOR  (kept from original asset_node)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_structure(soup: BeautifulSoup) -> dict:
    structure = {
        "title": "",
        "headings": [],
        "buttons":  [],
        "paragraphs": [],
        "sections": 0,
        "images":   [],
    }
    if soup.title:
        structure["title"] = soup.title.get_text(strip=True)
    for h in soup.find_all(["h1", "h2", "h3"]):
        txt = h.get_text(strip=True)
        if txt:
            structure["headings"].append(txt)
    for b in soup.find_all(["button", "a"]):
        txt = b.get_text(strip=True)
        if txt and len(txt) < 50:
            structure["buttons"].append(txt)
    for p in soup.find_all("p"):
        txt = p.get_text(strip=True)
        if txt and len(txt) < 400:
            structure["paragraphs"].append(txt)
    for img in soup.find_all("img"):
        src = img.get("src")
        if src:
            structure["images"].append(src)
    structure["sections"] = len(soup.find_all("section"))
    return structure


def _extract_main_sections(soup: BeautifulSoup) -> str:
    important = []
    seen = set()
    selectors = ["header", "nav", "main", "section", "footer"]
    keywords  = [
        "hero", "footer", "about", "contact", "pricing",
        "testimonial", "feature", "services", "team", "cta",
        "faq", "banner", "grid", "cards",
    ]

    def add(html: str):
        key = hash(html)
        if key not in seen:
            seen.add(key)
            important.append(html[:50_000])

    for sel in selectors:
        for tag in soup.find_all(sel):
            add(str(tag))

    for div in soup.find_all("div"):
        classes = " ".join(div.get("class", []))
        text    = div.get_text(strip=True)
        if any(k in classes.lower() for k in keywords) or len(text) > 300:
            add(str(div))

    return "\n".join(important)


# ─────────────────────────────────────────────────────────────────────────────
# COLLECT ALL IMAGE / VIDEO URLS FROM SOUP
# ─────────────────────────────────────────────────────────────────────────────

def _collect_media_urls(soup: BeautifulSoup) -> tuple[list[str], list[str]]:
    image_urls = []
    video_urls = []
    seen = set()

    # <img src>
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if src and src not in seen and not src.startswith("data:"):
            seen.add(src)
            image_urls.append(src)

    # <source srcset> inside <picture>
    for source in soup.find_all("source"):
        srcset = source.get("srcset", "")
        for part in srcset.split(","):
            url = part.strip().split(" ")[0]
            if url and url not in seen and not url.startswith("data:"):
                seen.add(url)
                image_urls.append(url)

    # inline style background-image
    for tag in soup.find_all(style=True):
        urls = re.findall(r'url\(["\']?(.*?)["\']?\)', tag.get("style", ""))
        for url in urls:
            if url and url not in seen and not url.startswith("data:"):
                seen.add(url)
                image_urls.append(url)

    # <video src> and <source src> inside <video>
    for video in soup.find_all("video"):
        src = video.get("src")
        if src and src not in seen:
            seen.add(src)
            video_urls.append(src)
        for source in video.find_all("source"):
            src = source.get("src")
            if src and src not in seen:
                seen.add(src)
                video_urls.append(src)

    return image_urls, video_urls


# ─────────────────────────────────────────────────────────────────────────────
# REWRITE HTML ASSET PATHS
# ─────────────────────────────────────────────────────────────────────────────

def _rewrite_html(
    soup: BeautifulSoup,
    asset_map: dict[str, str],
    css_font_map: dict[str, str],
    video_map: dict[str, dict],
) -> BeautifulSoup:
    """Rewrite all asset URLs in the soup to their local paths."""

    # img src
    for img in soup.find_all("img"):
        src = img.get("src")
        if src and src in asset_map and asset_map[src]:
            img["src"] = asset_map[src]

    # link href (CSS / icons)
    for link in soup.find_all("link"):
        href = link.get("href")
        if href and href in css_font_map:
            link["href"] = css_font_map[href]

    # inline style url(...)
    for tag in soup.find_all(style=True):
        style = tag.get("style", "")
        urls  = re.findall(r'url\(["\']?(.*?)["\']?\)', style)
        for url in urls:
            if url.startswith("data:"):
                continue
            if url in asset_map and asset_map[url]:
                style = style.replace(url, asset_map[url])
        tag["style"] = style

    # video src
    for video in soup.find_all("video"):
        src = video.get("src")
        if src and src in video_map and video_map[src]["video"]:
            video["src"] = video_map[src]["video"]
            if video_map[src]["poster"]:
                video["poster"] = video_map[src]["poster"]
        for source in video.find_all("source"):
            src = source.get("src")
            if src and src in video_map and video_map[src]["video"]:
                source["src"] = video_map[src]["video"]

    return soup


# ─────────────────────────────────────────────────────────────────────────────
# MAIN NODE
# ─────────────────────────────────────────────────────────────────────────────

def asset_node(state: ClonerState) -> dict:
    try:
        # ── Setup dirs ────────────────────────────────────────────────────
        for d in [ASSETS_DIR, IMAGES_DIR, VIDEOS_DIR, POSTERS_DIR]:
            os.makedirs(d, exist_ok=True)

        html     = state["rendered_html"]
        base_url = state["target_url"]

        soup = BeautifulSoup(html, "html.parser")

        # Remove noise
        for tag in soup(["script", "svg", "noscript"]):
            tag.decompose()

        # ── 1. Collect all media URLs from the page ───────────────────────
        image_urls, video_urls = _collect_media_urls(soup)
        print(f"\n📸 Found {len(image_urls)} images, {len(video_urls)} videos")

        # ── 2. Download images (deterministic names) ──────────────────────
        asset_map = _download_images(image_urls, base_url)

        # ── 3. Download videos + extract posters ─────────────────────────
        video_map = _download_videos(video_urls, base_url)

        # ── 4. Download CSS and fonts ─────────────────────────────────────
        css_font_map = _download_css_and_fonts(soup, base_url)

        # ── 5. Rewrite url() inside CSS files ────────────────────────────
        _rewrite_css_asset_urls(css_font_map, base_url)

        # ── 6. Read all CSS into one string (for splitter_node) ───────────
        css_content = _read_all_css(css_font_map)
        print(f"🎨 Total CSS loaded: {len(css_content)} chars")

        # ── 7. Rewrite HTML asset paths ───────────────────────────────────
        soup = _rewrite_html(soup, asset_map, css_font_map, video_map)

        # ── 8. Extract structure + sections ──────────────────────────────
        structure    = _extract_structure(soup)

        # Full rewritten HTML — passed to splitter_node so NO sections are lost.
        # We use str(soup) to get the complete page with all asset paths rewritten.
        full_localised_html = str(soup)
        print(f"✅ Full localised HTML: {len(full_localised_html)} chars")

        # Summarised/compressed version kept only for legacy gemini_node use.
        cleaned_html = _extract_main_sections(soup)
        cleaned_html = re.sub(r"\s+", " ", cleaned_html)
        if len(cleaned_html) > MAX_HTML_CHARS:
            cleaned_html = cleaned_html[:MAX_HTML_CHARS]
        print(f"✅ Compressed HTML (legacy): {len(cleaned_html)} chars")

        # ── 9. Build unified asset_manifest ──────────────────────────────
        asset_manifest = []
        for original, local in asset_map.items():
            if local and local != original:  # skip self-maps
                asset_manifest.append({"original": original, "local": local})
        for original, local in css_font_map.items():
            asset_manifest.append({"original": original, "local": local})

        total = len([v for v in asset_map.values() if v]) + len(video_map) + len(css_font_map)

        # resolved_asset_map: what the LLM sees — only local paths
        resolved_asset_map = {v: v for v in asset_map.values() if v}

        print(f"\n✅ Total assets: {total}")
        print(f"   Images: {len([v for v in asset_map.values() if v])}")
        print(f"   Videos: {len(video_map)}")
        print(f"   CSS/fonts: {len(css_font_map)}")

        return {
            "localised_html":       full_localised_html,  # FULL html → splitter sees all sections
            "compressed_structure": structure,
            "asset_manifest":       asset_manifest,
            "asset_count":          total,
            "asset_map":            asset_map,
            "video_map":            video_map,
            "resolved_asset_map":   resolved_asset_map,
            "css_content":          css_content,
            "logs": [f"Downloaded {total} assets"],
        }

    except Exception as e:
        print(f"❌ Asset node failed: {e}")
        return {
            "error": str(e),
            "logs":  [f"Asset node failed: {e}"],
        }