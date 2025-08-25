# MIDDLEMAN

For the JavaScript version, use [Node.js](https://nodejs.org) >= 22 and run the following (this will also install Patchright):

```bash
npm install
./middleman.js
```

For the Python version, use [uv](https://docs.astral.sh/uv) and run:

```bash
uv sync
.venv/bin/patchright install chromium
./middleman.py
```

Then open `localhost:3000` and choose one of the examples.
