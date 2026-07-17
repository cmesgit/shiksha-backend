# PLACEMENT: backend/backend/forum/migrations/0006_solved_views_profile.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/forum/migrations/0006_solved_views_profile.py
#
# Adds:
#   - ForumPost.view_count       (incremented once per viewer on thread detail)
#   - ForumPost.is_solved        (set when the thread author accepts a reply)
#   - ForumPost.accepted_reply   (FK to the accepted Reply, nullable)
#   - ForumProfile               (one-to-one bio, everything else computed on read)

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("forum", "0005_delete_notification"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="forumpost",
            name="view_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="forumpost",
            name="is_solved",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="forumpost",
            name="accepted_reply",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="accepted_for_post",
                to="forum.reply",
            ),
        ),
        migrations.CreateModel(
            name="ForumProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("bio", models.CharField(blank=True, default="", max_length=280)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="forum_profile", to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
