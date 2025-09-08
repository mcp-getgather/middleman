#!/usr/bin/env python3

import argparse
import asyncio
import os
import re
import sys
import urllib.parse
from glob import glob
from typing import Dict, List, Optional, TypedDict, cast

from bs4 import BeautifulSoup
from bs4.element import Tag
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
import nanoid
from patchright.async_api import BrowserContext, Locator, Page, async_playwright
import pwinput
import uvicorn


MIDDLEMAN_DEBUG = os.getenv("MIDDLEMAN_DEBUG")
MIDDLEMAN_PAUSE = os.getenv("MIDDLEMAN_PAUSE")

NORMAL = "\033[0m"
BOLD = "\033[1m"
YELLOW = "\033[93m"
MAGENTA = "\033[35m"
RED = "\033[91m"
GREEN = "\033[92m"
CYAN = "\033[36m"
GRAY = "\033[90m"

ARROW = "⇢"
CHECK = "✓"
CROSS = "✘"

FRIENDLY_CHARS = "23456789abcdefghijkmnpqrstuvwxyz"


async def sleep(seconds: float):
    await asyncio.sleep(seconds)


async def pause():
    input("Press Enter to continue...")


def get_selector(input_selector: str):
    pattern = r"^(iframe(?:[^\s]*\[[^\]]+\]|[^\s]+))\s+(.+)$"
    match = re.match(pattern, input_selector)
    if not match:
        return input_selector, None
    return match.group(2), match.group(1)


async def ask(message: str, mask: Optional[str] = None) -> str:
    if mask:
        return pwinput.pwinput(f"{message}: ", mask=mask)
    else:
        return input(f"{message}: ")


async def locate(locator: Locator) -> Optional[Locator]:
    count = await locator.count()
    if count > 0:
        for i in range(count):
            el = locator.nth(i)
            if await el.is_visible():
                return el
    return None


async def click(page: Page, selector: str, timeout: int = 3000, frame_selector: str | None = None) -> None:
    LOCATOR_ALL_TIMEOUT = 100
    if frame_selector:
        locator = page.frame_locator(str(frame_selector)).locator(str(selector))
    else:
        locator = page.locator(str(selector))
    try:
        elements = await locator.all()
        print(f'Found {len(elements)} elements for selector "{selector}"')
        for element in elements:
            print("Checking", element)
            if await element.is_visible():
                print("Clicking on", element)
                try:
                    await element.click()
                    return
                except Exception as err:
                    print(f"Failed to click on {selector} {element}: {err}")
    except Exception as e:
        if timeout > 0 and "TimeoutError" in str(type(e)):
            print(f"retrying click {selector} {timeout}")
            await click(page, selector, timeout - LOCATOR_ALL_TIMEOUT, frame_selector)
            return
        raise e


def search(directory: str) -> List[str]:
    results: List[str] = []
    for root, _, files in os.walk(directory):
        for file in files:
            results.append(os.path.join(root, file))
    return results


def parse(html: str):
    return BeautifulSoup(html, "html.parser")


class Handle(TypedDict):
    id: str
    hostname: str
    location: str
    context: BrowserContext
    page: Page


async def init(location: str = "", hostname: str = "") -> Handle:
    global playwright_instance, browser_instance

    id = nanoid.generate(FRIENDLY_CHARS, 6)
    directory = f"user-data-dir/{id}"

    if not playwright_instance:
        playwright_instance = await async_playwright().start()
        browser_instance = await playwright_instance.chromium.launch(headless=False, channel="chromium")

    context = await playwright_instance.chromium.launch_persistent_context(  # type: ignore
        directory, headless=False, viewport={"width": 1920, "height": 1080}
    )

    page = context.pages[0] if context.pages else await context.new_page()
    return {"id": id, "hostname": hostname, "location": location, "context": context, "page": page}


class Pattern(TypedDict):
    name: str
    pattern: BeautifulSoup


