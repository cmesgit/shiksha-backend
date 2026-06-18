from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import User, Profile


@receiver(post_save, sender=User)
def create_profile(sender, instance, created, **kwargs):
    defaults = {
        "full_name": instance.username or instance.email,
        "first_name": instance.username or instance.email,
        "student_id": f"STU-{instance.id.hex[:8]}",
    }
    Profile.objects.get_or_create(
        user=instance,
        defaults=defaults,
    )
