from django.urls import path
from .views import (
    ChapterMaterials, 
    UploadStudyMaterial, 
    DeleteStudyMaterial, 
    SubjectMaterials, 
    StudentSubjectMaterials,
    StudentCourseMaterials,
    TeacherAllMaterials,
    StudyMaterialDetail,
    UploadTempFile
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

    path("materials/upload/", UploadStudyMaterial.as_view()),

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
    # Course-wide: the learner's flat Study Material list (one request instead
    # of one per subject).
    path(
        "student/courses/<uuid:course_id>/materials/",
        StudentCourseMaterials.as_view(),
    ),
    # Flat: every material across the subjects this teacher is assigned to.
    path(
        "teacher/materials/all/",
        TeacherAllMaterials.as_view(),
    ),
    path("files/upload/", UploadTempFile.as_view()),

]
