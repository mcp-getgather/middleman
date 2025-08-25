# DEVELOPMENT

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

Alternatively, use [Docker](https://docker.com) or [Podman](https://podman.io) to build the container image and run it:

```bash
docker build -t middleman .
docker run --rm --name middleman -p 3000:3000 -p 3001:3001 middleman
```

Then open `localhost:3000` and pick one of the examples.

Open `localhost:3001` to view the containerized desktop live.
