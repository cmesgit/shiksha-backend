from django.apps import AppConfig


class ChatConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "chat"

    def ready(self):
        # M3 (Phase 3 §10): register the Academy "course" capability
        # provider. Delegates straight to the existing, UNCHANGED
        # services.can_join_course_room — chat/policy.py itself never
        # imports courses/skills models; only this one registration line
        # (via chat's own services module, never a vertical's) does.
        from . import policy, services

        policy.register_provider("course", services.can_join_course_room)
