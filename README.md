# MIDDLEMAN

![Screenshot of Middleman](middleman.png)

First, run a containerized Chromium with active CDP (Chrome DevTools Protocol):
```bash
podman run -p 3001:3001 -p 9222:9222 ghcr.io/mcp-getgather/chromium-live
```

Open `localhost:3001` to view the containerized desktop live.

Then, run:
```bash
uv run middleman.py
```

Open `localhost:3000` and pick one of the examples.

See also [DEVELOPMENT.md](DEVELOPMENT.md).
