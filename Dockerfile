FROM mirror.gcr.io/library/python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8.4 /uv /uvx /bin/

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    xvfb \
    xauth \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libxss1 \
    libasound2 \
    libgbm1 \
    libxshmfence1 \
    fonts-liberation \
    libu2f-udev \
    libvulkan1 \
    x11vnc \
    jwm \
    x11-apps \
    novnc \
    websockify \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
ENV PYTHONFAULTHANDLER=1

WORKDIR /app

COPY pyproject.toml uv.lock* ./

ENV VENV_PATH="/app/.venv"
ENV UV_FROZEN=1
RUN uv sync --no-dev --no-install-workspace

ENV PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1
RUN $VENV_PATH/bin/patchright install --with-deps chromium

COPY middleman.py /app/middleman.py
COPY specs /app/specs/
COPY entrypoint.sh /app/entrypoint.sh

RUN uv sync --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 3000

RUN cp /usr/share/novnc/vnc_lite.html /usr/share/novnc/index.html
RUN sed -i 's/rfb.scaleViewport = readQueryVariable.*$/rfb.scaleViewport = true;/' /usr/share/novnc/index.html
EXPOSE 3001

ENTRYPOINT ["/app/entrypoint.sh"]
