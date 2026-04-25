"""
Celery task re-exports for this app.

Celery's ``autodiscover_tasks()`` only looks for a module literally named
``tasks`` inside each installed app, so we bring in the actual task
implementations from ``study_group_tasks`` here.  This keeps the worker
registry aware of every task without having to touch ``config/celery.py``.
"""

from .study_group_tasks import hard_expire_study_group  # noqa: F401