def load_patterns() -> List[Pattern]:
    patterns: List[Pattern] = []
    for name in glob("./specs/**/*.html", recursive=True):
        with open(name, "r", encoding="utf-8") as f:
            content = f.read()
        patterns.append(Pattern(name=name, pattern=parse(content)))
    return patterns


class Match(TypedDict):
    name: str
    priority: int
    distilled: str
    matches: List[Locator]


async def distill(hostname: Optional[str], page: Page, patterns: List[Pattern]) -> Optional[Match]:
    result: List[Match] = []

    for item in patterns:
        name = item["name"]
        pattern = item["pattern"]

        root = pattern.find("html")
        gg_priority = root.get("gg-priority", "-1") if isinstance(root, Tag) else "-1"
        try:
            priority = int(str(gg_priority).lstrip("= "))
        except ValueError:
            priority = -1
        domain = root.get("gg-domain") if isinstance(root, Tag) else None

        if domain and hostname:
            if isinstance(domain, str) and domain.lower() not in hostname.lower():
                if MIDDLEMAN_DEBUG:
                    print(f"{GRAY}Skipping {name} due to mismatched domain {domain}{NORMAL}")
                continue

        print(f"Checking {name} with priority {priority}")

        found = True
        matches: List[Locator] = []
        targets = pattern.find_all(attrs={"gg-match": True}) + pattern.find_all(attrs={"gg-match-html": True})

        for target in targets:
            if not isinstance(target, Tag):
                continue

            html = target.get("gg-match-html")
            selector, frame_selector = get_selector(str(html if html else target.get("gg-match")))
            if not selector or not isinstance(selector, str):
                continue

            if frame_selector:
                source = await locate(page.frame_locator(str(frame_selector)).locator(selector))
            else:
                source = await locate(page.locator(selector))

            if source:
                if html:
                    target.clear()
                    fragment = BeautifulSoup("<div>" + await source.inner_html() + "</div>", "html.parser")
                    if fragment.div:
                        for child in list(fragment.div.children):
                            child.extract()
                            target.append(child)
                else:
                    raw_text = await source.text_content()
                    if raw_text:
                        target.string = raw_text.strip()
                matches.append(source)
            else:
                optional = target.get("gg-optional") is not None
                if MIDDLEMAN_DEBUG and optional:
                    print(f"{GRAY}Optional {selector} has no match{NORMAL}")
                if not optional:
                    found = False

        if found and len(matches) > 0:
            distilled = str(pattern)
            result.append({
                "name": name,
                "priority": priority,
                "distilled": distilled,
                "matches": matches,
            })

    result = sorted(result, key=lambda x: x["priority"])

    if len(result) == 0:
        if MIDDLEMAN_DEBUG:
            print("No matches found")
        return None
    else:
        if MIDDLEMAN_DEBUG:
            print(f"Number of matches: {len(result)}")
            for item in result:
                print(f" - {item['name']} with priority {item['priority']}")

        match = result[0]
        print(f"{YELLOW}{CHECK} Best match: {BOLD}{match['name']}{NORMAL}")
        return match


async def autofill(page: Page, distilled: str, fields: List[str]):
    document = parse(distilled)
    root = document.find("html")
    domain = None
    if root:
        domain = cast(Tag, root).get("gg-domain")

    for field in fields:
        element = document.find("input", {"type": field})
        selector = None
        frame_selector = None
        if element:
            selector, frame_selector = get_selector(str(cast(Tag, element).get("gg-match")))

        if element and selector:
            source = f"{domain}_{field}" if domain else field
            key = source.upper()
            value = os.getenv(key)

            if value and len(value) > 0:
                print(f"{CYAN}{ARROW} Using {BOLD}{key}{NORMAL} for {field}{NORMAL}")
                if frame_selector:
                    await page.frame_locator(str(frame_selector)).locator(str(selector)).fill(value)
                else:
                    await page.fill(str(selector), value)
            else:
                placeholder = cast(Tag, element).get("placeholder")
                prompt = str(placeholder) if placeholder else f"Please enter {field}"
                mask = "*" if field == "password" else None
                if frame_selector:
                    await page.frame_locator(str(frame_selector)).locator(str(selector)).fill(await ask(prompt, mask))
                else:
                    await page.fill(str(selector), await ask(prompt, mask))
            await sleep(0.25)


