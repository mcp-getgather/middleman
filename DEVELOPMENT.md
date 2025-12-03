# DEVELOPMENT

For the Python version, use [uv](https://docs.astral.sh/uv) and run:

```bash
uv sync
./middleman.py
```

Then open `localhost:3000` and choose one of the examples.

Open `localhost:3001` to view the containerized desktop live.

## CLI

To run a series of automation tasks on a specific location:

```bash
./middleman.py run thriftbooks.com/list
./middleman.py run www.goodreads.com/review
./middleman.py run www.bbc.com/saved
./middleman.py run www.amazon.com/gp/history
```

To perform a simple page distillation on a remote URL:

```bash
./middleman.py distill www.goodreads.com/user/sign_in
```

For more elaborate use cases:

```bash
MIDDLEMAN_PAUSE=1 MIDDLEMAN_DEBUG=1 ./middleman.py distill thriftbooks.com/list
```

- `MIDDLEMAN_PAUSE=1` will pause the process at certain steps to allow for inspection.
- `MIDDLEMAN_DEBUG=1` will print additional debugging messages.
