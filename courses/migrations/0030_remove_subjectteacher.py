from django.db import migrations


class Migration(migrations.Migration):
    """Schema-only, step 3 of 3 for retiring SubjectTeacher. By this point
    every row has been copied into TeachingAssignment as a course-wide
    (batch=NULL) row (0029_migrate_subject_teacher_to_teaching_assignment),
    and every code path that read SubjectTeacher directly has been converted
    to TeachingAssignment / the teaches_subject()/is_teacher_of() helpers."""

    dependencies = [
        ('courses', '0029_migrate_subject_teacher_to_teaching_assignment'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='subjectteacher',
            name='unique_teacher_per_subject',
        ),
        migrations.DeleteModel(
            name='SubjectTeacher',
        ),
    ]
