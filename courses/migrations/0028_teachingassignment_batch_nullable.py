import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Schema-only, step 1 of 3 for retiring SubjectTeacher.

    Makes TeachingAssignment.batch nullable (NULL = course-wide, matching
    every other content model here) and adds the two constraints that only
    bite for course-wide rows — see courses/models.py's TeachingAssignment
    docstring for why the existing batch-scoped constraints don't cover
    batch=NULL (Postgres treats each NULL as distinct even under a matching
    partial unique index).

    SubjectTeacher itself is untouched here on purpose: step 2
    (0029_migrate_subject_teacher_to_teaching_assignment) copies its rows in
    as batch=NULL TeachingAssignments before step 3 drops it, so there's
    never a window where the data exists in neither place.
    """

    dependencies = [
        ('courses', '0027_coursenotifyrequest'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='teachingassignment',
            name='batch',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='teaching_assignments', to='courses.batch'),
        ),
        migrations.AddConstraint(
            model_name='teachingassignment',
            constraint=models.UniqueConstraint(condition=models.Q(('batch__isnull', True), ('is_active', True)), fields=('subject', 'teacher'), name='uniq_active_teacher_per_subject_courselevel'),
        ),
        migrations.AddConstraint(
            model_name='teachingassignment',
            constraint=models.UniqueConstraint(condition=models.Q(('batch__isnull', True), ('is_active', True), ('role', 'PRIMARY')), fields=('subject',), name='uniq_active_primary_per_subject_courselevel'),
        ),
    ]
