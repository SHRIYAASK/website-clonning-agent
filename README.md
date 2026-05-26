# 🕸️ Web Page Cloning Agent

> An AI-powered agent that clones any public website into a **fully offline, standalone HTML file** — complete with downloaded assets, preserved layout, and responsive design — using a **LangGraph pipeline** + **DeepSeek** for intelligent HTML reconstruction.

---

## ✨ What It Does

Give it any URL. It will:

1. **Scrape** the live page using headless Chromium (Playwright) — fully rendered, JavaScript-executed DOM
2. **Download** all assets (images, CSS, fonts, favicons, background images) locally
3. **Rewrite** all asset URLs to point to local files
4. **Send** the cleaned HTML + asset manifest to **DeepSeek** (`deepseek-chat`) to intelligently reconstruct the page
5. **Output** a single `cloned-site/index.html` with a companion `assets/` folder
6. **Serve** it locally via a Flask development server

---

## 🗂️ Project Structure

```
Web-page-clonning/
├── cloner.py              # CLI entry point — orchestrates the full pipeline
├── graph.py               # LangGraph pipeline definition (nodes + edges + conditionals)
├── state.py               # Shared TypedDict state flowing through every node
├── server.py              # Flask server to preview the cloned site
├── requirements.txt       # Python dependencies
├── .env                   # API keys (never commit this!)
│
├── nodes/
│   ├── playwright_node.py # Node 1 — Headless browser scraping + screenshot
│   ├── asset_node.py      # Node 2 — Asset download, URL rewriting, DOM compression
│   └── gemini_node.py     # Node 3 — DeepSeek AI reconstruction of the webpage
│
└── cloned-site/           # Output directory (auto-created)
    ├── index.html         # The cloned webpage
    ├── screenshot.jpg     # Full-page screenshot of the original
    ├── .cache.json        # Cached scrape data (used by --retry mode)
    └── assets/            # All downloaded images, CSS, fonts, etc.
```

---

## ⚙️ How the Pipeline Works

The agent is built on **LangGraph** — a graph-based orchestration framework for stateful AI pipelines. Each step is a **node** that reads from and writes to a shared `ClonerState` dict.

### Pipeline Flow

```
          ┌─────────────────────────────────────────────────────────┐
          │                    ClonerState (shared)                 │
          │  target_url, rendered_html, screenshot_b64,             │
          │  localised_html, asset_manifest, final_html, error ...  │
          └─────────────────────────────────────────────────────────┘

                                   START
                                     │
                          ┌──────────▼──────────┐
                          │   should_scrape()?   │   ← checks skip_scrape flag
                          └──────────┬──────────┘
                     "scrape"        │        "skip" (cache hit)
              ┌──────────────────────┘              │
              ▼                                     │
    ┌──────────────────┐                            │
    │  playwright_node │  (Node 1)                  │
    │  ─────────────── │                            │
    │  • Headless      │                            │
    │    Chromium      │                            │
    │  • Full scroll   │                            │
    │  • Screenshot    │                            │
    │  • Rendered HTML │                            │
    └────────┬─────────┘                            │
             │ error? → END                         │
             ▼                                      │
    ┌──────────────────┐                            │
    │   asset_node     │  (Node 2)                  │
    │  ─────────────── │                            │
    │  • Parse DOM     │                            │
    │  • Download      │                            │
    │    images/CSS/   │                            │
    │    fonts/icons   │                            │
    │  • Rewrite URLs  │                            │
    │  • Compress HTML │                            │
    │  • Build manifest│                            │
    └────────┬─────────┘                            │
             │ error? → END                         │
             └──────────────┐   ◄───────────────────┘
                            ▼
                  ┌──────────────────┐
                  │   gemini_node    │  (Node 3)
                  │  ─────────────── │
                  │  • Sends HTML +  │
                  │    asset list to │
                  │    DeepSeek API  │
                  │  • Throttles     │
                  │    requests      │
                  │  • Retries on    │
                  │    rate limits   │
                  │  • Extracts HTML │
                  │    from response │
                  └────────┬─────────┘
                           │
                          END → writes cloned-site/index.html
```

