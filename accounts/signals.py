import uuid
from django.db import IntegrityError, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import User, Profile


@receiver(post_save, sender=User)
def create_profile(sender, instance, created, **kwargs):
    # Create the blank profile only. student_id is allocated lazily when a
    # *student* completes their profile (see StudentFormFillupSerializer), so
    # teacher accounts never receive one.
    if not created:
        return
    if Profile.objects.filter(user=instance).exists():
        return
    try:
        with transaction.atomic():
            Profile.objects.create(
                user=instance,
                full_name=instance.username or instance.email,
                first_name=instance.username or instance.email,
            )
    except IntegrityError:
        if not Profile.objects.filter(user=instance).exists():
            raise
