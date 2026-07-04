# PLACEMENT: backend/backend/counseling/migrations/0002_seed.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/counseling/migrations/0002_seed.py
#
# Seeds the three things the app needs on day one:
#   1. the COUNSELOR row in accounts.Role (so has_role("COUNSELOR") and
#      forum COUNSELOR badges work as soon as an admin approves someone)
#   2. the shared specialization vocabulary (matching + directory filters)
#   3. the default career-assessment template from the MVP spec
# Reversible: reverse deletes only what this migration created.

from django.db import migrations

SPECIALIZATIONS = [
    "Computer Science & IT", "Engineering Careers", "Technology",
    "Medicine & Health Sciences", "Commerce & Finance",
    "Business & Management", "Entrepreneurship", "Arts & Humanities",
    "Design & Creative Careers", "Media & Communication",
    "Civil Services & Government Exams", "University Admissions",
    "Study Abroad", "Vocational & Skill Careers",
    "Stream Selection (Class 9–10)", "Career Discovery",
    "Defence & Armed Forces", "Law", "Education & Teaching",
    "Sports & Fitness Careers",
]

DEFAULT_TEMPLATE = {
    "name": "Career Assessment (Default)",
    "sections": [
        {"key": "personal_interests", "title": "Personal Interests", "questions": [
            {"key": "enjoy_doing", "label": "What do you enjoy doing in your free time?", "type": "textarea"},
            {"key": "curious_about", "label": "Which topics make you curious enough to read or watch more about them?", "type": "textarea"},
        ]},
        {"key": "academic_background", "title": "Academic Background", "questions": [
            {"key": "best_subjects", "label": "Which subjects do you score best in?", "type": "text"},
            {"key": "hardest_subjects", "label": "Which subjects feel hardest, and why?", "type": "textarea"},
        ]},
        {"key": "skills", "title": "Skills", "questions": [
            {"key": "self_skills", "label": "Pick the skills that describe you", "type": "multi",
             "options": ["Communication", "Leadership", "Programming", "Creativity", "Design",
                          "Mathematics", "Writing", "Public Speaking", "Problem Solving"]},
            {"key": "skill_proud", "label": "Describe one thing you built, made, or organised that you're proud of.", "type": "textarea"},
        ]},
        {"key": "career_aspirations", "title": "Career Aspirations", "questions": [
            {"key": "dream_role", "label": "If you could have any career, what would it be?", "type": "text"},
            {"key": "why_role", "label": "What attracts you to it?", "type": "textarea"},
            {"key": "role_models", "label": "Any people whose careers you admire?", "type": "text"},
        ]},
        {"key": "strengths", "title": "Strengths", "questions": [
            {"key": "friends_say", "label": "What would your friends or teachers say you're good at?", "type": "textarea"},
        ]},
        {"key": "challenges", "title": "Challenges", "questions": [
            {"key": "worries", "label": "What worries you most about choosing a career?", "type": "textarea"},
            {"key": "constraints", "label": "Any constraints we should know about (location, finances, family expectations)?", "type": "textarea"},
        ]},
    ],
}


def seed(apps, schema_editor):
    Specialization = apps.get_model("counseling", "Specialization")
    for name in SPECIALIZATIONS:
        Specialization.objects.get_or_create(name=name)

    AssessmentTemplate = apps.get_model("counseling", "AssessmentTemplate")
    AssessmentTemplate.objects.get_or_create(
        name=DEFAULT_TEMPLATE["name"],
        defaults={"sections": DEFAULT_TEMPLATE["sections"], "is_default": True},
    )

    try:
        Role = apps.get_model("accounts", "Role")
        Role.objects.get_or_create(name="COUNSELOR")
    except LookupError:
        pass  # accounts.Role missing only in stripped test setups


def unseed(apps, schema_editor):
    Specialization = apps.get_model("counseling", "Specialization")
    Specialization.objects.filter(name__in=SPECIALIZATIONS).delete()
    AssessmentTemplate = apps.get_model("counseling", "AssessmentTemplate")
    AssessmentTemplate.objects.filter(name=DEFAULT_TEMPLATE["name"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("counseling", "0001_initial"),
        ("accounts", "__first__"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
