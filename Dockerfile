# Django 6.0.x declares Requires-Python >=3.12, so this image previously could
# not even complete `pip install -r requirements.txt` — the build failed, it was
# not a latent runtime problem. 3.12 is the MINIMUM compatible bump: nothing
# else pinned here needs above 3.10 (channels/daphne/celery 3.9, uvicorn/psycopg
# 3.10), so this is not an opportunistic upgrade.
#
# Note this image is NOT how Shiksha is deployed — scripts/deploy.sh runs prod
# from a venv at /app/shiksha-backend under uvicorn and dev under gunicorn. See
# docs/architecture/shiksha-remote/evidence/runtime-baseline.md before assuming
# a change here reaches a server.
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput

# ASGI (uvicorn) so both HTTP and WebSockets (Channels) are served by one process.
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--workers", "3"]
