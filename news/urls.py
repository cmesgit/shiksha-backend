from django.urls import path
from . import views

urlpatterns = [
    path("top-headlines/", views.top_headlines, name="top_headlines"),
    path("education/", views.education_news, name="education_news"),
]