### Node Details

#### Node 1 — `playwright_node`
- Launches **headless Chromium** with a 1440×900 desktop viewport
- Spoofs a real Chrome `User-Agent` to avoid bot detection
- Blocks `media` resource types for speed
- **Auto-scrolls** the entire page to trigger lazy-loaded content
- Captures a **full-page JPEG screenshot** (base64-encoded, quality 60)
- Returns: `rendered_html`, `screenshot_b64`, `page_title`

#### Node 2 — `asset_node`
- Parses the rendered HTML with **BeautifulSoup**
- Strips `<script>`, `<svg>`, and `<noscript>` tags to reduce noise
- Collects all asset URLs: `<img src>`, `<link rel=stylesheet>`, `<link rel=icon>`, inline `style="background: url(...)"`, and `url()` refs inside downloaded CSS files
- Downloads all assets **concurrently** (8 threads, 20s timeout)
- Saves each asset with a `name_<md5hash>.ext` safe filename to `cloned-site/assets/`
- Rewrites all URLs in HTML and CSS to point to `./assets/...`
- Extracts **important DOM sections** (header, nav, main, section, footer + keyword-matched divs)
- Compresses the HTML to ≤20,000 chars for the AI prompt
- Builds an **asset manifest** (list of `{original, local}` pairs for DeepSeek)
- Returns: `localised_html`, `compressed_structure`, `asset_manifest`, `asset_count`

#### Node 3 — `gemini_node` *(actually powered by DeepSeek)*
- Constructs a structured prompt with:
  - List of available local asset paths
  - Extracted page structure (title, headings, buttons, paragraphs, sections)
  - Compressed reference HTML
- Calls **`deepseek-chat`** via the DeepSeek API (`temperature=0.2`, `max_tokens=2500`)
- Implements **request throttling** (minimum 10s between calls, thread-safe)
- **Retries** on 429 (rate limit, 30s wait) and 5xx (server error, 20s wait)
- Strips markdown code fences from the response and extracts the `<!DOCTYPE html>...</html>` block
- Writes `cloned-site/index.html`

### Caching / Retry Mode

After a successful scrape, the pipeline caches `rendered_html`, `screenshot_b64`, `page_title`, `localised_html`, and `asset_count` into `cloned-site/.cache.json`.

If the DeepSeek call fails (e.g. rate limit), you can re-run with `--retry` to **skip Playwright and asset downloading entirely** and jump straight to the AI step using the cached data — saving time and bandwidth.

```
Normal flow:  START → playwright → assets → gemini → END
Retry flow:   START ──────────────────────→ gemini → END
                     (cache hit detected)
```

---

## 🚀 Setup & Installation

### Prerequisites

- **Python 3.10+**
- **pip** or a virtual environment manager

### 1. Clone the repository

```bash
git clone <your-repo-url>
cd Web-page-clonning
```

### 2. Create a virtual environment

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# Mac / Linux
python -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Install Playwright browsers

```bash
playwright install chromium
```

### 5. Configure API keys

Create a `.env` file in the project root (or set environment variables):

```env
DEEPSEEK_API_KEY=sk-...
OPENROUTER_API_KEY=sk-or-...
```

