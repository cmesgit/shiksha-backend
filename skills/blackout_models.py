# skills/blackout_models.py
#
# Additive model (imported into skills/models.py like course_models.py etc).
# A per-date exception on top of ExpertProfile's weekly-recurring
# availability_slots grid — "Family trip 12-16 Aug" suppresses every slot in
# that range regardless of what the weekly template says (README/WORKFLOW.md
# §6). The weekly grid alone has no way to exclude a single occurrence
# without also removing that weekday/slot forever, hence a separate table.
import uuid

from django.db import models


class ExpertBlackoutDate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    expert = models.ForeignKey(
        "skills.ExpertProfile", on_delete=models.CASCADE, related_name="blackouts"
    )
    date_from = models.DateField()
    date_to = models.DateField()
    label = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date_from"]

    def covers(self, d):
        return self.date_from <= d <= self.date_to

    def __str__(self):
        return f"{self.expert} · {self.date_from}–{self.date_to} ({self.label})"
