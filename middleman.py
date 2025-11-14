#!/usr/bin/env python3

import argparse
import asyncio
import os
import random
import re
import sys
import urllib.parse
from glob import glob
from dataclasses import dataclass

from bs4 import BeautifulSoup
from bs4.element import Tag
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
import json
import nanoid

import pwinput
import uvicorn

import zendriver as zd
from zendriver.core.connection import ProtocolException

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


async def pause():
    input("Press Enter to continue...")


def get_selector(input_selector: str):
    pattern = r"^(iframe(?:[^\s]*\[[^\]]+\]|[^\s]+))\s+(.+)$"
    match = re.match(pattern, input_selector)
    if not match:
        return input_selector, None
    return match.group(2), match.group(1)


async def ask(message: str, mask: str | None = None) -> str:
    if mask:
        return pwinput.pwinput(f"{message}: ", mask=mask)
    else:
        return input(f"{message}: ")


def search(directory: str) -> list[str]:
    results: list[str] = []
    for root, _, files in os.walk(directory):
        for file in files:
            results.append(os.path.join(root, file))
    return results


def parse(html: str):
    return BeautifulSoup(html, "html.parser")


@dataclass
class Handle:
    id: str
    hostname: str
    location: str
    browser: zd.Browser
    page: zd.Tab


def collect(filename: str) -> list[str]:
    try:
        with open(filename, "r", encoding="utf-8") as f:
            entries = [line.strip() for line in f if line.strip()]
        print(f"{GREEN}{CHECK}{NORMAL} Loaded {MAGENTA}{len(entries)} entries{NORMAL} from {filename}")
        return entries
    except FileNotFoundError:
        print(f"{YELLOW}Warning: {filename} not found, using empty list{NORMAL}")
        return []
    except Exception as e:
        print(f"{RED}Error loading {filename}: {e}{NORMAL}")
        return []


async def setup_proxy(username: str, password: str, page: zd.Tab):
    async def auth_challenge_handler(event: zd.cdp.fetch.AuthRequired):
        print(f"{CHECK} Supplying proxy authentication...")
        await page.send(
            zd.cdp.fetch.continue_with_auth(
                request_id=event.request_id,
                auth_challenge_response=zd.cdp.fetch.AuthChallengeResponse(
                    response="ProvideCredentials",
                    username=username,
                    password=password,
                ),
            )
        )

    async def req_paused(event: zd.cdp.fetch.RequestPaused) -> None:
        await page.send(zd.cdp.fetch.continue_request(request_id=event.request_id))

    page.add_handler(zd.cdp.fetch.RequestPaused, req_paused)  # type: ignore[arg-type]
    page.add_handler(zd.cdp.fetch.AuthRequired, auth_challenge_handler)  # type: ignore[arg-type]
    await page.send(zd.cdp.fetch.enable(handle_auth_requests=True))