| Key | Where to get it |
|-----|----------------|
| `DEEPSEEK_API_KEY` | [platform.deepseek.com](https://platform.deepseek.com) |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) |

> **Windows PowerShell alternative** (no `.env` file needed):
> ```powershell
> $env:DEEPSEEK_API_KEY = "sk-..."
> $env:OPENROUTER_API_KEY = "sk-or-..."
> ```

---

## 📖 How to Use

### Clone a website

```bash
python cloner.py https://example.com
```

Or without the `https://` prefix — it'll be added automatically:

```bash
python cloner.py example.com
```

### Re-run AI reconstruction only (skip scraping)

Use this if the DeepSeek call failed but the scrape succeeded:

```bash
python cloner.py https://example.com --retry
```

### Preview the cloned site

After cloning, start the local Flask server:

```bash
python server.py
```

Then open your browser at: **http://localhost:5000**

You can also specify a custom port:

```bash
python server.py 8080
```

---

## 📤 Output

After a successful run, you'll find:

```
cloned-site/
├── index.html       ← The AI-reconstructed webpage
├── screenshot.jpg   ← Full-page screenshot of the original
├── .cache.json      ← Scrape cache (enables --retry mode)
└── assets/
    ├── logo_abc123.png
    ├── style_def456.css
    ├── font_ghi789.woff2
    └── ...           ← All downloaded assets
```

The terminal will print a summary:

```
──────────────────────────────────────────────────
  ✅  Done!
  📄  cloned-site/index.html
  📸  cloned-site/screenshot.jpg
  📦  cloned-site/assets/  (42 files)
──────────────────────────────────────────────────

  Run the server:
      python server.py
  Then open: http://localhost:5000
```

---

## 🔧 Configuration

Key constants you can adjust:

| File | Constant | Default | Description |
|------|----------|---------|-------------|
| `nodes/asset_node.py` | `MAX_WORKERS` | `8` | Concurrent asset download threads |
| `nodes/asset_node.py` | `TIMEOUT_SEC` | `20` | Per-asset download timeout (seconds) |
| `nodes/asset_node.py` | `MAX_HTML_CHARS` | `20000` | Max compressed HTML chars sent to AI |
| `nodes/gemini_node.py` | `MIN_REQUEST_INTERVAL` | `10` | Min seconds between DeepSeek API calls |
| `nodes/gemini_node.py` | `MODEL` | `deepseek-chat` | DeepSeek model to use |
| `nodes/gemini_node.py` | `MAX_TOKENS` | `2500` | Max tokens in DeepSeek response |
| `nodes/playwright_node.py` | viewport | `1440×900` | Browser viewport size for scraping |

---

## 🐛 Troubleshooting

### `OPENROUTER_API_KEY is not set` / `DEEPSEEK_API_KEY is not set`
Make sure your `.env` file exists at the project root and contains both keys, or set them as environment variables before running.

### `playwright install` — browser not found
Run `playwright install chromium` to download the headless browser binary.

### DeepSeek rate limit (429)
The agent auto-waits 30 seconds and retries. If it still fails:
```bash
python cloner.py <url> --retry   # skip scraping, hit DeepSeek only
```

### Cloned site looks incomplete or broken
- The AI reconstruction is limited to `max_tokens=2500` — very large pages may be truncated. Try increasing `MAX_TOKENS` in `gemini_node.py`.
- Some assets (behind auth, CDN restrictions) may fail to download — the agent logs these with ❌.
- Dynamic SPA content (React/Vue/Angular) that requires user interaction may not be fully captured.

### `No cloned site found` when running `server.py`
Run `python cloner.py <url>` first to generate the `cloned-site/` output.

---

## 📦 Dependencies

| Package | Purpose |
|---------|---------|
| `langgraph>=0.2.0` | Graph-based pipeline orchestration |
| `langchain>=0.2.0` | LangChain core (used by LangGraph) |
| `langchain-google-genai>=1.0.0` | Google Generative AI integration |
| `playwright>=1.44.0` | Headless browser scraping |
| `beautifulsoup4>=4.12.0` | HTML parsing and DOM manipulation |
| `requests>=2.31.0` | Asset downloading + DeepSeek API calls |
| `flask>=3.0.0` | Local preview server |
| `python-dotenv>=1.0.0` | `.env` file loading |
| `Pillow>=10.0.0` | Image processing utilities |

---

## 📝 License

MIT — use freely, modify liberally, clone responsibly.

---

> **Note:** Only clone websites you have permission to clone. Respect `robots.txt`, terms of service, and copyright. This tool is intended for educational, archival, and legitimate development purposes.
