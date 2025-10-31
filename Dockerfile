FROM mirror.gcr.io/library/python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8.4 /uv /uvx /bin/

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    chromium \
    tigervnc-standalone-server \
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
    dbus-x11 \
    jwm \
    x11-apps \
    xterm \
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

COPY middleman.py /app/middleman.py
COPY patterns /app/patterns/
COPY entrypoint.sh /app/entrypoint.sh
COPY .jwmrc /app/.jwmrc

RUN uv sync --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 3000

RUN cp /usr/share/novnc/vnc_lite.html /usr/share/novnc/index.html
RUN sed -i 's/rfb.scaleViewport = readQueryVariable.*$/rfb.scaleViewport = true;/' /usr/share/novnc/index.html
EXPOSE 3001

RUN useradd -m -s /bin/bash middleman && \
    chown -R middleman:middleman /app && \
    usermod -aG sudo middleman && \
    echo 'middleman ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

USER middleman

ENTRYPOINT ["/app/entrypoint.sh"]