async def init(location: str = "", hostname: str = "") -> Handle:
    id = nanoid.generate(FRIENDLY_CHARS, 6)
    directory = f"user-data-dir/{id}"

    http_proxy = os.getenv("BROWSER_HTTP_PROXY", "")
    proxy_username = None
    proxy_password = None
    proxy_server = None

    if http_proxy:
        parsed = urllib.parse.urlparse(http_proxy)
        proxy_username = parsed.username
        proxy_password = parsed.password
        proxy_server = f"{parsed.scheme}://{parsed.hostname}"  # sans credentials
        if parsed.port:
            proxy_server += f":{parsed.port}"

    browser = None
    try:
        print(f"{CYAN}{ARROW} Starting the browser...{NORMAL}")
        browser_args: list[str] = []
        if proxy_server:
            print(f"Configuring proxy at {MAGENTA}{proxy_server}{NORMAL}...")
            browser_args.append(f"--proxy-server={proxy_server}")
        browser = await zd.start(user_data_dir=directory, browser_args=browser_args)

        page = await browser.get("about:blank")
        if proxy_username or proxy_password:
            print("Setting up proxy authentication...")
            await setup_proxy(proxy_username or "", proxy_password or "", page)

        denylist = collect("denylist.txt")

        async def handle_request(event):
            resource_type = event.resource_type
            request_url = event.request.url

            deny_type = resource_type in [
                zd.cdp.network.ResourceType.MEDIA,
                zd.cdp.network.ResourceType.FONT,
            ]
            deny_url = any(domain in request_url for domain in denylist)
            should_deny = deny_type or deny_url

            if not should_deny:
                try:
                    await page.send(zd.cdp.fetch.continue_request(request_id=event.request_id))
                except ProtocolException as e:
                    if "Invalid state for continueInterceptedRequest" in str(e):
                        pass  # Request already processed, ignore
                    else:
                        raise
                return

            if MIDDLEMAN_DEBUG:
                print(f"{CROSS}{RED} DENY{NORMAL} {request_url}")

            try:
                await page.send(
                    zd.cdp.fetch.fail_request(
                        request_id=event.request_id, error_reason=zd.cdp.network.ErrorReason.BLOCKED_BY_CLIENT
                    )
                )
            except ProtocolException as e:
                if "Invalid state for continueInterceptedRequest" in str(e):
                    pass  # Request already processed, ignore
                else:
                    raise

        page.add_handler(zd.cdp.fetch.RequestPaused, handle_request)

        return Handle(id=id, hostname=hostname, location=location, browser=browser, page=page)
    except Exception as e:
        print(f"{CROSS}{RED} Fatal ERROR: {e}{NORMAL}")
        if browser:
            await browser.stop()
        raise


@dataclass
class Pattern:
    name: str
    pattern: BeautifulSoup


def load_patterns() -> list[Pattern]:
    patterns: list[Pattern] = []
    for name in glob("./patterns/*.html"):
        with open(name, "r", encoding="utf-8") as f:
            content = f.read()
        patterns.append(Pattern(name=name, pattern=parse(content)))
    return patterns


@dataclass
class Match:
    name: str
    priority: int
    distilled: str


async def distill(hostname: str | None, page, patterns: list[Pattern]) -> Match | None:
    result: list[Match] = []

    for item in patterns:
        name = item.name
        pattern = item.pattern

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
        match_count = 0
        targets = pattern.find_all(attrs={"gg-match": True}) + pattern.find_all(attrs={"gg-match-html": True})

        for target in targets:
            if not isinstance(target, Tag):
                continue

            if MIDDLEMAN_DEBUG:
                print(f"Checking target = {target}")
            html = target.get("gg-match-html")
            selector, _ = get_selector(str(html if html else target.get("gg-match")))
            if not selector or not isinstance(selector, str):
                continue

            print(f"Find selector {selector}")
            source = await page_query_selector(page, selector)
            if source:
                print(f"{GREEN}Selector {selector} is source {source}{NORMAL}")
                if html:
                    target.clear()
                    fragment = BeautifulSoup("<div>" + await source.inner_html() + "</div>", "html.parser")
                    if fragment.div:
                        for child in list(fragment.div.children):
                            child.extract()
                            target.append(child)
                else:
                    raw_text = await source.inner_text()
                    if raw_text:
                        target.string = raw_text.strip()
                    if source.tag in ["input", "textarea", "select"]:
                        target["value"] = source.element.value or ""
                match_count += 1
            else:
                print(f"{RED}Selector {selector} has no match{NORMAL}")
                optional = target.get("gg-optional") is not None
                if MIDDLEMAN_DEBUG and optional:
                    print(f"{GRAY}Optional {selector} has no match{NORMAL}")
                if not optional:
                    found = False

        if found and match_count > 0:
            distilled = str(pattern)
            result.append(
                Match(
                    name=name,
                    priority=priority,
                    distilled=distilled,
                )
            )

    result = sorted(result, key=lambda x: x.priority)

    if len(result) == 0:
        if MIDDLEMAN_DEBUG:
            print("No matches found")
        return None
    else:
        if MIDDLEMAN_DEBUG:
            print(f"Number of matches: {len(result)}")
            for item in result:
                print(f" - {item.name} with priority {item.priority}")

        match = result[0]
        print(f"{YELLOW}{CHECK} Best match: {BOLD}{match.name}{NORMAL}")
        return match


