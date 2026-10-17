from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from config.upload_validation import validate_upload
from .taxonomy import normalise_subject_topic


class ManuscriptSerializer(serializers.Serializer):
    file = serializers.FileField()
    response = serializers.CharField(max_length=10000, required=False, allow_blank=True, default="")
    permission_confirmed = serializers.BooleanField()

    def validate_permission_confirmed(self, value):
        if not value:
            raise serializers.ValidationError("Confirm that you have permission to submit this paper.")
        return value

    def validate_file(self, value):
        try:
            validate_upload(value, {".pdf"}, max_mb=50)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message)
        return value


class SubmissionSerializer(ManuscriptSerializer):
    title = serializers.CharField(max_length=300)
    abstract = serializers.CharField(max_length=15000)
    authors = serializers.CharField(max_length=1500)
    institution = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    subject = serializers.CharField(max_length=120)
    topic = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    original_url = serializers.URLField(max_length=1000, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        subject, topic = normalise_subject_topic(attrs["subject"], attrs["topic"])
        if subject.casefold() == "all" or topic.casefold() == "all":
            raise serializers.ValidationError("Enter an actual subject and subcategory.")
        attrs.update(subject=subject, topic=topic)
        return attrs


class ReviewerApplicationSerializer(serializers.Serializer):
    expertise = serializers.CharField(max_length=1000)
    subjects = serializers.ListField(child=serializers.CharField(max_length=120), min_length=1, max_length=20)
    topics = serializers.ListField(child=serializers.CharField(max_length=120), max_length=40, required=False, default=list)

    def validate_subjects(self, values):
        if any(v.casefold() == "all" for v in values):
            raise serializers.ValidationError("List your specific subjects.")
        return list(dict.fromkeys(normalise_subject_topic(v, "")[0] for v in values))


class ActionSerializer(serializers.Serializer):
    version = serializers.IntegerField(min_value=1)
    action = serializers.ChoiceField(choices=["assign", "accept", "decline", "review", "changes", "reject", "publish", "withdraw"])
    note = serializers.CharField(max_length=15000, required=False, allow_blank=True, default="")
    reviewer_id = serializers.UUIDField(required=False)
    days = serializers.IntegerField(required=False, default=7, min_value=1, max_value=30)
    checks = serializers.DictField(child=serializers.ChoiceField(choices=["checked", "issues", "not_applicable"]), required=False)
    decision = serializers.ChoiceField(choices=["approve", "changes", "reject"], required=False)
    expertise_confirmed = serializers.BooleanField(required=False, default=False)
    no_conflict = serializers.BooleanField(required=False, default=False)
    confidential = serializers.BooleanField(required=False, default=False)
