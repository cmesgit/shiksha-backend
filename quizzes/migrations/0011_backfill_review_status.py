from django.db import migrations


def forwards(apps, schema_editor):
    """Quizzes that already existed before the admin-verification workflow
    was introduced don't have a meaningful review_status yet (they all
    default to 'draft'). Backfill so already-live quizzes stay live and
    keep their approved standing, while unpublished ones stay drafts."""
    Quiz = apps.get_model("quizzes", "Quiz")
    Quiz.objects.filter(is_published=True).update(review_status="approved")
    Quiz.objects.filter(is_published=False).update(review_status="draft")


def backwards(apps, schema_editor):
    # No-op: review_status simply reverts to its default on rollback.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("quizzes", "0010_quiz_workflow_fields"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
