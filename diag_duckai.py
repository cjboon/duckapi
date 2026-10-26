#!/usr/bin/env python3
"""See what Duck.ai shows a fresh browser - same browser and settings as the relay.

Run it with the project's interpreter (the one run.py prints as "Interpreter:"):

    .venv/bin/python diag_duckai.py            # opens a VISIBLE window and waits for Enter
    .venv/bin/python diag_duckai.py --headless # no window; prints the report and saves a screenshot

It loads the page and reads it. It does not send any chat message.
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()
from duckai import BASE, CHROME_PATH, REAL_UA  # noqa: E402  (reads DUCKAI_CHROME_PATH from .env)
from playwright.async_api import async_playwright  # noqa: E402

REPORT_JS = r"""
() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const ta = document.querySelector('textarea');
  const dialogs = [...document.querySelectorAll('[role=dialog], dialog, [aria-modal=true]')]
      .filter(vis).map(d => (d.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 400));
  const buttons = [...document.querySelectorAll('button')].filter(vis)
      .map(b => ((b.innerText || b.getAttribute('aria-label') || '')).replace(/\s+/g, ' ').trim())
      .filter(Boolean).slice(0, 20);
  return {
    url: location.href,
    title: document.title,
    textarea: ta ? {disabled: ta.disabled, readOnly: ta.readOnly, maxlength: ta.getAttribute('maxlength'),
                    placeholder: ta.getAttribute('placeholder')} : null,
    dialogs, buttons,
    body: (document.body.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 700),
  };
}
"""


async def main() -> None:
    headless = "--headless" in sys.argv
    launch = {"headless": headless, "args": ["--disable-blink-features=AutomationControlled"]}
    if CHROME_PATH and os.path.exists(CHROME_PATH):
        launch["executable_path"] = CHROME_PATH
    else:
        launch["channel"] = "chrome"
    print("browser:", launch.get("executable_path", "channel=chrome"), "| headless:", headless)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch)
        ctx = await browser.new_context(user_agent=REAL_UA)
        page = await ctx.new_page()
        await page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000)
        info = await page.evaluate(REPORT_JS)
        os.makedirs("debug", exist_ok=True)
        await page.screenshot(path="debug/diag.png")

        print("\nurl      :", info["url"])
        print("title    :", info["title"])
        print("textarea :", info["textarea"])
        print("dialogs  :", info["dialogs"] or "none visible")
        print("buttons  :", info["buttons"])
        print("page text:", info["body"])
        print("\nscreenshot saved to debug/diag.png")
        if not headless:
            input("\nLook at the window, then press Enter to close it... ")
        await browser.close()


asyncio.run(main())
