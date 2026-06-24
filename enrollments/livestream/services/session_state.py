import redis
import json
from django.utils import timezone

r = redis.Redis(host="127.0.0.1", port=6379, db=0)


def _key(session_id):
    return f"live_session:{session_id}"


def set_session_state(session):
    data = {
        "status": session.computed_status(),  # 🔥 FIXED
        "teacher_left_at": (
            session.teacher_left_at.isoformat()
            if session.teacher_left_at else None
        ),
        "last_activity_at": timezone.now().isoformat(),
    }

    r.set(_key(session.id), json.dumps(data), ex=3600)


def get_session_state(session_id):
    data = r.get(_key(session_id))
    if data:
        return json.loads(data)
    return None
