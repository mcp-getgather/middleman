#!/usr/bin/env node

const fs = require('fs');
const nanoid = require('nanoid');
const path = require('path');
const prettier = require('prettier');

const { Hono } = require('hono');
const { serve } = require('@hono/node-server');
const { serveStatic } = require('@hono/node-server/serve-static');

const { chromium } = require('patchright');
const { JSDOM, VirtualConsole } = require('jsdom');

const MIDDLEMAN_DEBUG = process.env.MIDDLEMAN_DEBUG;
const MIDDLEMAN_PAUSE = process.env.MIDDLEMAN_PAUSE;

const NORMAL = '\x1b[0m';
const BOLD = '\x1b[1m';
const YELLOW = '\x1b[93m';
const MAGENTA = '\x1b[35m';
const RED = '\x1b[91m';
const GREEN = '\x1b[92m';
const CYAN = '\x1b[36m';
const GRAY = '\x1b[90m';

const ARROW = '⇢';
const CHECK = '✓';
const CROSS = '✘';

const sleep = async (seconds) => await new Promise((resolve) => setTimeout(resolve, seconds * 1000.0));

const pause = async () => {
  const readline = require('readline').createInterface({
    input: process.stdin,
    output: process.stdout
  });
  await new Promise((resolve) => {
    readline.question('Press Enter to continue...', () => {
      readline.close();
      resolve();
    });
  });
};

const get_selector = (input_selector) => {
  if (!input_selector) return { selector: null, frame_selector: null };
  const match = input_selector.match(/^(iframe(?:[^\s]*\[[^\]]+\]|[^\s]+))\s+(.+)$/);
  if (!match) return { selector: input_selector, frame_selector: null };
  return { frame_selector: match[1], selector: match[2] };
};

const ask = async (message, mask) => {
  const readline = require('readline');
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: true
  });

  if (!mask) {
    return await new Promise((resolve) => {
      rl.question(`${message}: `, (input) => {
        rl.close();
        resolve(input);
      });
    });
  }

  return await new Promise((resolve) => {
    const stdin = process.stdin;
    const raw = stdin.isRaw;

    let input = '';

    if (stdin.isTTY) {
      stdin.setRawMode(true);
    }
    readline.emitKeypressEvents(stdin, rl);

    const refresh = () => {
      rl.output.write('\x1b[2K\r');
      rl.output.write(`${message}: ${mask.repeat(input.length)}`);
    };

    rl.output.write(`${message}: `);

    const onKeypress = (str, key) => {
      // Enter -> finish
      if (key && (key.name === 'return' || key.name === 'enter')) {
        stdin.removeListener('keypress', onKeypress);
        if (stdin.isTTY) {
          stdin.setRawMode(raw);
        }
        rl.output.write('\n');
        rl.close();
        resolve(input);
        return;
      }

      // Backspace -> remove last char
      if (key && key.name === 'backspace') {
        if (input.length > 0) {
          input = input.slice(0, -1);
          refresh();
        }
        return;
      }

      input += str;
      refresh();
    };

    stdin.on('keypress', onKeypress);
  });
};

const locate = async (locator) => {
  const count = await locator.count();
  if (count > 0) {
    for (let i = 0; i < count; i++) {
      const el = await locator.nth(i);
      if (await el.isVisible()) {
        // console.log('locator.nth(', i, ') is visible');
        return el;
      }
    }
  } else {
    // console.warn('No element found for', locator);
  }
};

const click = async (page, selector, timeout = 3 * 1000, frame_selector = null) => {
  const LOCATOR_ALL_TIMEOUT = 100; // ms
  const locator = frame_selector ? page.frameLocator(frame_selector).locator(selector) : page.locator(selector);
  try {
    const elements = await locator.all();
    console.log(`Found ${elements.length} elements for selector "${selector}"`);
    for (const element of elements) {
      console.log('Checking', element);
      if (await element.isVisible()) {
        console.log('Clicking on', element);
        try {
          await element.click();
          return;
        } catch (err) {
          // Unable to click, try the next one
          console.warn('Failed to click on', selector, element, err);
        }
      }
    }
  } catch (e) {
    if (timeout > 0 && e.constructor.name === 'TimeoutError') {
      console.log('retrying click', selector, timeout);
      return await click(page, selector, timeout - LOCATOR_ALL_TIMEOUT, frame_selector);
    }
    throw e;
  }
};