async def autofill(page: zd.Tab, distilled: str):
    document = parse(distilled)
    root = document.find("html")
    domain = None
    if root and isinstance(root, Tag):
        domain = root.get("gg-domain")

    processed = []

    for element in document.find_all("input", {"type": True}):
        if not isinstance(element, Tag):
            continue

        input_type = element.get("type")
        name = element.get("name")

        if not name or (isinstance(name, str) and len(name) == 0):
            print(f"{CROSS}{RED} There is an input (of type {input_type}) without a name!{NORMAL}")

        selector, _ = get_selector(str(element.get("gg-match", "")))
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
                input_element = await page_query_selector(page, str(selector))
                if input_element:
                    await input_element.type_text(value)
                element["value"] = value
            else:
                placeholder = element.get("placeholder")
                prompt = str(placeholder) if placeholder else f"Please enter {field}"
                mask = "*" if input_type == "password" else None
                user_input = await ask(prompt, mask)
                input_element = await page_query_selector(page, str(selector))
                if input_element:
                    await input_element.type_text(user_input)
                element["value"] = user_input
            await asyncio.sleep(0.25)
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
                selector, _ = get_selector(str(radio.get("gg-match")))
                radio_element = await page_query_selector(page, str(selector))
                if radio_element:
                    await radio_element.click()
        elif input_type == "checkbox":
            checked = element.get("checked")
            if checked is not None:
                print(f"{CYAN}{ARROW} Checking {BOLD}{name}{NORMAL}")
                checkbox_element = await page_query_selector(page, str(selector))
                if checkbox_element:
                    await checkbox_element.click()

    return str(document)


async def autoclick(page: zd.Tab, distilled: str, expr: str):
    document = parse(distilled)
    elements = document.select(expr)
    for el in elements:
        if isinstance(el, Tag):
            selector, _ = get_selector(str(el.get("gg-match")))
            if selector:
                target = await page_query_selector(page, selector)
                if target:
                    print(f"{CYAN}{ARROW} Clicking {NORMAL}{selector}")
                    await target.click()
                else:
                    print(f"{YELLOW}Warning: {selector} not found, can't click on it")


async def terminate(distilled: str) -> bool:
    document = parse(distilled)
    stops = document.find_all(attrs={"gg-stop": True})
    if len(stops) > 0:
        print("Found stop elements, terminating session...")
        return True
    return False


class Element:
    """Wrapper to handle both CSS and XPath selector differences for browser elements."""

    def __init__(self, element: zd.Element, css_selector: str | None = None, xpath_selector: str | None = None):
        self.element = element
        self.tag = element.tag
        self.page = element.tab
        self.css_selector = css_selector
        self.xpath_selector = xpath_selector

    async def inner_html(self) -> str:
        return await self.element.get_html()

    async def inner_text(self) -> str:
        return self.element.text

    async def click(self) -> None:
        if self.css_selector:
            await self.css_click()
        else:
            await self.xpath_click()
        await asyncio.sleep(0.25)

    async def type_text(self, text: str) -> None:
        await self.element.clear_input()
        await asyncio.sleep(0.1)
        for char in text:
            await self.element.send_keys(char)
            await asyncio.sleep(random.uniform(0.01, 0.05))

    async def css_click(self) -> None:
        if not self.css_selector:
            print(f"{RED}Cannot perform CSS click: no css_selector available{NORMAL}")
            return
        print(f"Attempting JavaScript CSS click for {self.css_selector}")
        try:
            escaped_selector = self.css_selector.replace("\\", "\\\\").replace('"', '\\"')
            js_code = f"""
            (() => {{
                let element = document.querySelector("{escaped_selector}");
                if (element) {{ element.click(); return true; }}
                return false;
            }})()
            """
            result = await self.page.evaluate(js_code)
            if result:
                print(f"{GREEN}JavaScript CSS click succeeded for {self.css_selector}{NORMAL}")
                return
            else:
                print(f"{RED}JavaScript CSS click could not find element {self.css_selector}{NORMAL}")
        except Exception as js_error:
            print(f"{RED}JavaScript CSS click failed: {js_error}{NORMAL}")

    async def xpath_click(self) -> None:
        if not self.xpath_selector:
            print(f"{RED}Cannot perform XPath click: no xpath_selector available{NORMAL}")
            return
        print(f"Attempting JavaScript XPath click for {self.xpath_selector}")
        try:
            escaped_selector = self.xpath_selector.replace("\\", "\\\\").replace('"', '\\"')
            js_code = f"""
            (() => {{
                let element = document.evaluate("{escaped_selector}", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                if (element) {{ element.click(); return true; }}
                return false;
            }})()
            """
            result = await self.page.evaluate(js_code)
            if result:
                print(f"{GREEN}JavaScript XPath click succeeded for {self.xpath_selector}{NORMAL}")
                return
            else:
                print(f"{RED}JavaScript XPath click could not find element {self.xpath_selector}{NORMAL}")
        except Exception as js_error:
            print(f"{RED}JavaScript XPath click failed: {js_error}{NORMAL}")


