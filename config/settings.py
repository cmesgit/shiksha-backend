"""Settings dispatcher.

Reads DJANGO_ENV from the environment and loads the matching settings module.
Defaults to production. Set DJANGO_ENV=dev on the dev droplet's .env file.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env BEFORE checking DJANGO_ENV below. settings_base.py also calls
# load_dotenv() further down this import chain, but by then this file has
# already picked a branch — if DJANGO_ENV only lives in .env (not a real
# exported shell/systemd env var), that check always saw None and always
# fell through to settings_prod, no matter what .env said.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

if os.getenv("DJANGO_ENV") == "dev":
    from .settings_dev import *  # noqa: F401,F403
else:
    from .settings_prod import *  # noqa: F401,F403