const init = async () => {
  const FRIENDLY_CHARS = '23456789abcdefghijkmnpqrstuvwxyz';
  const generator = nanoid.customAlphabet(FRIENDLY_CHARS, 6);
  const id = generator();
  const directory = `user-data-dir/${id}`;
  const context = await chromium.launchPersistentContext(directory, {
    headless: false,
    channel: 'chromium',
    viewport: { width: 1920, height: 1080 }
  });
  const page = context.pages()[0];
  return { id, context, page };
};

const search = (dir) => {
  let results = [];
  const list = fs.readdirSync(dir);
  for (const file of list) {
    const name = path.join(dir, file);
    const stat = fs.statSync(name);
    if (stat && stat.isDirectory()) {
      results = results.concat(search(name));
    } else {
      results.push(name);
    }
  }
  return results;
};

const parse = (html) => {
  const dom = new JSDOM(html, { virtualConsole: new VirtualConsole() });
  return dom.window.document;
};

const distill = async (hostname, page, patterns) => {
  let result = [];
  for (const item of patterns) {
    const { name, pattern } = item;

    const root = pattern.querySelector('html');
    const priority = root ? root.getAttribute('gg-priority') || -1 : -1;
    const domain = root?.getAttribute('gg-domain');

    if (domain && hostname) {
      const local = hostname.includes('localhost') || hostname.includes('127.0.0.1');
      if (!local && !hostname.toLowerCase().includes(domain.toLowerCase())) {
        MIDDLEMAN_DEBUG && console.log(`${GRAY}Skipping ${name} due to mismatched domain ${domain}${NORMAL}`);
        continue;
      }
    }

    console.log('Checking', name, 'with priority', priority);

    let found = true;
    const matches = [];
    const targets = pattern.querySelectorAll('[gg-match], [gg-match-html]');
    for (const target of targets) {
      const html = target.hasAttribute('gg-match-html');
      const { selector, frame_selector } = get_selector(
        html ? target.getAttribute('gg-match-html') : target.getAttribute('gg-match')
      );

      const source = await locate(
        frame_selector ? page.frameLocator(frame_selector).locator(selector) : page.locator(selector)
      );
      if (source) {
        if (html) {
          const html = await source.innerHTML();
          target.innerHTML = html;
        } else {
          const text = (await source.textContent()).trim();
          if (text.length > 0) {
            target.textContent = text.trim();
          }
        }
        matches.push(source);
      } else {
        const optional = target.hasAttribute('gg-optional');
        MIDDLEMAN_DEBUG && optional && console.log(`${GRAY}Optional ${selector} has no match${NORMAL}`);
        const mandatory = !optional;
        if (mandatory) {
          found = false;
        }
      }
    }

    if (found && matches.length > 0) {
      const distilled = pattern.documentElement.outerHTML;
      result.push({ name, priority, distilled, matches });
    }
  }

  result = result.sort((a, b) => a.priority - b.priority);
  if (result.length === 0) {
    MIDDLEMAN_DEBUG && console.warn('No matches found');
    return;
  } else {
    if (MIDDLEMAN_DEBUG) {
      console.log('Number of matches', result.length);
      result.forEach((item) => {
        const { name, priority } = item;
        console.log(' -', name, 'with priority', priority);
      });
    }
    const match = result[0];
    console.log(`${YELLOW}${CHECK} Best match: ${BOLD}${match.name}${NORMAL}`);
    return match;
  }
};