async def page_query_selector(page: zd.Tab, selector: str, timeout: float = 0) -> Element | None:
    try:
        if selector.startswith("//"):
            elements = await page.xpath(selector, timeout)
            if elements and len(elements) > 0:
                return Element(elements[0], xpath_selector=selector)
            return None

        element = await page.select(selector, timeout=timeout)
        if element:
            return Element(element, css_selector=selector)
        return None
    except (asyncio.TimeoutError, Exception):
        return None


def extract_value(item: Tag, attribute: str | None = None) -> str:
    if attribute:
        value = item.get(attribute)
        if isinstance(value, list):
            value = value[0] if value else ""
        return value.strip() if isinstance(value, str) else ""
    return item.get_text(strip=True)


async def convert(distilled: str):
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
                kv: dict[str, str | list[str]] = {}
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


def render(content: str, options: dict[str, str] | None = None) -> str:
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


browsers: list[Handle] = []


app = FastAPI()


@app.get("/health")
async def health() -> dict[str, float | str]:
    return {"status": "OK", "timestamp": asyncio.get_event_loop().time()}


@app.get("/", response_class=HTMLResponse)
async def home():
    def build_example_items(examples: list[dict[str, str]]) -> list[str]:
        return [f'<li><a href="{item["link"]}" target="_blank">{item["title"]}</a></li>' for item in examples]

    extraction_examples = [
        {"title": "NPR Headlines", "link": "/start?location=text.npr.org"},
        {"title": "Slashdot: Most Discussed", "link": "/start?location=technology.slashdot.org"},
        {"title": "ESPN College Football Schedule", "link": "/start?location=espn.com/college-football/schedule"},
        {"title": "NBA Key Dates", "link": "/start?location=nba.com/news/key-dates"},
        {"title": "NYT Best Sellers", "link": "/start?location=www.nytimes.com/books/best-sellers"},
    ]

    signin_examples = [
        {"title": "BBC Saved Articles", "link": "/start?location=bbc.com/saved"},
        {"title": "Goodreads Bookshelf", "link": "/start?location=goodreads.com/signin"},
        {"title": "Amazon Browsing History", "link": "/start?location=amazon.com/gp/history"},
        {"title": "Gofood Order History", "link": "/start?location=gofood.co.id/en/orders"},
        {"title": "eBird Life List", "link": "/start?location=ebird.org/lifelist"},
        {"title": "Agoda Booking History", "link": "/start?location=agoda.com/account/bookings.html"},
        {
            "title": "Wayfair Order History",
            "link": "/start?location=www.wayfair.com/session/secure/account/order_search.php",
        },
    ]

    extraction_items = build_example_items(extraction_examples)
    signin_items = build_example_items(signin_examples)

    content = f"""
    <p>Try these extraction examples:</p>
    <ul>{"".join(extraction_items)}</ul>
    <p>or explore these examples that require sign-in:</p>
    <ul>{"".join(signin_items)}</ul>
    """

    return HTMLResponse(render(content))


