import uuid
from django.db import models
from django.conf import settings
from courses.models_chapter_tags import (
    chapter_note_field,
    no_specific_chapter_field,
)


class PendingVideoUpload(models.Model):
    """Tracks who asked Bunny to create a video slot, so the signed-upload
    step can verify the caller actually owns that video_id before handing
    out a valid TUS upload ticket for it.

    CreateVideoSlotView creates the Bunny video and returns a bare video_id
    with NOTHING persisted about who asked for it — SignedUploadUrlView then
    had no record to check the caller against, so it would sign a ticket for
    ANY client-supplied video_id, letting any teacher-context account
    (including an unreviewed skill expert) overwrite another teacher's
    recording, since bunny_video_id is already serialized back to teachers
    elsewhere in this same app. Consumed (deleted) once the recording is
    actually saved; a stray row surviving an abandoned upload costs nothing
    beyond one small DB row.
    """
    video_id = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
    )
    created_at = models.DateTimeField(auto_now_add=True)


class SessionRecording(models.Model):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    subject = models.ForeignKey(
        "courses.Subject",
        on_delete=models.CASCADE,
        related_name="recordings"
    )

    chapter = models.ForeignKey(
        "courses.Chapter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recordings"
    )

    # Delivery scope, inherited from the source LiveSession when the Bunny
    # video lands: the batch that attended the class sees its recording;
    # other batches don't. Admin clears this to share a recording
    # course-wide. NULL also covers legacy rows. SET_NULL: deleting a batch
    # demotes its recordings to course-wide instead of destroying them.
    batch = models.ForeignKey(
        "courses.Batch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recordings",
    )

    # Optional link back to the live class this recording came from. Null for
    # standalone/manual uploads and legacy rows. SET_NULL so deleting a session
    # never destroys its recording. Lets the admin console show a recording in
    # the context of its source session (and future egress automation populate
    # batch/session_date from it).
    live_session = models.ForeignKey(
        "livestream.LiveSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recordings",
    )

    title = models.CharField(max_length=255)

    description = models.TextField(blank=True)

    session_date = models.DateField(null=True, blank=True)

    duration_seconds = models.PositiveIntegerField(null=True, blank=True)

    bunny_video_id = models.CharField(max_length=255)

    STATUS_CHOICES = [
        (0, "Created"),
        (1, "Uploaded"),
        (2, "Processing"),
        (3, "Transcoding"),
        (4, "Finished"),
        (5, "Error"),
    ]

    status = models.IntegerField(choices=STATUS_CHOICES, default=0)

    thumbnail_url = models.URLField(blank=True)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="uploaded_recordings"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    is_published = models.BooleanField(default=True)

    # --- Flexible chapter tagging (courses.models_chapter_tags) ---
    # The rich multi-chapter placement lives in ContentChapterTag, keyed on
    # (content_type, object_id). These two are the scalar companions; see
    # courses/models_chapter_tags.py for what each one means and why
    # no_specific_chapter is not the same state as "no tags".
    chapter_note = chapter_note_field()
    no_specific_chapter = no_specific_chapter_field()

    class Meta:
        ordering = ["-session_date"]

    def __str__(self):
        return self.title


class RecordingNote(models.Model):
    """A viewer's private notes on one recording — same shape as
    livestream.SessionNote / sessions_app.GroupSessionNote (one per
    (recording, user), upserted via update_or_create, never shown to anyone
    but its author). The live session itself already has this via
    SessionNote; this is the same capability for its recording afterward,
    since a teacher revisiting a class's recording has nowhere today to jot
    anything down."""
    recording = models.ForeignKey(
        SessionRecording,
        on_delete=models.CASCADE,
        related_name="notes",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )
    content = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("recording", "user")
        indexes = [models.Index(fields=["recording", "user"])]

    def __str__(self):
        return f"{self.user_id} notes on {self.recording_id}"
