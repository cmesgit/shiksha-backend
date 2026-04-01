from django.urls import path
from .views import (
    ChapterMaterials, 
    UploadStudyMaterial, 
    DeleteStudyMaterial, 
    SubjectMaterials, 
    StudentSubjectMaterials,
    StudyMaterialDetail,
    UploadMaterialFile
)
urlpatterns = [


    path(
        "subjects/<uuid:subject_id>/materials/",
        SubjectMaterials.as_view()
    ),

    path(
        "chapters/<uuid:chapter_id>/materials/",
        ChapterMaterials.as_view(),
    ),

    path(
        "chapters/<uuid:chapter_id>/materials/upload/",
        UploadStudyMaterial.as_view(),
    ),

    # ✅ GET → material detail
    path(
        "materials/<uuid:material_id>/",
        StudyMaterialDetail.as_view(),
    ),

    # ✅ DELETE → separate endpoint
    path(
        "materials/<uuid:material_id>/delete/",
        DeleteStudyMaterial.as_view(),
    ),
    path(
        "student/subjects/<uuid:subject_id>/materials/",
        StudentSubjectMaterials.as_view(),
    ),
    path("upload-file/", UploadMaterialFile.as_view()),

]