@app.get("/start", response_class=HTMLResponse)
async def start(location: str):
    if not location:
        raise HTTPException(status_code=400, detail="Missing location parameter")

    if not location.startswith("http"):
        location = f"https://{location}"

    hostname = urllib.parse.urlparse(location).hostname or ""

    handle = await init(location, hostname)
    id = handle.id
    page = handle.page

    print(f"{GREEN}{ARROW} Browser launched with generated id: {BOLD}{id}{NORMAL}")
    browsers.append(handle)

    if MIDDLEMAN_PAUSE:
        await pause()

    try:
        print(f"{GREEN}{ARROW} Navigating to {NORMAL}{location}")
        await page.get(location)
    except Exception as e:
        print(f"{RED}Error navigating to {location}: {e}{NORMAL}")
        raise HTTPException(status_code=500, detail=f"Navigation failed: {str(e)}")

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
    handle = next((b for b in browsers if b.id == id), None)
    if not handle:
        raise HTTPException(status_code=404, detail=f"Invalid id: {id}")

    hostname = handle.hostname
    browser = handle.browser
    page = handle.page

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
        await asyncio.sleep(TICK)

        match = await distill(hostname, page, patterns)
        if not match:
            print(f"{CROSS}{RED} No matched pattern found{NORMAL}")
            continue

        distilled = match.distilled
        if distilled == current["distilled"]:
            print(f"{ARROW} Still the same: {match.name}")
            continue

        current["name"] = match.name
        current["distilled"] = match.distilled
        print()
        print(distilled)

        document = parse(distilled)
        title_element = document.find("title")
        title = title_element.get_text() if title_element else "MIDDLEMAN"
        action = f"/link/{id}"

        if await terminate(distilled):
            print(f"{GREEN}{CHECK} Finished!{NORMAL}")
            converted = await convert(distilled)
            await browser.stop()
            browsers[:] = [b for b in browsers if b.id != id]
            if converted:
                return JSONResponse(converted)
            return HTMLResponse(render(str(document.find("body")), {"title": title, "action": action}))

        if fields.get("button"):
            button = document.find("button", value=str(fields.get("button")))
            if button and isinstance(button, Tag):
                button_selector, _ = get_selector(str(button.get("gg-match")))
                button_element = await page_query_selector(page, str(button_selector))
                if button_element:
                    print(f"{CYAN}{ARROW} Clicking button {BOLD}{button_selector}{NORMAL}")
                    await button_element.click()
                continue

        names: list[str] = []
        inputs = document.find_all("input")

        for input in inputs:
            if isinstance(input, Tag):
                selector, _ = get_selector(str(input.get("gg-match")))
                element = await page_query_selector(page, selector)
                name = input.get("name")

                if selector and element:
                    if input.get("type") == "checkbox":
                        if not name:
                            print(f"{CROSS}{RED} No name for the checkbox {NORMAL}{selector}")
                            continue
                        value = fields.get(str(name))
                        checked = value and len(str(value)) > 0
                        names.append(str(name))
                        print(f"{CYAN}{ARROW} Status of checkbox {BOLD}{name}={checked}{NORMAL}")
                        if checked:
                            await element.click()
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
                        radio_selector, _ = get_selector(str(radio.get("gg-match")))
                        radio_element = await page_query_selector(page, str(radio_selector))
                        if radio_element:
                            await radio_element.click()
                        radio["checked"] = "checked"
                        current["distilled"] = str(document)
                        names.append(str(input.get("id")) if input.get("id") else "radio")
                        await asyncio.sleep(0.25)
                    elif name:
                        value = fields.get(str(name))
                        if value and len(str(value)) > 0:
                            print(f"{CYAN}{ARROW} Using form data {BOLD}{name}{NORMAL}")
                            names.append(str(name))
                            input["value"] = str(value)
                            current["distilled"] = str(document)
                            await element.type_text(str(value))
                            del fields[str(name)]
                            await asyncio.sleep(0.25)
                        else:
                            print(f"{CROSS}{RED} No form data found for {BOLD}{name}{NORMAL}")

        await autoclick(page, distilled, "[gg-autoclick]:not(button)")

        SUBMIT_BUTTON = "button[gg-autoclick], button[type=submit]"
        if document.select(SUBMIT_BUTTON):
            if len(names) > 0 and len(inputs) == len(names):
                print(f"{GREEN}{CHECK} Submitting form{NORMAL}, all fields are filled...")
                await autoclick(page, distilled, SUBMIT_BUTTON)
                continue

            print(f"{CROSS}{RED} Not all form fields are filled{NORMAL}")
            return HTMLResponse(render(str(document.find("body")), {"title": title, "action": action}))

    raise HTTPException(status_code=503, detail="Timeout reached")