const autofill = async (page, distilled) => {
  const document = parse(distilled);
  const root = document.querySelector('html');
  const domain = root?.getAttribute('gg-domain');

  const processed = [];

  for (const element of document.querySelectorAll('input[type]')) {
    const type = element.getAttribute('type');
    const name = element.getAttribute('name');
    if (!name || name.length === 0) {
      console.error(`${CROSS}${RED} There is an input (of type ${type}) without a name!`);
    }
    const { selector, frame_selector } = get_selector(element?.getAttribute('gg-match'));
    if (!selector) {
      console.error(`${CROSS}${RED} There is an input (of type ${type}) without a selector!`);
      continue;
    }

    if (['email', 'tel', 'text', 'password'].includes(type)) {
      const field = name || type;
      MIDDLEMAN_DEBUG && console.log(`${ARROW} Autofilling type=${type} name=${name}...`);
      const source = domain ? domain + '_' + field : field;
      const key = source.toUpperCase();
      const value = process.env[key];
      if (value && value.length && value.length > 0) {
        console.log(`${CYAN}${ARROW} Using ${BOLD}${key}${NORMAL} for ${field}${NORMAL}`);
        if (frame_selector) {
          await page.frameLocator(frame_selector).locator(selector).fill(value);
        } else {
          await page.fill(selector, value);
        }
        element.setAttribute('value', value);
      } else {
        const placeholder = element.getAttribute('placeholder');
        const prompt = placeholder || `Please enter ${field}`;
        const mask = field === 'password' ? '*' : null;
        const value = await ask(prompt, mask);
        if (frame_selector) {
          await page.frameLocator(frame_selector).locator(selector).fill(value);
        } else {
          await page.fill(selector, value);
        }
        element.setAttribute('value', value);
      }
      await sleep(0.25);
    } else if (type === 'radio') {
      const name = element.getAttribute('name');
      if (!name) {
        console.error(`${CROSS}${RED} There is no name for radio button with id ${element.id}!`);
        continue;
      }
      if (processed.includes(name)) {
        continue;
      }
      processed.push(name);
      const choices = [];
      console.log();
      for (const button of document.querySelectorAll(`input[type=radio][name="${name}"]`)) {
        const id = button.id;
        const label = document.querySelector(`label[for="${id}"]`)?.textContent;
        choices.push({ id, label });
        console.log(` ${choices.length}. ${label || id}`);
      }
      let choice = 0;
      while (choice < 1 || choice > choices.length) {
        const answer = await ask(`Your choice (1-${choices.length})`);
        choice = parseInt(answer, 10);
      }
      console.log(`${CYAN}${ARROW} Choosing ${YELLOW}${choices[choice - 1].label}${NORMAL}`);
      console.log();
      const radio = document.querySelector(`input[type=radio]#${choices[choice - 1].id}`);
      const { selector, frame_selector } = get_selector(radio?.getAttribute('gg-match'));
      if (frame_selector) {
        await page.frameLocator(frame_selector).locator(selector).check();
      } else {
        await page.check(selector);
      }
    }
  }

  return document.documentElement.outerHTML;
};

const autoclick = async (page, distilled) => {
  const document = parse(distilled);
  const buttons = document.querySelectorAll('[gg-autoclick]');
  for (const button of buttons) {
    const { selector, frame_selector } = get_selector(button.getAttribute('gg-match'));
    if (selector) {
      console.log(`${CYAN}${ARROW} Auto-clicking ${NORMAL}${selector}`);
      await click(page, selector, 3 * 1000, frame_selector);
    }
  }
};

const terminate = async (page, distilled) => {
  const document = parse(distilled);
  const stops = document.querySelectorAll('[gg-stop]');
  if (stops.length > 0) {
    console.log('Found stop elements, terminating session...');
    return true;
  }
  return false;
};

const extractValue = (item, attribute = null) => {
  if (attribute) {
    let value = item.getAttribute(attribute);

    if (Array.isArray(value)) {
      value = value.length > 0 ? value[0] : '';
    }

    return typeof value === 'string' ? value.trim() : '';
  }

  return item.textContent.trim();
};

const convert = async (page, distilled) => {
  const document = parse(distilled);
  const snippet = document.querySelector('script[type="application/json"]');
  if (snippet) {
    console.log(`${GREEN}${ARROW} Found a data converter.${NORMAL}`);
    MIDDLEMAN_DEBUG && console.log(`${snippet.innerHTML}`);
    try {
      const converter = JSON.parse(snippet.innerHTML);
      MIDDLEMAN_DEBUG && console.log('Start converting using', converter);

      const rows = Array.from(document.querySelectorAll(converter.rows));
      console.log(`  Finding rows using ${CYAN}${converter.rows}${NORMAL}: found ${GREEN}${rows.length}${NORMAL}.`);
      const converted = [];
      rows.forEach((el, i) => {
        MIDDLEMAN_DEBUG && console.log(` Converting row ${GREEN}${i + 1}${NORMAL} of ${rows.length}`);
        const kv = {};
        converter.columns.forEach(({ name, selector, attribute, kind }) => {
          if (kind === 'list') {
            const items = el.querySelectorAll(selector);
            kv[name] = Array.from(items).map((item) => extractValue(item, attribute));
          } else {
            const item = el.querySelector(selector);
            kv[name] = extractValue(item, attribute);
          }
        });
        if (Object.keys(kv).length > 0) {
          converted.push(kv);
        }
      });
      console.log(`${GREEN}${CHECK} Conversion done for ${GREEN}${converted.length}${NORMAL} entries.`);
      return converted;
    } catch (error) {
      console.error(`${RED}Conversion error:${NORMAL}`, error.message);
    }
  }
};