async def autoclick(page: Page, distilled: str):
    document = parse(distilled)
    buttons = document.find_all(attrs={"gg-autoclick": True})

    for button in buttons:
        if isinstance(button, Tag):
            selector, frame_selector = get_selector(str(button.get("gg-match")))
            if selector:
                print(f"{CYAN}{ARROW} Auto-clicking {NORMAL}{selector}")
                if isinstance(frame_selector, list):
                    frame_selector = frame_selector[0] if frame_selector else None
                await click(page, str(selector), frame_selector=frame_selector)


async def terminate(page: Page, distilled: str) -> bool:
    document = parse(distilled)
    stops = document.find_all(attrs={"gg-stop": True})
    if len(stops) > 0:
        print("Found stop elements, terminating session...")
        return True
    return False


def render(content: str, options: Optional[Dict[str, str]] = None) -> str:
    if options is None:
        options = {}

    title = options.get("title", "MIDDLEMAN")
    action = options.get("action", "")

    return f"""<!doctype html>
<html>
  <head>
    <title>{title}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
  </head>
  <body>
    <main class="container">
      <section>
        <h2>{title}</h2>
        <articles>
        <form method="POST" action="{action}">
        {content}
        </form>
        </articles>
      </section>
    </main>
  </body>
</html>"""


browsers: List[Handle] = []
playwright_instance = None
browser_instance = None


app = FastAPI()


@app.get("/health")
async def health() -> dict[str, float | str]:
    return {"status": "OK", "timestamp": asyncio.get_event_loop().time()}


@app.get("/", response_class=HTMLResponse)
async def home():
    examples = [
        {"title": "NYT Best Sellers", "link": "/start?location=www.nytimes.com/books/best-sellers"},
        {"title": "Slashdot: Most Discussed", "link": "/start?location=technology.slashdot.org"},
        {"title": "Goodreads Bookshelf", "link": "/start?location=goodreads.com/signin"},
        {"title": "BBC Saved Articles", "link": "/start?location=bbc.com/saved"},
        {"title": "Amazon Browsing History", "link": "/start?location=amazon.com/gp/history"},
        {"title": "Gofood Order History", "link": "/start?location=gofood.co.id/en/orders"},
        {"title": "eBird Life List", "link": "/start?location=ebird.org/lifelist"},
        {"title": "Agoda Booking History", "link": "/start?location=agoda.com/account/bookings.html"},
    ]

    items = [f'<li><a href="{item["link"]}" target="_blank">{item["title"]}</a></li>' for item in examples]
    content = f"<p>Try the following examples:</p><ul>{''.join(items)}</ul>"

    return HTMLResponse(render(content))