async def list_command():
    for name in glob("./patterns/*.html"):
        print(os.path.basename(name))


async def distill_command(location: str, option: str | None = None):
    patterns = load_patterns()

    print(f"Distilling {location}")

    browser = await zd.start()
    if location.startswith("http"):
        hostname = urllib.parse.urlparse(location).hostname
        if MIDDLEMAN_PAUSE:
            await pause()
        page: zd.Tab = await browser.get(location)
        await page
    else:
        hostname = option or ""
        page: zd.Tab = await browser.get("about:blank")
        # TODO: zendriver Tab doesn't have set_content method to load local HTML
        # with open(location, "r", encoding="utf-8") as f:
        #     content = f.read()
        #     await page.set_content(content)
        await page

    match = await distill(hostname, page, patterns)

    if match:
        distilled = match.distilled
        print()
        print(distilled)
        print()
        if await terminate(distilled):
            print(f"{GREEN}{CHECK} Finished!{NORMAL}")
            converted = await convert(distilled)
            if converted:
                print()
                print(converted)
                print()

    if MIDDLEMAN_PAUSE:
        await pause()
    await browser.stop()


async def run_command(location: str):
    if not location.startswith("http"):
        location = f"https://{location}"

    hostname = urllib.parse.urlparse(location).hostname or ""
    patterns = load_patterns()

    browser_data = await init(location, hostname)
    browser_id = browser_data.id
    browser = browser_data.browser

    print(f"Starting browser {browser_id}")

    print(f"{GREEN}{ARROW} Navigating to {NORMAL}{location}")
    page = await browser.get(location)
    await page

    TICK = 1  # seconds
    TIMEOUT = 15  # seconds
    max = TIMEOUT // TICK

    current: dict[str, str] = {"name": "", "distilled": ""}

    try:
        for iteration in range(max):
            print()
            print(f"{MAGENTA}Iteration {iteration + 1}{NORMAL} of {max}")
            await asyncio.sleep(TICK)

            match = await distill(hostname, page, patterns)

            # if MIDDLEMAN_PAUSE:
            #     await pause()

            if match:
                if match.distilled == current["distilled"]:
                    print(f"Still the same: {match.name}")
                else:
                    distilled = match.distilled
                    current["name"] = match.name
                    current["distilled"] = distilled
                    print()
                    print(distilled)

                    if await terminate(distilled):
                        converted = await convert(distilled)
                        if converted:
                            print()
                            print(converted)
                        break

                    distilled = await autofill(page, match.distilled)
                    await autoclick(page, distilled, "[gg-autoclick]:not(button)")
                    await autoclick(page, distilled, "button[gg-autoclick], button[type=submit]")
            else:
                print(f"{CROSS}{RED} No matched pattern found{NORMAL}")

        print()
        print(f"Terminating browser {browser_id}")

        if MIDDLEMAN_PAUSE:
            await pause()

    finally:
        await browser.stop()
        print("Terminated.")


async def inspect_command(browser_id: str, option: str | None = None):
    directory = f"user-data-dir/{browser_id}"
    browser = await zd.start(user_data_dir=directory)
    page = await browser.get()
    if option and len(option) > 0:
        url = option if option.startswith("http") else f"https://{option}"
        await page.get(url)
    await pause()


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
