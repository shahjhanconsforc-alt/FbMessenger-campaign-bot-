# messenger-bot — FastAPI console + Playwright-driven Google Chrome.
#
# Built for the VM deployment: the container has no display of its own and
# shows its Chrome windows on the shared web desktop (see docker-compose.yml and
# DEPLOY.md). Google Chrome stable is installed next to Playwright's bundled
# Chromium so saved profiles made by a desktop Chrome keep working, and the
# console's default channel ("chrome") resolves.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Deps first, so this layer is cached until requirements.txt changes.
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache/pip

# Google Chrome stable (the "chrome" channel the console prefers).
RUN apt-get update \
 && apt-get install -y --no-install-recommends wget ca-certificates fonts-liberation xauth \
 && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm -f /tmp/chrome.deb \
 && rm -rf /var/lib/apt/lists/*

# The code. In the VM deployment the checkout is bind-mounted over this, so a
# `git pull` + restart is a deploy; the copy makes the image self-contained too.
COPY . .

RUN chmod +x /app/docker-entrypoint.sh

# Production defaults; .env / compose override them.
ENV XBOT_HOST=0.0.0.0 \
    XBOT_PORT=4300 \
    XBOT_HEADLESS=0 \
    XBOT_BROWSER_CHANNEL=chrome \
    XBOT_RELOAD=0 \
    DISPLAY=:1

EXPOSE 4300

ENTRYPOINT ["/app/docker-entrypoint.sh"]
