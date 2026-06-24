from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_profile_form_fillup_fields"),
    ]

    operations = [
        # ====================================================
        # PROFILE — New personal info fields
        # ====================================================
        migrations.AddField(
            model_name="profile",
            name="first_name",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="profile",
            name="last_name",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="profile",
            name="gender",
            field=models.CharField(
                blank=True,
                choices=[
                    ("male", "Male"),
                    ("female", "Female"),
                    ("other", "Other"),
                    ("prefer_not_to_say", "Prefer not to say"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="profile_photo",
            field=models.ImageField(blank=True, null=True, upload_to="profiles/photos/"),
        ),

        # ====================================================
        # PROFILE — Structured address fields
        # ====================================================
        migrations.AddField(
            model_name="profile",
            name="state",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="profile",
            name="district",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="profile",
            name="city_town",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="profile",
            name="pin_code",
            field=models.CharField(blank=True, max_length=10),
        ),

        # ====================================================
        # PROFILE — Student parent/guardian new fields
        # ====================================================
        migrations.AddField(
            model_name="profile",
            name="mother_phone",
            field=models.CharField(blank=True, max_length=15),
        ),
        migrations.AddField(
            model_name="profile",
            name="guardian_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="profile",
            name="parent_guardian_email",
            field=models.EmailField(blank=True, max_length=254),
        ),

        # ====================================================
        # PROFILE — Student academic info fields
        # ====================================================
        migrations.AddField(
            model_name="profile",
            name="currently_studying",
            field=models.CharField(
                blank=True,
                choices=[("yes", "Yes"), ("no", "No")],
                max_length=3,
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="current_class",
            field=models.CharField(
                blank=True,
                choices=[
                    ("8", "Class 8"),
                    ("9", "Class 9"),
                    ("10", "Class 10"),
                    ("11", "Class 11"),
                    ("12", "Class 12"),
                ],
                max_length=5,
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="stream",
            field=models.CharField(
                blank=True,
                choices=[
                    ("science", "Science"),
                    ("commerce", "Commerce"),
                    ("arts", "Arts"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="board",
            field=models.CharField(
                blank=True,
                choices=[
                    ("cbse", "CBSE"),
                    ("icse", "ICSE"),
                    ("mbse", "Mizoram Board of School Education"),
                    ("nios", "NIOS"),
                    ("other", "Other State Board"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="board_other",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="profile",
            name="school_name",
            field=models.CharField(blank=True, max_length=250),
        ),
        migrations.AddField(
            model_name="profile",
            name="academic_year",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="profile",
            name="highest_education",
            field=models.CharField(
                blank=True,
                choices=[
                    ("below_8", "Below Class 8"),
                    ("8", "Class 8"),
                    ("9", "Class 9"),
                    ("10", "Class 10"),
                    ("11", "Class 11"),
                    ("12", "Class 12"),
                ],
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="reason_not_studying",
            field=models.CharField(blank=True, max_length=200),
        ),

        # ====================================================
        # TEACHER PROFILE — Update gender choices
        # ====================================================
        migrations.AlterField(
            model_name="teacherprofile",
            name="gender",
            field=models.CharField(
                blank=True,
                choices=[
                    ("male", "Male"),
                    ("female", "Female"),
                    ("other", "Other"),
                    ("prefer_not_to_say", "Prefer not to say"),
                ],
                max_length=20,
            ),
        ),

        # ====================================================
        # TEACHER PROFILE — Educational Qualifications (new)
        # ====================================================
        migrations.AddField(
            model_name="teacherprofile",
            name="highest_degree",
            field=models.CharField(
                blank=True,
                choices=[
                    ("10th_pass", "10th Pass"),
                    ("12th_pass", "12th Pass"),
                    ("diploma", "Diploma"),
                    ("bachelors", "Bachelor's Degree"),
                    ("masters", "Master's Degree"),
                    ("phd", "Ph.D."),
                    ("other", "Other"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="field_of_study",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="year_of_completion",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="teaching_certifications",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="qualification_certificate",
            field=models.FileField(blank=True, null=True, upload_to="teachers/certificates/"),
        ),

        # ====================================================
        # TEACHER PROFILE — Teaching Experience (new)
        # ====================================================
        migrations.AddField(
            model_name="teacherprofile",
            name="experience_range",
            field=models.CharField(
                blank=True,
                choices=[
                    ("0", "New Teacher (0 years)"),
                    ("lt1", "Less than 1 year"),
                    ("1_3", "1-3 years"),
                    ("3_5", "3-5 years"),
                    ("5_10", "5-10 years"),
                    ("10plus", "10+ years"),
                ],
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="employment_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("fulltime", "Full-time teacher at school"),
                    ("parttime", "Part-time teacher"),
                    ("private_tutor", "Private tutor"),
                    ("unemployed", "Unemployed/Looking for opportunities"),
                    ("retired", "Retired teacher"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="currently_employed",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="current_institution",
            field=models.CharField(blank=True, max_length=250),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="current_position",
            field=models.CharField(blank=True, max_length=150),
        ),

        # ====================================================
        # TEACHER PROFILE — Verification Documents (new)
        # ====================================================
        migrations.AddField(
            model_name="teacherprofile",
            name="govt_id_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("aadhaar", "Aadhaar Card"),
                    ("pan", "PAN Card"),
                    ("voter_id", "Voter ID"),
                    ("driving_license", "Driving License"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="id_number",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="id_proof_front",
            field=models.FileField(blank=True, null=True, upload_to="teachers/id_proofs/"),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="id_proof_back",
            field=models.FileField(blank=True, null=True, upload_to="teachers/id_proofs/"),
        ),

        # ====================================================
        # TEACHER PROFILE — Course Application fields (new)
        # ====================================================
        migrations.AddField(
            model_name="teacherprofile",
            name="subject",
            field=models.CharField(
                blank=True,
                choices=[
                    ("mathematics", "Mathematics"),
                    ("physics", "Physics"),
                    ("chemistry", "Chemistry"),
                    ("biology", "Biology"),
                    ("english", "English"),
                    ("hindi", "Hindi"),
                    ("social_science", "Social Science"),
                    ("history", "History"),
                    ("geography", "Geography"),
                    ("economics", "Economics"),
                    ("computer_science", "Computer Science"),
                    ("accountancy", "Accountancy"),
                    ("business_studies", "Business Studies"),
                    ("political_science", "Political Science"),
                    ("other", "Other"),
                ],
                max_length=50,
            ),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="boards",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="classes",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="streams",
            field=models.JSONField(blank=True, default=list),
        ),

        # ====================================================
        # TEACHER PROFILE — Skill Application fields (new)
        # ====================================================
        migrations.AddField(
            model_name="teacherprofile",
            name="skill_name",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="skill_description",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="skill_related_subject",
            field=models.CharField(
                blank=True,
                choices=[
                    ("mathematics", "Mathematics"),
                    ("physics", "Physics"),
                    ("chemistry", "Chemistry"),
                    ("biology", "Biology"),
                    ("english", "English"),
                    ("hindi", "Hindi"),
                    ("social_science", "Social Science"),
                    ("history", "History"),
                    ("geography", "Geography"),
                    ("economics", "Economics"),
                    ("computer_science", "Computer Science"),
                    ("accountancy", "Accountancy"),
                    ("business_studies", "Business Studies"),
                    ("political_science", "Political Science"),
                    ("other", "Other"),
                ],
                max_length=50,
            ),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="skill_supporting_image",
            field=models.ImageField(blank=True, null=True, upload_to="teachers/skills/images/"),
        ),
        migrations.AddField(
            model_name="teacherprofile",
            name="skill_supporting_video",
            field=models.FileField(blank=True, null=True, upload_to="teachers/skills/videos/"),
        ),
    ]
