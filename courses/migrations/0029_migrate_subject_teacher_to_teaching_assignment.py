from django.db import migrations


def migrate_forward(apps, schema_editor):
    """Copy every SubjectTeacher row in as a course-wide (batch=NULL)
    TeachingAssignment. Idempotent (get_or_create) so re-running after an
    interruption is safe.

    SubjectTeacher never enforced "one primary per subject" (its own unique
    constraint was only (subject, teacher)), but TeachingAssignment's new
    course-wide constraint does — so if a subject somehow has more than one
    PRIMARY row (shouldn't happen, but this must not crash the migration on
    real data), every PRIMARY after the first for that subject is copied in
    as ASSISTANT instead.
    """
    SubjectTeacher = apps.get_model('courses', 'SubjectTeacher')
    TeachingAssignment = apps.get_model('courses', 'TeachingAssignment')

    seen_primary_for_subject = set()
    for st in SubjectTeacher.objects.order_by('subject_id', 'order', 'id'):
        role = st.display_role
        if role == 'PRIMARY':
            if st.subject_id in seen_primary_for_subject:
                role = 'ASSISTANT'
            else:
                seen_primary_for_subject.add(st.subject_id)

        TeachingAssignment.objects.get_or_create(
            batch=None, subject_id=st.subject_id, teacher_id=st.teacher_id,
            is_active=True,
            defaults={'role': role, 'order': st.order},
        )


def migrate_backward(apps, schema_editor):
    """Delete only the course-wide TeachingAssignment rows this migration
    created — batch-scoped rows and anything created through the app after
    this migration ran are left alone."""
    TeachingAssignment = apps.get_model('courses', 'TeachingAssignment')
    SubjectTeacher = apps.get_model('courses', 'SubjectTeacher')

    pairs = set(
        SubjectTeacher.objects.values_list('subject_id', 'teacher_id')
    )
    for subject_id, teacher_id in pairs:
        TeachingAssignment.objects.filter(
            batch__isnull=True, subject_id=subject_id, teacher_id=teacher_id,
        ).delete()


class Migration(migrations.Migration):
    """Data-only, step 2 of 3 for retiring SubjectTeacher. See
    0028_teachingassignment_batch_nullable and
    0030_remove_subjectteacher for the schema steps either side of this."""

    dependencies = [
        ('courses', '0028_teachingassignment_batch_nullable'),
    ]

    operations = [
        migrations.RunPython(migrate_forward, migrate_backward),
    ]
