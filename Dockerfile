# Asana Inbox web UI.
#
#   docker build -t asana-inbox .
#   docker run --rm -p 8765:8765 asana-inbox --fake                       # demo, no Asana
#   docker run --rm -p 8765:8765 -e ASANA_TOKEN=… asana-inbox              # personal access token
#   docker run --rm -p 8765:8765 -e ASANA_CLIENT_ID=… -e ASANA_CLIENT_SECRET=… \
#       -e ASANA_REDIRECT_URI=http://localhost:8765/auth/callback asana-inbox   # OAuth
#
# Then open http://localhost:8765/. Optional settings (all env vars; see --help):
#   ASANA_INBOX_LOGO_URL, ASANA_INBOX_FAVICON_URL   header logo / tab icon (http(s):// or data:image/)
#   ASANA_INBOX_IMG_SRC                             CSP img-src sources (default: 'self' + those origins)
#   ASANA_ALLOWED_HOSTS                             host names it's served under, e.g. behind a proxy
#   ASANA_WORKSPACE, ASANA_IGNORE_PROJECTS, ASANA_SCOPES, ASANA_PORT

FROM python:3.12-slim

# tini as PID 1: a container's first process ignores Ctrl-C (SIGINT) and `docker stop` (SIGTERM)
# unless it handles them; tini passes them on to the server, which shuts down cleanly.
RUN apt-get update && apt-get install -y --no-install-recommends tini && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --chown=app asana_inbox_web.py asana_api.py asana_oauth.py inbox_core.py ./
USER app

# The script's dependencies (its `# /// script` block) go into a virtualenv at build time, so the
# container needs no network to start and Python runs directly under tini (no uv in between).
ENV UV_PYTHON_DOWNLOADS=never UV_NO_PROGRESS=1 UV_CACHE_DIR=/tmp/uv-cache
RUN python -c "import re, tomllib; block = re.search(r'(?ms)^# /// script\n(.*?)^# ///', open('asana_inbox_web.py').read())[1]; print('\n'.join(tomllib.loads(''.join(line[2:] for line in block.splitlines(True)))['dependencies']))" > /home/app/requirements.txt \
    && uv venv /home/app/venv \
    && uv pip install --python /home/app/venv/bin/python -r /home/app/requirements.txt \
    && rm -rf /tmp/uv-cache

ENV PYTHONUNBUFFERED=1 ASANA_HOST=0.0.0.0 ASANA_PORT=8765
EXPOSE 8765
ENTRYPOINT ["tini", "--", "/home/app/venv/bin/python", "asana_inbox_web.py"]
