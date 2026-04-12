from config.celery import app
from django.utils import timezone
from datetime import timedelta


@app.task
def notify_session_starting_soon():
    """
    Runs every 15 minutes via Celery beat.
    Finds live sessions starting in the next 30 minutes
    and notifies the teacher once.
    """
    from django.contrib.contenttypes.models import ContentType
    from livestream.models import LiveSession
    from activity.models import Activity
    from livestream.services.notifications import push_ws_notification

    now = timezone.now()
    window_start = now + timedelta(minutes=25)
    window_end = now + timedelta(minutes=35)

    sessions = LiveSession.objects.filter(
        start_time__gte=window_start,
        start_time__lte=window_end,
        status=LiveSession.STATUS_SCHEDULED,
    ).select_related("created_by", "subject")

    content_type = ContentType.objects.get_for_model(LiveSession)

    for session in sessions:
        teacher = session.created_by
        if not teacher:
            continue

        subject = session.subject
        subject_id = subject.id if subject else None
        subject_name = subject.name if subject else ""

        # Dedup — only notify once per session
        already = Activity.objects.filter(
            user=teacher,
            type=Activity.TYPE_SESSION,
            content_type=content_type,
            object_id=session.id,
            title__startswith="Starting soon:",
        ).exists()

        if already:
            continue

        Activity.objects.create(
            user=teacher,
            type=Activity.TYPE_SESSION,
            title=f"Starting soon: {session.title}",
            due_date=session.start_time,
            subject_id=subject_id,
            subject_name=subject_name,
            content_type=content_type,
            object_id=session.id,
        )

        push_ws_notification(teacher.id, {
            "type": "live_session",
            "title": f"Starting soon: {session.title}",
            "subject_name": subject_name,
            "start_time": session.start_time.isoformat(),
            "id": str(session.id),
            "subject_id": str(subject_id) if subject_id else None,
        })
