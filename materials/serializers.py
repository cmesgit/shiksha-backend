from rest_framework import serializers
from .models import StudyMaterial, MaterialFile


class MaterialFileSerializer(serializers.ModelSerializer):

    file_name = serializers.SerializerMethodField()
    file_url = serializers.SerializerMethodField()
    file_size = serializers.SerializerMethodField()

    class Meta:
        model = MaterialFile
        fields = ["id", "file_url", "file_name", "file_size"]

    def get_file_name(self, obj):
        return obj.filename()

    def get_file_url(self, obj):
        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(obj.file.url)
        return obj.file.url

    def get_file_size(self, obj):
        if not obj.file:
            return None

        size = obj.file.size

        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{round(size / 1024, 1)} KB"
        else:
            return f"{round(size / (1024 * 1024), 1)} MB"


class StudyMaterialSerializer(serializers.ModelSerializer):

    files = serializers.SerializerMethodField()
    chapter_title = serializers.SerializerMethodField()

    class Meta:
        model = StudyMaterial
        fields = [
            "id",
            "title",
            "description",
            "created_at",
            "chapter_title",
            "files"
        ]

    def get_files(self, obj):
        request = self.context.get("request")
        return MaterialFileSerializer(
            obj.files.all(),
            many=True,
            context={"request": request}
        ).data

    def get_chapter_title(self, obj):
        if obj.chapter:
            return obj.chapter.title
        return obj.custom_chapter or "No chapter"