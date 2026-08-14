"""
sf_capture.py
=============
Headless-browser capture of the S&F guild-attack report from network traffic.

WHY THIS IS THE ONLY WAY (recap):
    - sf-api Rust crate: does NOT expose the guild-reports mail tab.
    - Unity WebGL client: the whole UI is a <canvas>, so no DOM to scrape.
    - BUT the client fetches mail data as plaintext over the network before
      painting it — so we run the real client headless and intercept that
      response. Parsing is done by sf_absence.parse_absent (already validated).

HONEST STATUS:
    The PARSER is proven against two real reports. This CAPTURE layer is
    written against observed behavior but has NOT been run end-to-end headless
    from here — the game's exact in-client navigation to open a guild report
    (which is what produces the 'messagetext.s' response) happens inside the
    Unity canvas and may need coordinate-clicks or an in-client trigger we
    can't fully predict without live testing. Run `python3 sf_capture.py test
    <server> <login> <pass>` FIRST and confirm it captures a report before
    wiring this into the hourly Discord loop.

STRATEGY:
    We attach a response listener to the page that inspects EVERY game-server
    response for the 'messagetext.s:' marker, and stash the first one that is
    a guild-attack report (type code '2a'). We don't assume which request
    carries it — we watch them all. After login we give the client time and,
    if needed, drive it toward the mailbox. If no report response appears
    within a timeout, we report that cleanly rather than hanging.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from sf_absence import extract_section, parse_absent

# Guild-attack reports use messagetext type code "2a" (observed in both
# validated samples: "2a/Evil Returns/..." and "2a/SANSIBAR/...").
ATTACK_TYPE_CODE = "2a"

PLAY_URL = "https://sfgame.net/play"

# How long (seconds) to wait after login for a report response to show up.
CAPTURE_TIMEOUT = 45


async def capture_report(server: str, login: str, password: str,
                         headless: bool = True, debug: bool = False) -> Optional[dict]:
    """
    Launch the client headless, log in, and intercept the guild-attack report
    network response. Returns a dict:
        {"opponent": str, "absent": [str], "raw": str}
    or None if no report was captured within CAPTURE_TIMEOUT.

    Raises only on hard failures (browser launch, navigation). Login/nav
    problems resolve to None so the caller's loop never crashes.
    """
    from playwright.async_api import async_playwright

    captured: dict = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})

            async def on_response(resp):
                # Only look at game-server responses; ignore images/fonts/etc.
                try:
                    ctype = resp.headers.get("content-type", "")
                    if "image" in ctype or "font" in ctype:
                        return
                    body = await resp.text()
                except Exception:
                    return
                if "messagetext.s:" not in body:
                    return
                section = extract_section(body, "messagetext.s")
                # Is this specifically a guild-attack report?
                if section.split("/", 1)[0] != ATTACK_TYPE_CODE:
                    if debug:
                        print(f"  (saw messagetext type {section.split('/',1)[0]}, not an attack report)")
                    return
                opponent, absent = parse_absent(section)
                if not captured:  # keep the first attack report we see
                    captured.update({"opponent": opponent, "absent": absent, "raw": body})
                    if debug:
                        print(f"  CAPTURED attack report vs {opponent}: {len(absent)} absent")

            page.on("response", lambda r: asyncio.create_task(on_response(r)))

            if debug:
                print(f"Navigating to {PLAY_URL} ...")
            await page.goto(PLAY_URL, wait_until="domcontentloaded", timeout=60000)

            # The client boots slowly. Give it time to load and auto-fetch mail.
            # NOTE: whether the guild-report 'messagetext.s' is fetched
            # automatically on login, or only when the report is opened in the
            # canvas UI, is the KEY UNKNOWN. We poll for up to CAPTURE_TIMEOUT.
            waited = 0
            step = 3
            while waited < CAPTURE_TIMEOUT and not captured:
                await page.wait_for_timeout(step * 1000)
                waited += step
                if debug and waited % 9 == 0:
                    print(f"  ...waited {waited}s, still watching for a report response")

            return captured or None
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# STANDALONE TEST — run this BEFORE wiring into Discord.
#   python3 sf_capture.py test s20.sfgame.eu LOGIN PASSWORD
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 5 and sys.argv[1] == "test":
        _server, _login, _pass = sys.argv[2], sys.argv[3], sys.argv[4]
        result = asyncio.run(capture_report(_server, _login, _pass, headless=True, debug=True))
        if result:
            print("\n=== CAPTURE OK ===")
            print("opponent:", result["opponent"])
            print("absent  :", result["absent"])
        else:
            print("\n=== NO REPORT CAPTURED ===")
            print("The client didn't emit a guild-attack 'messagetext.s' response within",
                  CAPTURE_TIMEOUT, "seconds.")
            print("This likely means the report is only fetched when OPENED in the")
            print("canvas UI — which needs an in-client trigger we can't send blindly.")
            print("Tell Claude this result; it changes the navigation approach.")
    else:
        print("Usage: python3 sf_capture.py test <server> <login> <password>")
