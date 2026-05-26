"""
server.py — Flask server to serve the cloned site from ./cloned-site/

Usage:
    python server.py [port]
"""

import sys
import os
from pathlib import Path
from flask import Flask, send_from_directory, send_file, abort

OUTPUT_DIR = "cloned-site"
PORT       = int(sys.argv[1]) if len(sys.argv) > 1 else 5000

# ── Sanity check ──────────────────────────────────────────────────────────────
index_path = Path(OUTPUT_DIR) / "index.html"
if not index_path.exists():
    print(f"\n❌  No cloned site found at {index_path}")
    print("   Run:  python cloner.py <url>  first.\n")
    sys.exit(1)

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=OUTPUT_DIR)


@app.route("/")
def index():
    return send_from_directory(OUTPUT_DIR, "index.html")


@app.route("/assets/<path:filename>")
def assets(filename):
    assets_dir = os.path.join(OUTPUT_DIR, "assets")
    filepath   = os.path.join(assets_dir, filename)
    if os.path.exists(filepath):
        return send_from_directory(assets_dir, filename)
    abort(404)


@app.route("/screenshot.jpg")
def screenshot():
    path = os.path.join(OUTPUT_DIR, "screenshot.jpg")
    if os.path.exists(path):
        return send_file(path, mimetype="image/jpeg")
    abort(404)


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


if __name__ == "__main__":
    print("\n" + "─" * 50)
    print("  🌐  Cloned site running at:")
    print(f"      http://localhost:{PORT}")
    print("─" * 50)
    print("  Press Ctrl-C to stop.\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)