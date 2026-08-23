import uuid
from django.db import models
from django.conf import settings


class VideoProgress(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="video_progress"
    )

    # WHO actually watched. `student` above is the ACCOUNT, and one account
    # holds many learner profiles (siblings on a parent's email), so keying on
    # it alone merged two children into one watch position: whoever watched
    # last moved the other's resume point, and one child finishing a lecture
    # marked it "Watched" for the other. Same account-vs-profile confusion the
    # rosters and attendance carry (audit theme T2).
    #
    # NULL is legitimate, not just legacy: a TEACHER reviewing their own
    # recording has no learner profile at all. Those rows stay account-keyed.
    learner_profile = models.ForeignKey(
        "accounts.LearnerProfile",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="video_progress",
    )

    recording = models.ForeignKey(
        "courses.SessionRecording",
        on_delete=models.CASCADE,
        related_name="progress"
    )

    last_position = models.FloatField(default=0)       # seconds
    completed = models.BooleanField(default=False)
    last_watched_at = models.DateTimeField(auto_now=True)  # ← added

    class Meta:
        # Two PARTIAL constraints rather than one unique_together over all
        # three columns: Postgres treats NULLs as distinct, so a plain
        # ("student", "learner_profile", "recording") tuple would enforce
        # nothing at all for the NULL-profile (teacher) rows.
        constraints = [
            models.UniqueConstraint(
                fields=["learner_profile", "recording"],
                condition=models.Q(learner_profile__isnull=False),
                name="uniq_videoprogress_profile_recording",
            ),
            models.UniqueConstraint(
                fields=["student", "recording"],
                condition=models.Q(learner_profile__isnull=True),
                name="uniq_videoprogress_account_recording",
            ),
        ]

    def __str__(self):
        who = self.learner_profile or self.student
        return f"{who} – {self.recording} @ {self.last_position}s"
