# nodes/gemini_node.py

"""
LangGraph Node 3 — DeepSeek Website Reconstruction

Features:
- Sends compressed semantic HTML to DeepSeek
- Uses localized assets only
- Throttles requests
- Handles retries/rate limits
- Extracts valid HTML only
- Saves standalone responsive webpage
"""

import os
import re
import time
import threading
import requests

from state import ClonerState


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

DEEPSEEK_URL = (
    "https://api.deepseek.com/chat/completions"
)

MODEL = "deepseek-chat"

DEEPSEEK_API_KEY = os.getenv(
    "DEEPSEEK_API_KEY"
)

OUTPUT_DIR = "cloned-site"

# request throttling
_last_call_time = 0

_rate_lock = threading.Lock()

MIN_REQUEST_INTERVAL = 10


# ─────────────────────────────────────────────
# EXTRACT HTML ONLY
# ─────────────────────────────────────────────
def extract_html(text):

    text = re.sub(
        r"```html",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = text.replace(
        "```",
        ""
    )

    text = text.strip()

    match = re.search(
        r'<!DOCTYPE html>.*?</html>',
        text,
        re.DOTALL | re.IGNORECASE
    )

    if match:
        return match.group(0)

    start = text.find("<!DOCTYPE html>")

    if start != -1:

        partial = text[start:]

        if "</body>" not in partial:
            partial += "\n</body>"

        if "</html>" not in partial:
            partial += "\n</html>"

        return partial

    return f"""
<!DOCTYPE html>
<html>
<body>
<pre>{text}</pre>
</body>
</html>
"""

# ─────────────────────────────────────────────
# API RETRY + THROTTLING
# ─────────────────────────────────────────────

def call_with_retry(
    payload,
    headers,
    retries=2
):

    global _last_call_time

    for attempt in range(retries):

        # ─────────────────────
        # THROTTLING
        # ─────────────────────

        with _rate_lock:

            elapsed = (
                time.time()
                - _last_call_time
            )

            if elapsed < MIN_REQUEST_INTERVAL:

                wait_time = (
                    MIN_REQUEST_INTERVAL
                    - elapsed
                )

                print(
                    f"⏳ Waiting "
                    f"{wait_time:.1f}s"
                )

                time.sleep(wait_time)

            _last_call_time = time.time()

        # ─────────────────────
        # REQUEST
        # ─────────────────────

        r = requests.post(
            DEEPSEEK_URL,
            json=payload,
            headers=headers,
            timeout=300,
        )

        print(
            "📡 Status:",
            r.status_code
        )

        # ─────────────────────
        # RATE LIMIT
        # ─────────────────────

        if r.status_code == 429:

            wait = 30

            print(
                f"⚠️ Rate limited."
                f" Waiting {wait}s"
            )

            time.sleep(wait)

            continue

        # ─────────────────────
        # AUTH / GOVERNOR
        # ─────────────────────

        if r.status_code in [
            401,
            403
        ]:

            print(r.text)

            raise Exception(
                "Authentication / "
                "governor protection triggered"
            )

        # ─────────────────────
        # SERVER FAILURES
        # ─────────────────────

        if r.status_code >= 500:

            wait = 20

            print(
                f"⚠️ Server issue."
                f" Waiting {wait}s"
            )

            time.sleep(wait)

            continue

        # ─────────────────────
        # OTHER FAILURES
        # ─────────────────────

        if r.status_code != 200:

            print(r.text)

            r.raise_for_status()

        return r.json()

    raise Exception(
        "Too many retries"
    )


# ─────────────────────────────────────────────
# MAIN NODE
# ─────────────────────────────────────────────

def gemini_node(state: ClonerState):

    try:

        assets = state.get(
            "asset_manifest",
            []
        )

        structure = state.get(
            "compressed_structure",
            {}
        )

        html = state.get(
            "localised_html",
            ""
        )

        # ─────────────────────
        # CLEAN ASSET MANIFEST
        # ─────────────────────

        asset_text = "\n".join([
            f"{x['local']}"
            for x in assets[:30]
        ])

        # ─────────────────────
        # PROMPT
        # ─────────────────────

        prompt = f"""
Recreate this website as a standalone responsive HTML page.

RULES:
- Use ONLY provided local assets
- Never generate placeholder SVGs
- Never use data:image URLs
- Never invent filenames
- Never use remote URLs
- Preserve responsiveness
- Preserve spacing/layout
- Preserve typography/colors
- Return ONLY HTML
- Start with <!DOCTYPE html>

AVAILABLE ASSETS:
{asset_text}


REFERENCE STRUCTURE:
{structure}

REFERENCE HTML:
{html}
"""

        # ─────────────────────
        # PAYLOAD
        # ─────────────────────

        payload = {

            "model": MODEL,

            "messages": [

                {
                    "role": "system",
                    "content": (
                        "You are an elite "
                        "frontend engineer."
                    )
                },

                {
                    "role": "user",
                    "content": prompt
                }

            ],

            "temperature": 0.2,

            "max_tokens": 2500,
        }

        headers = {

            "Authorization":
                f"Bearer {DEEPSEEK_API_KEY}",

            "Content-Type":
                "application/json",
        }

        print(
            "🧠 Generating "
            "clone with DeepSeek..."
        )

        # ─────────────────────
        # API CALL
        # ─────────────────────

        data = call_with_retry(
            payload,
            headers
        )

        # ─────────────────────
        # EXTRACT OUTPUT
        # ─────────────────────

        raw_output = (
            data["choices"][0]
            ["message"]["content"]
        )
        print("\n========== RAW OUTPUT START ==========\n")

        print(raw_output[:2000])

        print("\n========== RAW OUTPUT END ==========\n")

        print(raw_output[-2000:])

        final_html = extract_html(
            raw_output
        )

        # ─────────────────────
        # SAVE OUTPUT
        # ─────────────────────

        os.makedirs(
            OUTPUT_DIR,
            exist_ok=True
        )

        output_path = os.path.join(
            OUTPUT_DIR,
            "index.html"
        )

        with open(
            output_path,
            "w",
            encoding="utf-8"
        ) as f:

            f.write(final_html)

        print(
            "✅ Clone generated:"
        )

        print(output_path)

        return {

            "final_html":
                final_html,

            "logs": [
                "Clone generated "
                "successfully"
            ],
        }

    except Exception as e:

        print(
            "❌ Gemini node failed:",
            e
        )

        return {

            "error":
                str(e),

            "logs": [
                f"Gemini node failed: {e}"
            ],
        }