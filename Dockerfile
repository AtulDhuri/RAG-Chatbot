# Microsoft's Playwright image ships Chromium + every system library it needs,
# preinstalled, so there is no apt-get / root step at build time. The tag is
# pinned to the same Playwright version as the Python package (1.49.1) and uses
# a Python 3.12 base, which has prebuilt wheels for all our pinned deps.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

# Install Python dependencies first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# The base image already includes the browser binaries, but this is a cheap,
# idempotent guarantee that the Chromium build matching this Playwright is present.
RUN playwright install chromium

# Copy the application code.
COPY . .

# ChromaDB persists here. On Render, mount a Disk at /app/data to keep the index
# across deploys; without a disk it is ephemeral and re-crawls on demand.
ENV CHROMA_PERSIST_DIR=/app/data/chroma

# Render provides $PORT at runtime; default to 8000 for local `docker run`.
ENV PORT=8000
EXPOSE 8000

# Shell form so $PORT is expanded at container start.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}