@app.get("/start", response_class=HTMLResponse)
async def start(location: str):
    if not location:
        raise HTTPException(status_code=400, detail="Missing location parameter")

    if not location.startswith("http"):
        location = f"https://{location}"

    hostname = urllib.parse.urlparse(location).hostname or ""

    handle = await init(location, hostname)
    id = handle["id"]
    page = handle["page"]

    print(f"{GREEN}{ARROW} Browser launched with generated id: {BOLD}{id}{NORMAL}")
    browsers.append(handle)

    if MIDDLEMAN_PAUSE:
        await pause()

    print(f"{GREEN}{ARROW} Navigating to {NORMAL}{location}")
    await page.goto(location)

    # Since the browser can't redirect from GET to POST,
    # we'll use an auto-submit form to do that.
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <body>
      <form id="redirect" action="/link/{id}" method="post">
      </form>
      <script>document.getElementById('redirect').submit();</script>
    </body>
    </html>
    """)


@app.post("/link/{id}", response_class=HTMLResponse)
async def link(id: str, request: Request):
    browser = next((b for b in browsers if b["id"] == id), None)
    if not browser:
        raise HTTPException(status_code=404, detail=f"Invalid id: {id}")

    hostname = browser["hostname"]
    context = browser["context"]
    page = browser["page"]

    patterns = load_patterns()

    print(f"{GREEN}{ARROW} Continuing automation for {BOLD}{id}{NORMAL} at {BOLD}{hostname}{NORMAL}")

    form_data = await request.form()
    fields = dict(form_data)

    TICK = 1  # seconds
    TIMEOUT = 15  # seconds
    max = TIMEOUT // TICK

    current = {"name": None, "distilled": None}

    for iteration in range(max):
        print()
        print(f"{MAGENTA}Iteration {iteration + 1}{NORMAL} of {max}")
        await sleep(TICK)

        match = await distill(hostname, page, patterns)
        if not match:
            print(f"{CROSS}{RED} No matched pattern found{NORMAL}")
            continue

        distilled = match["distilled"]
        if distilled == current["distilled"]:
            print(f"{ARROW} Still the same: {match['name']}")
            continue

        current = match
        print()
        print(distilled)

        names: List[str] = []
        document = parse(distilled)
        inputs = document.find_all("input")

        for input in inputs:
            if isinstance(input, Tag):
                selector, frame_selector = get_selector(str(input.get("gg-match")))
                name = input.get("name")

                if selector:
                    if input.get("type") == "checkbox":
                        names.append(str(name) if name else "checkbox")
                        print(f"{CYAN}{ARROW} Handling {NORMAL}{selector} using autoclick")
                    elif name:
                        value = fields.get(str(name))
                        if value and len(str(value)) > 0:
                            print(f"{CYAN}{ARROW} Using form data {BOLD}{name}{NORMAL}")
                            names.append(str(name))
                            input["value"] = str(value)
                            if frame_selector:
                                await page.frame_locator(str(frame_selector)).locator(str(selector)).fill(str(value))
                            else:
                                await page.fill(str(selector), str(value))
                            del fields[str(name)]
                            await sleep(0.25)
                        else:
                            print(f"{CROSS}{RED} No form data found for {BOLD}{name}{NORMAL}")

        title_element = document.find("title")
        title = title_element.get_text() if title_element else "MIDDLEMAN"
        action = f"/link/{id}"

        if len(inputs) == len(names):
            await autoclick(page, distilled)
            if await terminate(page, distilled):
                print(f"{GREEN}{CHECK} Finished!{NORMAL}")
                await context.close()
                browsers[:] = [b for b in browsers if b["id"] != id]
                return HTMLResponse(render(str(document.find("body")), {"title": title, "action": action}))

            print(f"{GREEN}{CHECK} All form fields are filled{NORMAL}")
            continue

        if await terminate(page, distilled):
            print(f"{GREEN}{CHECK} Finished!{NORMAL}")
            await context.close()
            browsers[:] = [b for b in browsers if b["id"] != id]
        else:
            print(f"{CROSS}{RED} Not all form fields are filled{NORMAL}")

        return HTMLResponse(render(str(document.find("body")), {"title": title, "action": action}))

    raise HTTPException(status_code=503, detail="Timeout reached")


async def list_command():
    spec_files = glob("./specs/**/*", recursive=True)
    spec_files = [f for f in spec_files if f.endswith(".html")]

    for name in spec_files:
        print(os.path.basename(name))


async def distill_command(location: str, option: Optional[str] = None):
    patterns = load_patterns()

    print(f"Distilling {location}")

    async with async_playwright() as p:
        if location.startswith("http"):
            hostname = urllib.parse.urlparse(location).hostname
            browser = await p.chromium.launch(headless=False, channel="chromium")
            context = await browser.new_context()
            page = await context.new_page()

            if MIDDLEMAN_PAUSE:
                await pause()

            await page.goto(location)
        else:
            hostname = option or ""
            browser = await p.chromium.launch(headless=False, channel="chromium")
            context = await browser.new_context()
            page = await context.new_page()

            with open(location, "r", encoding="utf-8") as f:
                content = f.read()
            await page.set_content(content)

        match = await distill(hostname, page, patterns)

        if match:
            distilled = match["distilled"]
            print()
            print(distilled)
            print()

        if MIDDLEMAN_PAUSE:
            await pause()

        await browser.close()


async def run_command(location: str):
    if not location.startswith("http"):
        location = f"https://{location}"

    hostname = urllib.parse.urlparse(location).hostname or ""
    patterns = load_patterns()

    browser_data = await init(location, hostname)
    browser_id = browser_data["id"]
    context = browser_data["context"]
    page = browser_data["page"]

    print(f"Starting browser {browser_id}")

    if MIDDLEMAN_PAUSE:
        await pause()

    print(f"{GREEN}{ARROW} Navigating to {NORMAL}{location}")
    await page.goto(location)

    TICK = 1  # seconds
    TIMEOUT = 15  # seconds
    max = TIMEOUT // TICK

    current = {"name": None, "distilled": None}

    try:
        for iteration in range(max):
            print()
            print(f"{MAGENTA}Iteration {iteration + 1}{NORMAL} of {max}")
            await sleep(TICK)

            match = await distill(hostname, page, patterns)
            if match:
                name = match["name"]
                distilled = match["distilled"]

                if distilled == current["distilled"]:
                    print(f"Still the same: {name}")
                else:
                    current = match
                    print()
                    print(distilled)
                    await autofill(page, distilled, ["email", "tel", "text", "password"])
                    await autoclick(page, distilled)

                    if await terminate(page, distilled):
                        break
            else:
                print(f"{CROSS}{RED} No matched pattern found{NORMAL}")

        print()
        print(f"Terminating browser {browser_id}")

        if MIDDLEMAN_PAUSE:
            await pause()

    finally:
        await context.close()
        print("Terminated.")


async def inspect_command(browser_id: str, option: Optional[str] = None):
    directory = f"user-data-dir/{browser_id}"

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(directory, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()

        if option and len(option) > 0:
            url = option if option.startswith("http") else f"https://{option}"
            await page.goto(url)

        await pause()
        await context.close()


async def main():
    if len(sys.argv) == 1:
        return "server"

    parser = argparse.ArgumentParser(description="MIDDLEMAN")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    subparsers.add_parser("list", help="List all patterns")

    distill_parser = subparsers.add_parser("distill", help="Distill a webpage")
    distill_parser.add_argument("parameter", help="URL or file path")
    distill_parser.add_argument("option", nargs="?", help="Hostname for file distillation")

    run_parser = subparsers.add_parser("run", help="Run automation")
    run_parser.add_argument("parameter", help="URL or domain")

    inspect_parser = subparsers.add_parser("inspect", help="Inspect browser session")
    inspect_parser.add_argument("parameter", help="Browser ID")
    inspect_parser.add_argument("option", nargs="?", help="URL to navigate to")

    subparsers.add_parser("server", help="Start web server")

    args = parser.parse_args()

    if args.command == "list":
        await list_command()
    elif args.command == "distill":
        await distill_command(args.parameter, args.option)
    elif args.command == "run":
        await run_command(args.parameter)
    elif args.command == "inspect":
        await inspect_command(args.parameter, args.option)
    elif args.command == "server":
        return "server"
    else:
        parser.print_help()


if __name__ == "__main__":
    result = asyncio.run(main())
    if result == "server":
        port = int(os.getenv("PORT", 3000))
        print(f"Listening on port {port}")
        uvicorn.run(app, host="0.0.0.0", port=port)
