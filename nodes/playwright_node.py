"""
nodes/playwright_node.py

LangGraph Node 1 — Playwright
  • Launches headless Chromium
  • Full scroll to trigger all lazy-loaded content
  • Captures full-page PNG screenshot (high quality for vision model)
  • Also captures viewport-only screenshot for vision model context
  • Returns rendered HTML + screenshot_b64 + page_title
"""
# nodes/playwright_node.py

import asyncio
import base64

from playwright.async_api import async_playwright

from state import ClonerState

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def _scrape(url: str):

    async with async_playwright() as pw:

        browser = await pw.chromium.launch(headless=True)

        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
        )

        page = await context.new_page()

        # ─────────────────────────────────────
        # Block heavy resources
        # ─────────────────────────────────────

        async def block(route):

            if route.request.resource_type in [
                "media",
            ]:
                await route.abort()

            else:
                await route.continue_()

        await page.route("**/*", block)

        print("🌐 Opening:", url)

        await page.goto(
            url,
            wait_until="networkidle",
            timeout=60000,
        )

        await page.wait_for_timeout(3000)

        # ─────────────────────────────────────
        # AUTO SCROLL ENTIRE PAGE
        # ─────────────────────────────────────

        print("📜 Scrolling page...")

        await page.evaluate("""
        async () => {

            await new Promise((resolve) => {

                let totalHeight = 0;
                const distance = 500;

                const timer = setInterval(() => {

                    window.scrollBy(0, distance);

                    totalHeight += distance;

                    if (
                        totalHeight >=
                        document.body.scrollHeight
                    ) {

                        clearInterval(timer);
                        resolve();

                    }

                }, 300);

            });

        }
        """)

        await page.wait_for_timeout(3000)

        # ─────────────────────────────────────
        # GET FULL RENDERED HTML
        # ─────────────────────────────────────

        html = await page.content()

        # ─────────────────────────────────────
        # SCREENSHOT
        # ─────────────────────────────────────

        screenshot = await page.screenshot(
            full_page=True,
            type="jpeg",
            quality=60,
        )

        screenshot_b64 = base64.b64encode(
            screenshot
        ).decode()

        title = await page.title()

        await browser.close()

        return {
            "rendered_html": html,
            "screenshot_b64": screenshot_b64,
            "page_title": title,
        }

def playwright_node(state: ClonerState):

    try:

        result = asyncio.run(
            _scrape(state["target_url"])
        )

        return {
            **result,
            "logs": ["Playwright scrape complete"],
        }

    except Exception as e:

        return {
            "error": str(e),
            "logs": [f"Playwright failed: {e}"],
        }