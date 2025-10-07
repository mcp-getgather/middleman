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
from fastapi.responses import HTMLResponse, JSONResponse
import json
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


BLOCKED_DOMAINS = [
    "3lift.com",
    "adnxs.com",
    "adobedtm.com",
    "adsrvr.org",
    "amazon-adsystem.com",
    "amplitude.com",
    "appboy.com",
    "bamgrid.com",
    "bluekai.com",
    "bounceexchange.com",
    "brandmetrics.com",
    "casalamedia.com",
    "consentmanager.net",
    "cookielaw.org",
    "covatic.io",
    "criteo.com",
    "cxense.com",
    "datadoghq-browser-agent.com",
    "dotmetrics.net",
    "doubleclick.net",
    "doubleverify.com",
    "edigitalsurvey.com",
    "engsvc.go.com",
    "fls-na.amazon.com",
    "go-mpulse.net",
    "googlesyndication.com",
    "googletagmanager.com",
    "imrworldwide.com",
    "ipredictive.com",
    "kochava.com",
    "media.net",
    "mgid.com",
    "nr-data.net",
    "omtrdc.net",
    "openx.net",
    "opin.media",
    "optimizationguide-pa.googleapis",
    "optimizely.com",
    "permutive.com",
    "piano.io",
    "privacymanager.io",
    "privacy-mgmt.com",
    "pubmatic.com",
    "qualtrics.com",
    "quantummetric.com",
    "registerdisney.go.com",
    "rubiconproject.com",
    "scorecardresearch.com",
    "serving-sys.com",
    "sovrn.com",
    "taboola.com",
    "tealiumiq.com",
    "the-ozone-project.com",
    "thetradedesk.com",
    "tinypass.com",
    "tiqcdn.com",
    "tremorhub.com",
    "zemanta.com",
]


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
    await page.route(
        "**/*",
        lambda route: asyncio.create_task(
            route.abort()
            if route.request.resource_type in ["media", "font"]
            or any(domain in route.request.url for domain in BLOCKED_DOMAINS)
            else route.continue_()
        ),
    )

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
            local = "localhost" in hostname or "127.0.0.1" in hostname
            if isinstance(domain, str) and not local and domain.lower() not in hostname.lower():
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


async def autofill(page: Page, distilled: str):
    document = parse(distilled)
    root = document.find("html")
    domain = None
    if root:
        domain = cast(Tag, root).get("gg-domain")

    processed = []

    for element in document.find_all("input", {"type": True}):
        if not isinstance(element, Tag):
            continue

        input_type = element.get("type")
        name = element.get("name")

        if not name or (isinstance(name, str) and len(name) == 0):
            print(f"{CROSS}{RED} There is an input (of type {input_type}) without a name!{NORMAL}")

        selector, frame_selector = get_selector(str(element.get("gg-match", "")))
        if not selector:
            print(f"{CROSS}{RED} There is an input (of type {input_type}) without a selector!{NORMAL}")
            continue

        if input_type in ["email", "tel", "text", "password"]:
            field = name or input_type
            if MIDDLEMAN_DEBUG:
                print(f"{ARROW} Autofilling type={input_type} name={name}...")

            source = f"{domain}_{field}" if domain else field
            key = str(source).upper()
            value = os.getenv(key)

            if value and isinstance(value, str) and len(value) > 0:
                print(f"{CYAN}{ARROW} Using {BOLD}{key}{NORMAL} for {field}{NORMAL}")
                if frame_selector:
                    await page.frame_locator(str(frame_selector)).locator(str(selector)).fill(value)
                else:
                    await page.fill(str(selector), value)
                element["value"] = value
            else:
                placeholder = element.get("placeholder")
                prompt = str(placeholder) if placeholder else f"Please enter {field}"
                mask = "*" if input_type == "password" else None
                user_input = await ask(prompt, mask)
                if frame_selector:
                    await page.frame_locator(str(frame_selector)).locator(str(selector)).fill(user_input)
                else:
                    await page.fill(str(selector), user_input)
                element["value"] = user_input
            await sleep(0.25)
        elif input_type == "radio":
            if not name:
                print(f"{CROSS}{RED} There is no name for radio button with id {element.get('id')}!{NORMAL}")
                continue
            if name in processed:
                continue
            processed.append(name)

            choices = []
            print()
            radio_buttons = document.find_all("input", {"type": "radio"})
            for button in radio_buttons:
                if not isinstance(button, Tag):
                    continue
                if button.get("name") != name:
                    continue
                button_id = button.get("id")
                label_element = document.find("label", {"for": str(button_id)}) if button_id else None
                label = label_element.get_text() if label_element else None
                choices.append({"id": button_id, "label": label})
                print(f" {len(choices)}. {label or button_id}")

            choice = 0
            while choice < 1 or choice > len(choices):
                answer = await ask(f"Your choice (1-{len(choices)})")
                try:
                    choice = int(answer)
                except ValueError:
                    choice = 0

            print(f"{CYAN}{ARROW} Choosing {YELLOW}{choices[choice - 1]['label']}{NORMAL}")
            print()

            radio = document.find("input", {"type": "radio", "id": choices[choice - 1]["id"]})
            if radio and isinstance(radio, Tag):
                selector, frame_selector = get_selector(str(radio.get("gg-match")))
                if frame_selector:
                    await page.frame_locator(str(frame_selector)).locator(str(selector)).check()
                else:
                    await page.check(str(selector))
        elif input_type == "checkbox":
            checked = element.get("checked")
            if checked is not None:
                print(f"{CYAN}{ARROW} Checking {BOLD}{name}{NORMAL}")
                if frame_selector:
                    await page.frame_locator(str(frame_selector)).locator(str(selector)).check()
                else:
                    await page.check(str(selector))

    return str(document)


