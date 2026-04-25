import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0010_course_subscription_duration_days'),
        ('enrollments', '0003_enrollmentrequest'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Subscription',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('starts_at', models.DateTimeField()),
                ('expires_at', models.DateTimeField()),
                ('status', models.CharField(
                    choices=[('ACTIVE', 'Active'), ('EXPIRED', 'Expired'), ('CANCELLED', 'Cancelled')],
                    default='ACTIVE',
                    max_length=20,
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('course', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='subscriptions',
                    to='courses.course',
                )),
                ('source_request', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='subscriptions',
                    to='enrollments.enrollmentrequest',
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='subscriptions',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-expires_at'],
                'indexes': [
                    models.Index(fields=['user', 'course', 'status'], name='enrollments_sub_uc_status_idx'),
                    models.Index(fields=['expires_at'], name='enrollments_sub_expires_idx'),
                ],
            },
        ),
    ]