const render = (content, options = {}) => {
  const title = options.title || 'MIDDLEMAN';
  const action = options.action;
  return `<!doctype html>
<html data-theme=light>
  <head>
    <title>${title}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
    <style>
      .vertical-radios {
        display: flex;
        flex-direction: column;
        gap: 1rem;
        margin-bottom: 1.5rem;
      }

      .radio-wrapper {
        display: flex;
        align-items: center;
        gap: 0.5rem;
      }

      .radio-wrapper input[type='radio'] {
        margin: 0;
        flex-shrink: 0;
      }

      .radio-wrapper label {
        margin: 0;
        cursor: pointer;
        line-height: 1.5;
      }

      .radio-wrapper:hover label {
        color: var(--pico-primary);
      }
    </style>
  </head>
  <body>
    <main class="container">
      <section>
        <h2>${title}</h2>
        <articles>
        <form method="POST" action="${action}">
        ${content}
        </form>
        </articles>
      </section>
    </main>
  </body>
</html>`;
};

(async () => {
  const [, , command, parameter, option] = process.argv;

  const patterns = search('./specs')
    .filter((name) => name.endsWith('.html'))
    .map((name) => {
      const content = fs.readFileSync(name, 'utf-8');
      const pattern = parse(content);
      return { name, pattern };
    });

  if (command === 'list') {
    patterns.forEach(({ name }) => console.log(name.replace('specs/', '')));
    return;
  }

  if (command === 'distill' && parameter) {
    const location = parameter;

    let hostname, context, page;
    console.log('Distilling', location);

    if (location.startsWith('http')) {
      hostname = new URL(location).hostname;
      context = await chromium.launch({
        headless: false,
        channel: 'chromium',
        viewport: { width: 1920, height: 1080 }
      });
      page = await context.newPage();
      MIDDLEMAN_PAUSE && (await pause());
      await page.goto(location);
    } else {
      hostname = option;
      context = await chromium.launch({
        javascriptEnabled: false,
        headless: false,
        channel: 'chromium',
        viewport: { width: 1920, height: 1080 }
      });
      page = await context.newPage();
      await page.setContent(fs.readFileSync(location, 'utf-8'));
    }

    const match = await distill(hostname, page, patterns);

    if (match) {
      const { distilled } = match;
      console.log();
      console.log(await prettier.format(distilled, { parser: 'html', printWidth: 120 }));
      console.log();

      if (await terminate(page, distilled)) {
        console.log(`${GREEN}${CHECK} Finished!${NORMAL}`);
        const converted = await convert(page, distilled);
        if (converted) {
          console.log();
          console.log(converted);
          console.log();
        }
      }
    }

    MIDDLEMAN_PAUSE && (await pause());
    await context.close();

    return;
  }

  if (command === 'run' && parameter) {
    let location = parameter;
    if (!location.startsWith('http')) {
      location = `https://${location}`;
    }
    const hostname = new URL(location).hostname;

    const { id, context, page } = await init();
    console.log('Starting browser', id);
    MIDDLEMAN_PAUSE && (await pause());

    console.log(`${GREEN}${ARROW} Navigating to ${NORMAL}${location}`);
    await page.goto(location);

    const TICK = 1; // seconds
    const TIMEOUT = 15; // seconds
    const max = TIMEOUT / TICK;

    let current = { name: null, distilled: null };
    for (let iteration = 0; iteration < max; iteration++) {
      console.log();
      console.log(`${MAGENTA}Iteration ${1 + iteration}${NORMAL} of ${max}`);
      await sleep(TICK);
      const match = await distill(hostname, page, patterns);
      if (match) {
        if (match.distilled === current.distilled) {
          console.log('Still the same:', match.name);
        } else {
          const distilled = await autofill(page, match.distilled);
          current.name = match.name;
          current.distilled = distilled;
          console.log();
          console.log(await prettier.format(distilled, { parser: 'html', printWidth: 120 }));

          await autoclick(page, distilled);
          if (await terminate(page, distilled)) {
            const converted = await convert(page, distilled);
            if (converted) {
              console.log();
              console.log(converted);
            }
            break;
          }
        }
      } else {
        console.warn(`${CROSS}${RED} No matched pattern found${NORMAL}`);
      }
    }

    console.log();
    console.log('Terminating browser', id);
    MIDDLEMAN_PAUSE && (await pause());
    await context.close();
    console.log('Terminated.');

    return;
  }

  if (command === 'inspect' && parameter) {
    const directory = `user-data-dir/${parameter}`;
    const context = await chromium.launchPersistentContext(directory, {
      headless: false
    });
    const page = context.pages()[0];
    if (option && option.length > 0) {
      const url = option.startsWith('http') ? option : `https://${option}`;
      await page.goto(url);
    }
    await pause();
    await context.close();
    return;
  }

  const app = new Hono();

  app.get('/health', (c) => c.text(`OK ${Date.now()}`));

  app.get('/', (c) => {
    const examples = [
      { title: 'NYT Best Sellers', link: '/start?location=www.nytimes.com/books/best-sellers' },
      { title: 'Slashdot: Most Discussed', link: '/start?location=technology.slashdot.org' },
      { title: 'Goodreads Bookshelf', link: '/start?location=goodreads.com/signin' },
      { title: 'BBC Saved Articles', link: '/start?location=bbc.com/saved' },
      { title: 'Amazon Browsing History', link: '/start?location=amazon.com/gp/history' },
      { title: 'Gofood Order History', link: '/start?location=gofood.co.id/en/orders' },
      { title: 'Agoda Booking History', link: '/start?location=agoda.com/account/bookings.html' },
      { title: 'ESPN College Football Schedule', link: '/start?location=espn.com/college-football/schedule' },
      { title: 'NBA Key Dates', link: '/start?location=nba.com/news/key-dates' }
    ];

    const itemize = ({ title, link }) => `<li><a href="${link}" target="_blank">${title}</a></li>`;
    const content = `<p>Try the following examples:</p><ul>${examples.map(itemize).join('\n')}</ul>`;
    return c.html(render(content));
  });

  const browsers = {}; // id => { context, page }

  app.get('/start', async (c) => {
    let location = c.req.query('location');
    if (!location) {
      return c.text('Missing location parameter', 400);
    }
    if (!location.startsWith('http')) {
      location = `https://${location}`;
    }
    const hostname = new URL(location).hostname;

    const { id, context, page } = await init();
    console.log(`${GREEN}${ARROW} Browser launched with generated id: ${BOLD}${id}${NORMAL}`);
    browsers[id] = { hostname, location, context, page };
    MIDDLEMAN_PAUSE && (await pause());

    console.log(`${GREEN}${ARROW} Navigating to ${NORMAL}${location}`);
    await page.goto(location);

    // Since the browser can't redirect from GET to POST,
    // we'll use an auto-submit form to do that.

    return c.html(`
    <!DOCTYPE html>
    <html>
    <body>
      <form id="redirect" action="/link/${id}" method="post">
      </form>
      <script>document.getElementById('redirect').submit();</script>
    </body>
    </html>
  `);
  });

  app.post('/link/:id', async (c) => {
    const id = c.req.param('id');
    const browser = browsers[id];
    if (!browser) {
      return c.text(`Invalid id: ${id}`, 404);
    }

    const { hostname, context, page } = browser;
    console.log(`${GREEN}${ARROW} Continuing automation for ${BOLD}${id}${NORMAL} at ${BOLD}${hostname}${NORMAL}`);

    const fields = Object.fromEntries(Object.entries(await c.req.parseBody()));

    const TICK = 1; // seconds
    const TIMEOUT = 15; // seconds
    const max = TIMEOUT / TICK;

    let current = { name: null, distilled: null };
    for (let iteration = 0; iteration < max; iteration++) {
      console.log();
      console.log(`${MAGENTA}Iteration ${1 + iteration}${NORMAL} of ${max}`);
      await sleep(TICK);
      const match = await distill(hostname, page, patterns);
      if (!match) {
        console.warn(`${CROSS}${RED} No matched pattern found${NORMAL}`);
        continue;
      }

      const { distilled } = match;
      if (distilled === current.distilled) {
        console.log(`${ARROW} Still the same : ${match.name}`);
        continue;
      }

      current.name = match.name;
      current.distilled = distilled;
      console.log();
      console.log(await prettier.format(current.distilled, { parser: 'html', printWidth: 120 }));

      const names = [];
      const document = parse(distilled);
      const inputs = document.querySelectorAll('input');
      for (const input of inputs) {
        const { selector, frame_selector } = get_selector(input.getAttribute('gg-match'));
        const name = input.name;
        if (selector) {
          if (input.type === 'checkbox') {
            names.push(name || 'checkbox');
            console.log(`${CYAN}${ARROW} Handling ${NORMAL}${selector} using autoclick`);
          } else if (input.type === 'radio') {
            const value = fields[name];
            if (!value || value.length === 0) {
              console.warn(`${CROSS}${RED} No form data found for radio button group ${BOLD}${name}${NORMAL}`);
              continue;
            }
            const radio = document.querySelector(`input[type=radio][id="${value}"]`);
            if (!radio) {
              console.warn(`${CROSS}${RED} No radio button found with id ${BOLD}${value}${NORMAL}`);
              continue;
            }
            console.log(`${CYAN}${ARROW} Handling radio button group ${BOLD}${name}${NORMAL}`);
            console.log(`${CYAN}${ARROW} Using form data ${BOLD}${name}=${value}${NORMAL}`);
            const { selector, frame_selector } = get_selector(radio?.getAttribute('gg-match'));
            if (frame_selector) {
              await page.frameLocator(frame_selector).locator(selector).check();
            } else {
              await page.check(selector);
            }
            radio.setAttribute('checked', 'checked');
            current.distilled = document.documentElement.outerHTML;
            names.push(input.id || 'radio');
            await sleep(0.25);
          } else if (name) {
            const value = fields[name];
            if (value && value.length && value.length > 0) {
              console.log(`${CYAN}${ARROW} Using form data ${BOLD}${name}${NORMAL}`);
              names.push(name);
              input.value = value;
              if (frame_selector) {
                await page.frameLocator(frame_selector).locator(selector).fill(value);
              } else {
                await page.fill(selector, value);
              }
              input.setAttribute('value', value);
              current.distilled = document.documentElement.outerHTML;
              delete fields[name];
              await sleep(0.25);
            } else {
              console.warn(`${CROSS}${RED} No form data found for ${BOLD}${name}${NORMAL}`);
            }
          }
        }
      }

      console.log(await prettier.format(current.distilled, { parser: 'html', printWidth: 120 }));

      const title = document.title;
      const action = `/link/${id}`;

      if (names.length > 0 && inputs.length === names.length) {
        await autoclick(page, distilled);
        if (await terminate(page, distilled)) {
          console.log(`${GREEN}${CHECK} Finished!${NORMAL}`);
          const converted = await convert(page, distilled);
          await context.close();
          if (converted) {
            console.log();
            console.log(converted);
            return c.json(converted);
          }
          return c.html(render(document.body.innerHTML, { title, action }));
        }

        console.log(`${GREEN}${CHECK} All form fields are filled${NORMAL}`);
        continue;
      }

      if (await terminate(page, distilled)) {
        console.log(`${GREEN}${CHECK} Finished!${NORMAL}`);
        const converted = await convert(page, distilled);
        await context.close();
        if (converted) {
          return c.json(converted);
        }
      } else {
        console.warn(`${CROSS}${RED} Not all form fields are filled${NORMAL}`);
      }

      return c.html(render(document.body.innerHTML, { title, action }));
    }

    return c.text('Unexpected error', 503);
  });

  app.use('*', serveStatic({ root: './public' }));

  const port = process.env.PORT || 3000;
  serve({ fetch: app.fetch, port });
  console.log('Listening on port', port);
})();