async def autoclick(page: Page, distilled: str):
    document = parse(distilled)
    buttons = document.select('[gg-autoclick]:not(button), button[gg-autoclick], button[type="submit"]')
    for button in buttons:
        if isinstance(button, Tag):
            selector, frame_selector = get_selector(str(button.get("gg-match")))
            if selector:
                print(f"{CYAN}{ARROW} Clicking {NORMAL}{selector}")
                await click(page, str(selector), frame_selector=frame_selector)


async def terminate(page: Page, distilled: str) -> bool:
    document = parse(distilled)
    stops = document.find_all(attrs={"gg-stop": True})
    if len(stops) > 0:
        print("Found stop elements, terminating session...")
        return True
    return False


def extract_value(item: Tag, attribute: str | None = None) -> str:
    if attribute:
        value = item.get(attribute)
        if isinstance(value, list):
            value = value[0] if value else ""
        return value.strip() if isinstance(value, str) else ""
    return item.get_text(strip=True)


async def convert(page: Page, distilled: str):
    document = parse(distilled)
    snippet = document.find("script", {"type": "application/json"})
    if snippet:
        print(f"{GREEN}{ARROW} Found a data converter.{NORMAL}")
        if MIDDLEMAN_DEBUG:
            print(snippet.get_text())
        try:
            converter = json.loads(snippet.get_text())
            if MIDDLEMAN_DEBUG:
                print("Start converting using", converter)

            rows = document.select(str(converter.get("rows", "")))
            print(f"  Finding rows using {CYAN}{converter.get('rows')}{NORMAL}: found {GREEN}{len(rows)}{NORMAL}.")
            converted = []
            for i, el in enumerate(rows):
                if MIDDLEMAN_DEBUG:
                    print(f" Converting row {GREEN}{i + 1}{NORMAL} of {len(rows)}")
                kv: Dict[str, str | list[str]] = {}
                for col in converter.get("columns", []):
                    name = col.get("name")
                    selector = col.get("selector")
                    attribute = col.get("attribute")
                    kind = col.get("kind")
                    if not name or not selector:
                        continue

                    if kind == "list":
                        items = el.select(str(selector))
                        kv[name] = [extract_value(item, attribute) for item in items]
                        continue

                    item = el.select_one(str(selector))
                    if item:
                        kv[name] = extract_value(item, attribute)
                if len(kv.keys()) > 0:
                    converted.append(kv)
            print(f"{GREEN}{CHECK} Conversion done for {GREEN}{len(converted)}{NORMAL} entries.")
            return converted
        except Exception as error:
            print(f"{RED}Conversion error:{NORMAL}", str(error))


