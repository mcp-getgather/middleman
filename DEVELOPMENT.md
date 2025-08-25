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

## CLI

To run a series of automation tasks on a specific location:

```bash
./middleman.js run thriftbooks.com/list
./middleman.js run www.goodreads.com/review
./middleman.js run www.bbc.com/saved
./middleman.js run www.amazon.com/gp/history
```

After the automation is complete, a unique browser ID will be shown (e.g., `xyz123`). Use this ID to inspect the results:

```bash
./middleman.js inspect xyz123
```

To perform a simple page distillation on a remote URL:

```bash
./middleman.js distill www.goodreads.com/user/sign_in
```

For more elaborate use cases:

```bash
MIDDLEMAN_PAUSE=1 MIDDLEMAN_DEBUG=1 ./middleman.js distill thriftbooks.com/list
```

- `MIDDLEMAN_PAUSE=1` will pause the process at certain steps to allow for inspection.
- `MIDDLEMAN_DEBUG=1` will print additional debugging messages.
