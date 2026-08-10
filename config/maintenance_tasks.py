"""config/maintenance_tasks.py — Celery Beat wrappers for cleanup management
commands that already existed but were never actually scheduled anywhere
(each one's own docstring says "run via cron" — no cron or beat entry for
any of them existed on either box). Thin `call_command` wrappers rather than
moving the logic here, so `python manage.py <name>` keeps working unchanged
for manual/dry-run use.
"""
import logging

from celery import shared_task
from django.core.management import call_command

logger = logging.getLogger(__name__)


@shared_task
def cleanup_expired_sessions_task():
    call_command("cleanup_expired_sessions")


@shared_task
def cleanup_unverified_users_task():
    call_command("cleanup_unverified_users")


@shared_task
def cleanup_orphan_material_files_task():
    call_command("cleanup_orphan_material_files")
