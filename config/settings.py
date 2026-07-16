"""Settings dispatcher.

Reads DJANGO_ENV from the environment and loads the matching settings module.
Defaults to production. Set DJANGO_ENV=dev on the dev droplet's .env file.
"""
import os

if os.getenv("DJANGO_ENV") == "dev":
    from .settings_dev import *  # noqa: F401,F403
else:
    from .settings_prod import *  # noqa: F401,F403


#testing