def render(content: str, options: Optional[Dict[str, str]] = None) -> str:
    if options is None:
        options = {}

    title = options.get("title", "MIDDLEMAN")
    action = options.get("action", "")

    return f"""<!doctype html>
<html data-theme=light>
  <head>
    <title>{title}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
    <style>
      .vertical-radios {{
        display: flex;
        flex-direction: column;
        gap: 1rem;
        margin-bottom: 1.5rem;
      }}

      .radio-wrapper {{
        display: flex;
        align-items: center;
        gap: 0.5rem;
      }}

      .radio-wrapper input[type='radio'] {{
        margin: 0;
        flex-shrink: 0;
      }}

      .radio-wrapper label {{
        margin: 0;
        cursor: pointer;
        line-height: 1.5;
      }}

      .radio-wrapper:hover label {{
        color: var(--pico-primary);
      }}
    </style>
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
        {"title": "ESPN College Football Schedule", "link": "/start?location=espn.com/college-football/schedule"},
        {"title": "NBA Key Dates", "link": "/start?location=nba.com/news/key-dates"},
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

    current: dict[str, str] = {"name": "", "distilled": ""}

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

        current["name"] = match["name"]
        current["distilled"] = match["distilled"]
        print()
        print(distilled)

        document = parse(distilled)
        title_element = document.find("title")
        title = title_element.get_text() if title_element else "MIDDLEMAN"
        action = f"/link/{id}"

        if await terminate(page, distilled):
            print(f"{GREEN}{CHECK} Finished!{NORMAL}")
            converted = await convert(page, distilled)
            await context.close()
            browsers[:] = [b for b in browsers if b["id"] != id]
            if converted:
                return JSONResponse(converted)
            return HTMLResponse(render(str(document.find("body")), {"title": title, "action": action}))

        names: List[str] = []
        inputs = document.find_all("input")

        for input in inputs:
            if isinstance(input, Tag):
                selector, frame_selector = get_selector(str(input.get("gg-match")))
                name = input.get("name")

                if selector:
                    if input.get("type") == "checkbox":
                        if not name:
                            print(f"{CROSS}{RED} No name for the checkbox {NORMAL}{selector}")
                            continue
                        value = fields.get(str(name))
                        checked = value and len(str(value)) > 0
                        names.append(str(name))
                        print(f"{CYAN}{ARROW} Status of checkbox {BOLD}{name}={checked}{NORMAL}")
                        if checked:
                            if frame_selector:
                                await page.frame_locator(str(frame_selector)).locator(str(selector)).check()
                            else:
                                await page.check(str(selector))
                    elif input.get("type") == "radio":
                        value = fields.get(str(name)) if name else None
                        if not value or len(str(value)) == 0:
                            print(f"{CROSS}{RED} No form data found for radio button group {BOLD}{name}{NORMAL}")
                            continue
                        radio = document.find("input", {"type": "radio", "id": str(value)})
                        if not radio or not isinstance(radio, Tag):
                            print(f"{CROSS}{RED} No radio button found with id {BOLD}{value}{NORMAL}")
                            continue
                        print(f"{CYAN}{ARROW} Handling radio button group {BOLD}{name}{NORMAL}")
                        print(f"{CYAN}{ARROW} Using form data {BOLD}{name}={value}{NORMAL}")
                        radio_selector, radio_frame_selector = get_selector(str(radio.get("gg-match")))
                        if radio_frame_selector:
                            await page.frame_locator(str(radio_frame_selector)).locator(str(radio_selector)).check()
                        else:
                            await page.check(str(radio_selector))
                        radio["checked"] = "checked"
                        current["distilled"] = str(document)
                        names.append(str(input.get("id")) if input.get("id") else "radio")
                        await sleep(0.25)
                    elif name:
                        value = fields.get(str(name))
                        if value and len(str(value)) > 0:
                            print(f"{CYAN}{ARROW} Using form data {BOLD}{name}{NORMAL}")
                            names.append(str(name))
                            input["value"] = str(value)
                            current["distilled"] = str(document)
                            if frame_selector:
                                await page.frame_locator(str(frame_selector)).locator(str(selector)).fill(str(value))
                            else:
                                await page.fill(str(selector), str(value))
                            del fields[str(name)]
                            await sleep(0.25)
                        else:
                            print(f"{CROSS}{RED} No form data found for {BOLD}{name}{NORMAL}")

        is_form_filled = len(names) > 0 and len(inputs) == len(names)
        has_no_form_fields = len(inputs) == 0
        has_click_buttons = len(document.find_all(attrs={"gg-autoclick": True})) > 0

        if is_form_filled or (has_click_buttons and has_no_form_fields):
            await autoclick(page, distilled)
            print(f"{GREEN}{CHECK} Clicked on buttons{NORMAL}")
            continue

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
            if await terminate(page, distilled):
                print(f"{GREEN}{CHECK} Finished!{NORMAL}")
                converted = await convert(page, distilled)
                if converted:
                    print()
                    print(converted)
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

    current: dict[str, str] = {"name": "", "distilled": ""}

    try:
        for iteration in range(max):
            print()
            print(f"{MAGENTA}Iteration {iteration + 1}{NORMAL} of {max}")
            await sleep(TICK)

            match = await distill(hostname, page, patterns)
            if match:
                if match["distilled"] == current["distilled"]:
                    print(f"Still the same: {match['name']}")
                else:
                    distilled = match["distilled"]
                    current["name"] = match["name"]
                    current["distilled"] = distilled
                    print()
                    print(distilled)

                    if await terminate(page, distilled):
                        converted = await convert(page, distilled)
                        if converted:
                            print()
                            print(converted)
                        break

                    distilled = await autofill(page, match["distilled"])
                    await autoclick(page, distilled)
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
