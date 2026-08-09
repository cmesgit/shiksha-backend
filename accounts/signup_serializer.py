"""
accounts/signup_serializer.py  ·  REFACTORED — single-password model

One email = one container (User). That container holds:
  - Up to 5 LearnerProfiles  (learner side: the holder + dependents)
  - One TeacherProfile        (teacher side: always also has a SELF learner)

PASSWORD MODEL:
  ONE password. The ACCOUNT password (User.password) is the only password.
  - It authenticates the learner login.
  - It authenticates entering teacher context (TeacherContextView).
  - There is NO separate teacher_password field on TeacherProfile.

Signup cases:

  Case 1  NEW email + Student
          → create User(account password) + LearnerProfile(s) + STUDENT role

  Case 2  NEW email + Teacher
          → create User(account password), create TeacherProfile,
            TEACHER role (inactive until approved), and a SELF LearnerProfile
            so they can also learn.

  Case 3  EXISTING (has_teacher, no student) + Student signup
          → verify ACCOUNT password (ownership proof — same password they
            already use to log in), add LearnerProfile(s) + STUDENT role.

  Case 4  EXISTING (has_student, no teacher) + Teacher signup
          → verify ACCOUNT password (ownership proof), add TeacherProfile
            + TEACHER role (reuses existing SELF learner).

  Case 5  EXISTING (has_student) + Student signup  → BLOCK
  Case 6  EXISTING teacher + signup for the OTHER track → ADD TRACK (asymmetric)
          A Guest-expert (Skill) teacher may apply for the Faculty (Academy)
          track; the new track is added to the SAME TeacherProfile:
            · adding Academy (faculty) → pending admin review
          The Skill track they already hold keeps working the whole time.
          The REVERSE is NOT allowed: a Faculty (Academy) teacher may NOT add
          the Skill/Guest track — faculty stay faculty-only (see
          TeacherProfile.can_apply_track / track_add_block_reason).
  Case 7  EXISTING teacher + signup for a track they already hold → BLOCK
  Case 8  EXISTING Faculty teacher + signup for Skill/Guest → BLOCK (asymmetry)
"""
import base64
import binascii

from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth import authenticate
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from .models import User, LearnerProfile, Role, UserRole, TeacherProfile


def _generate_unique_username(email):
    """Derive a unique username from the email local-part, adding a numeric
    suffix on collision. Used when signup doesn't supply one."""
    import re
    base = re.sub(r"[^a-zA-Z0-9_.+-]", "", (email or "").split("@")[0]) or "user"
    base = base[:140]  # leave room for a suffix (max_length 150)
    candidate = base
    n = 1
    while User.objects.filter(username__iexact=candidate).exists():
        n += 1
        candidate = f"{base}{n}"
    return candidate


class SignupProfileSerializer(serializers.Serializer):
    display_name = serializers.CharField(max_length=100)
    relationship = serializers.ChoiceField(
        choices=[LearnerProfile.RELATIONSHIP_SELF, LearnerProfile.RELATIONSHIP_DEPENDENT],
        required=False,
    )
    # Optional 4–6 digit PIN to lock this profile. Blank / omitted = no PIN.
    pin = serializers.CharField(max_length=6, required=False, allow_blank=True)


class SignupSerializer(serializers.Serializer):
    email    = serializers.EmailField()
    username = serializers.CharField(max_length=150, required=False, allow_blank=True)
    role     = serializers.ChoiceField(choices=[Role.STUDENT, Role.TEACHER])

    # THE ONE password — used for new accounts and as ownership proof for
    # add-to-existing flows.
    password = serializers.CharField(write_only=True)

    # NOTE: teacher_password field REMOVED — single password model.

    # Student-only: learner profiles to create under this account.
    profiles = SignupProfileSerializer(many=True, required=False)

    # Teacher-only: which track they're applying through.
    teacher_type = serializers.ChoiceField(
        choices=[TeacherProfile.TYPE_GUEST, TeacherProfile.TYPE_FACULTY],
        required=False,
    )

    # Guest-expert only: optional profile data captured during signup so the
    # expert's directory listing can be pre-filled (full_name, date_of_birth,
    # phone, subject_description/category, languages, bio, hourly_rate,
    # class_mode/class_location). Anything omitted is completed later on the
    # dashboard — the profile stays UNLISTED and the dashboard forces the
    # profile screen until every required field is present
    # (see ExpertProfile.refresh_listing / completeness).
    expert_profile = serializers.JSONField(required=False)

    # Faculty (Academy) only: optional teaching-background data captured during
    # signup so admins reviewing the application have something to assess. These
    # map onto EXISTING TeacherProfile columns (the same ones the full
    # /form-fillup application writes) — no migration. Personal details and
    # verification documents are finished later from the dashboard. Anything
    # malformed is dropped, never raised (see _provision_faculty).
    faculty_profile = serializers.JSONField(required=False)

    # ── internal flags set during validate() ──────────────────────────────
    # _mode: "create" | "add_student_to_teacher" | "add_teacher_to_student"
    # _existing_user: the User for add-to-existing modes

    def _account_state(self, email):
        try:
            user = User.objects.get(email__iexact=email)
            has_student = user.has_role(Role.STUDENT)
            has_teacher = hasattr(user, "teacher_profile")
            return user, has_student, has_teacher
        except User.DoesNotExist:
            return None, False, False

    # ── field validators ──────────────────────────────────────────────────

    def validate_email(self, value):
        return value.strip().lower()

    def validate_username(self, value):
        if value and User.objects.filter(username__iexact=value).exists():
            raise ValidationError("Username is already taken.")
        return value

    # ── cross-field validation (the core logic) ───────────────────────────

    def validate(self, data):
        email    = data["email"]
        role     = data["role"]
        password = data["password"]

        existing_user, has_student, has_teacher = self._account_state(email)

        if existing_user:
            if role == Role.STUDENT:
                if has_student:
                    raise ValidationError(
                        "This email already has learner profiles. Log in to manage them."
                    )
                # has_teacher only → add learner profiles.
                # Ownership proof = account password (same one they log in with).
                authed = authenticate(email=email, password=password)
                if not authed:
                    raise ValidationError(
                        {"password": "Incorrect password for this account."}
                    )
                data["_mode"]          = "add_student_to_teacher"
                data["_existing_user"] = existing_user

            elif role == Role.TEACHER:
                if not data.get("teacher_type"):
                    raise ValidationError(
                        {"teacher_type": "Choose Guest expert (skill) or Faculty (academy)."}
                    )
                target_track = TeacherProfile.track_for_type(data["teacher_type"])

                if has_teacher:
                    tp = existing_user.teacher_profile
                    # Enforce the asymmetric Faculty/Guest rule via the single
                    # source of truth on the model. This rejects both an
                    # already-held track AND a Faculty account trying to add
                    # Skill. (Guest adding Faculty stays allowed.)
                    if not tp.can_apply_track(target_track):
                        raise ValidationError(tp.track_add_block_reason(target_track))
                    # Otherwise they're adding a track they're allowed to add.
                    authed = authenticate(email=email, password=password)
                    if not authed:
                        raise ValidationError(
                            {"password": "Incorrect password for this account."}
                        )
                    data["_mode"]          = "add_teacher_track"
                    data["_existing_user"] = existing_user
                    data["_target_track"]  = target_track
                    return data

                # has_student only → add a brand-new teacher identity (one track).
                # Ownership proof = account password.
                authed = authenticate(email=email, password=password)
                if not authed:
                    raise ValidationError(
                        {"password": "Incorrect password for this account."}
                    )
                data["_mode"]          = "add_teacher_to_student"
                data["_existing_user"] = existing_user

        else:
            # ── brand new account ─────────────────────────────────────────
            data["_mode"] = "create"

            # Username is optional. If blank, we auto-generate a unique one in
            # create() from the email local-part. (Login is by email; the
            # username is just a unique internal handle — users shouldn't have
            # to invent one, and a profile/display name must NOT double as it.)

            try:
                validate_password(password)
            except Exception as e:
                raise ValidationError({"password": list(e.messages)})

            if role == Role.TEACHER:
                if not data.get("teacher_type"):
                    raise ValidationError({"teacher_type": "Choose Guest expert or Faculty."})

        # ── STUDENT identity guards (new + add-to-existing) ──────────────
        if role == Role.STUDENT:
            submitted = data.get("profiles", []) or []
            for i, p in enumerate(submitted):
                if not (p.get("display_name") or "").strip():
                    raise ValidationError(
                        {"profiles": f"Profile {i + 1}: a name is required."}
                    )
                pin = (p.get("pin") or "").strip()
                if pin and (not pin.isdigit() or not (4 <= len(pin) <= 6)):
                    raise ValidationError(
                        {"profiles": f"Profile {i + 1}: PIN must be 4-6 digits."}
                    )
            existing_count = (
                existing_user.learner_profiles.filter(is_active=True).count()
                if existing_user else 0
            )
            to_add = len(submitted) if submitted else 1
            if existing_count + to_add > 5:
                remaining = max(0, 5 - existing_count)
                raise ValidationError({
                    "profiles": (
                        "An account can hold at most 5 learner profiles. "
                        + (f"You can add {remaining} more."
                           if remaining else "This account is already at the limit.")
                    )
                })

        return data

    # ── creation ──────────────────────────────────────────────────────────

    @transaction.atomic
    def create(self, validated_data):
        mode          = validated_data.get("_mode", "create")
        existing_user = validated_data.get("_existing_user")
        role          = validated_data["role"]
        password      = validated_data["password"]

        if mode == "create":
            supplied = (validated_data.get("username") or "").strip()
            username = supplied or _generate_unique_username(validated_data["email"])
            user = User.objects.create_user(
                email    = validated_data["email"],
                username = username,
                password = password,
            )
            user.is_verified = False
            user.save(update_fields=["is_verified"])
        else:
            user = existing_user

        if role == Role.TEACHER:
            if mode == "add_teacher_track":
                self._add_teacher_track(
                    user, validated_data["_target_track"],
                    validated_data.get("expert_profile"),
                    validated_data.get("faculty_profile"),
                )
            else:
                self._setup_teacher(
                    user, validated_data["teacher_type"],
                    validated_data.get("expert_profile"),
                    validated_data.get("faculty_profile"),
                )
        else:
            self._setup_student(user, validated_data.get("profiles", []))

        return user

    # ── identity setup helpers ────────────────────────────────────────────

    def _setup_student(self, user, profiles):
        existing_count = user.learner_profiles.filter(is_active=True).count()
        has_self = user.learner_profiles.filter(
            relationship=LearnerProfile.RELATIONSHIP_SELF, is_active=True
        ).exists()

        entries = profiles or [{
            "display_name": user.username,
            "relationship": LearnerProfile.RELATIONSHIP_SELF,
        }]

        for i, entry in enumerate(entries):
            rel = entry.get("relationship")
            if not rel:
                rel = (
                    LearnerProfile.RELATIONSHIP_SELF
                    if (i == 0 and not has_self)
                    else LearnerProfile.RELATIONSHIP_DEPENDENT
                )
            if rel == LearnerProfile.RELATIONSHIP_SELF and has_self:
                rel = LearnerProfile.RELATIONSHIP_DEPENDENT
            if rel == LearnerProfile.RELATIONSHIP_SELF:
                has_self = True

            lp = LearnerProfile(
                account      = user,
                display_name = entry["display_name"].strip(),
                relationship = rel,
                is_default   = (i == 0 and existing_count == 0),
            )
            pin = (entry.get("pin") or "").strip()
            if pin:
                lp.set_pin(pin)
            lp.save()

        student_role, _ = Role.objects.get_or_create(name=Role.STUDENT)
        UserRole.objects.get_or_create(
            user = user,
            role = student_role,
            defaults={
                "is_active":  True,
                "is_primary": not user.user_roles.filter(is_primary=True).exists(),
            },
        )

    # ── track approval policy ──────────────────────────────────────────────
    # Skill (Guest expert) is auto-listed the moment they apply; Academy
    # (Faculty) waits in the admin review queue. One place decides this so the
    # new-teacher and add-track paths can never drift apart.
    def _initial_status_for(self, track):
        return (
            TeacherProfile.TRACK_APPROVED
            if track == TeacherProfile.TRACK_SKILL
            else TeacherProfile.TRACK_PENDING
        )

    def _ensure_teacher_role(self, user, *, active):
        """Create/refresh the TEACHER UserRole. `active` mirrors whether the
        teacher already has a live (approved) track — an active role row is
        what lets them enter teacher mode; a pending faculty applicant stays
        inactive so they surface in the admin approval queue."""
        teacher_role, _ = Role.objects.get_or_create(name=Role.TEACHER)
        role, created = UserRole.objects.get_or_create(
            user=user,
            role=teacher_role,
            defaults={
                "is_active":  active,
                "is_primary": not user.user_roles.filter(is_primary=True).exists(),
                "approved_at": timezone.now() if active else None,
            },
        )
        if not created and active and not role.is_active:
            role.is_active   = True
            role.approved_at = role.approved_at or timezone.now()
            role.save(update_fields=["is_active", "approved_at"])
        return role

    def _ensure_self_learner(self, user):
        """A teacher account always carries a SELF learner profile so the
        person can also learn / switch to the student side."""
        if not user.learner_profiles.filter(
            relationship=LearnerProfile.RELATIONSHIP_SELF, is_active=True
        ).exists():
            LearnerProfile.objects.create(
                account      = user,
                display_name = user.username or user.email.split("@")[0],
                relationship = LearnerProfile.RELATIONSHIP_SELF,
                is_default   = not user.learner_profiles.filter(is_active=True).exists(),
            )

    def _setup_teacher(self, user, teacher_type, expert_payload=None, faculty_payload=None):
        """Brand-new teacher identity, applying through a single track."""
        track  = TeacherProfile.track_for_type(teacher_type)
        status = self._initial_status_for(track)

        tp = TeacherProfile(user=user, teacher_type=teacher_type)
        tp.set_track_status(track, status)
        tp.sync_type_from_tracks()   # sets teacher_type + is_approved coherently
        tp.save()

        self._ensure_teacher_role(user, active=bool(tp.approved_tracks()))
        self._ensure_self_learner(user)

        # Guest expert → every skill teacher gets an ExpertProfile so the
        # dashboard + profile editor work immediately (no PermissionDenied),
        # pre-filled with anything captured at signup. It stays UNLISTED until
        # the profile is complete.
        if track == TeacherProfile.TRACK_SKILL:
            self._provision_expert(tp, expert_payload)
        # Faculty → store any teaching background captured at signup so it's
        # waiting in the admin review queue with the application.
        elif track == TeacherProfile.TRACK_ACADEMY:
            self._provision_faculty(tp, faculty_payload)

    def _add_teacher_track(self, user, track, expert_payload=None, faculty_payload=None):
        """Existing teacher applying for the track they don't yet hold.
        The track they already have keeps working untouched."""
        tp = user.teacher_profile
        # Defense in depth: never add a track the policy forbids, even if a
        # caller reached here directly. validate() is the primary gate.
        if not tp.can_apply_track(track):
            raise ValidationError(tp.track_add_block_reason(track))
        tp.set_track_status(track, self._initial_status_for(track))
        tp.sync_type_from_tracks()
        fields = ["academy_status", "skill_status", "teacher_type", "is_approved"]
        # Re-applying after a rejection must clear the old verdict, otherwise
        # academy_rejection_reason/academy_rejected_at survive onto the fresh
        # PENDING application and the profile picker keeps showing the previous
        # rejection reason for an application nobody has looked at yet.
        if track == TeacherProfile.TRACK_ACADEMY:
            tp.academy_rejection_reason = ""
            tp.academy_rejected_at = None
            fields += ["academy_rejection_reason", "academy_rejected_at"]
        tp.save(update_fields=fields)

        # If the newly added track is live (skill), make sure the role is
        # active so they can enter that dashboard right away.
        self._ensure_teacher_role(user, active=bool(tp.approved_tracks()))

        if track == TeacherProfile.TRACK_SKILL:
            self._provision_expert(tp, expert_payload)
        elif track == TeacherProfile.TRACK_ACADEMY:
            self._provision_faculty(tp, faculty_payload)

    # ── Expert-profile provisioning (guest track) ──────────────────────────
    def _provision_expert(self, teacher_profile, payload):
        """Create the ExpertProfile for a guest teacher (idempotent) and apply
        any profile data captured at signup. Runs through the SAME helpers the
        dashboard editor uses, so signup and edit can never diverge.

        ``payload`` may carry both expert fields (subject_description, category,
        languages, bio, hourly_rate, class_mode, class_location) and the SELF
        learner's personal fields (full_name, date_of_birth, phone)."""
        from skills.models import ExpertProfile
        from skills import profile_ops as ops

        ep, _ = ExpertProfile.objects.get_or_create(teacher_profile=teacher_profile)

        payload = payload if isinstance(payload, dict) else None
        if payload:
            try:
                ep_fields = ops.apply_expert_fields(ep, payload)
                ops.validate_location(ep)
            except ValidationError:
                # Never block account creation on optional signup-time profile
                # data — the dashboard gate will require it properly. Persist
                # whatever cleanly applied and move on.
                ep_fields = []
            if ep_fields:
                ep.save()

            learner = teacher_profile.user.default_learner_profile()
            if learner:
                try:
                    p_fields = ops.apply_personal_fields(learner, payload)
                except ValidationError:
                    p_fields = []
                if p_fields:
                    learner.save()

        # List now if (and only if) everything required is already present.
        ep.refresh_listing()
        return ep

    # ── Faculty-application provisioning (academy track) ───────────────────
    # Documents a faculty applicant may attach on signup step 2. Deliberately
    # narrow: this decodes user-supplied base64 on an AnonymousUser request, so
    # only the three types the form's file picker advertises are accepted.
    _SIGNUP_DOC_TYPES = {
        "application/pdf": ".pdf",
        "image/jpeg":      ".jpg",
        "image/jpg":       ".jpg",
        "image/png":       ".png",
    }
    # Matches MAX_DOC_MB in FacultySignup.jsx — the client already refuses
    # anything larger, this is the server-side half of the same limit.
    _SIGNUP_DOC_MAX_BYTES = 5 * 1024 * 1024

    @classmethod
    def _save_signup_document(cls, tp, field_name, doc):
        """Decode one {name, type, data} base64 document onto tp.<field_name>.

        Returns `field_name` when a file was attached (caller batches the
        save), or None. Best-effort like the rest of _provision_faculty:
        anything malformed, oversized or of an unexpected type is dropped
        rather than raised, so a bad attachment can never cost someone their
        account.
        """
        if not isinstance(doc, dict):
            return None
        ext = cls._SIGNUP_DOC_TYPES.get((doc.get("type") or "").strip().lower())
        if not ext:
            return None
        raw = doc.get("data")
        if not isinstance(raw, str) or not raw.strip():
            return None
        raw = raw.strip()
        # FileReader.readAsDataURL yields "data:<mime>;base64,<payload>"; the
        # form sends that string verbatim, so strip the prefix if present.
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            blob = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return None
        if not blob or len(blob) > cls._SIGNUP_DOC_MAX_BYTES:
            return None
        # Name by profile + field so two applicants' certificates never collide
        # and an admin can trace a file back to its TeacherProfile.
        getattr(tp, field_name).save(
            f"{tp.pk}_{field_name}{ext}", ContentFile(blob), save=False
        )
        return field_name

    def _provision_faculty(self, teacher_profile, payload):
        """Apply optional faculty-application background captured at signup to
        the EXISTING TeacherProfile columns (and one TeacherCourseApplication).
        These are the same fields the full /form-fillup application writes, so
        there's no new model and no migration — signup just pre-fills them.

        Mirrors _provision_expert's contract: this is BEST-EFFORT and must NEVER
        block account creation. A missing/malformed value is dropped, not
        raised. Personal details and verification documents are completed later
        from the dashboard (they need file uploads + a verified session).
        """
        if not isinstance(payload, dict):
            return
        tp = teacher_profile
        try:
            from .models import TeacherCourseApplication

            def _valid_choices(model, field_name):
                f = model._meta.get_field(field_name)
                return {c[0] for c in (f.choices or [])}

            changed = []

            # CharField choices — only store a value the model actually allows.
            for key in ("highest_degree", "experience_range", "employment_status", "govt_id_type"):
                val = payload.get(key)
                val = val.strip() if isinstance(val, str) else ""
                if val and val in _valid_choices(TeacherProfile, key):
                    setattr(tp, key, val)
                    changed.append(key)

            # Free-text scalars (truncated to the column width to be safe).
            for key, maxlen in (("field_of_study", 200),
                                ("current_institution", 250),
                                ("current_position", 150),
                                ("id_number", 50)):
                val = payload.get(key)
                if isinstance(val, str) and val.strip():
                    setattr(tp, key, val.strip()[:maxlen])
                    changed.append(key)

            yr = payload.get("year_of_completion")
            if yr not in (None, ""):
                try:
                    yr = int(yr)
                    if 1950 <= yr <= 2100:
                        tp.year_of_completion = yr
                        changed.append("year_of_completion")
                except (TypeError, ValueError):
                    pass

            if "currently_employed" in payload:
                tp.currently_employed = bool(payload.get("currently_employed"))
                changed.append("currently_employed")

            certs = payload.get("teaching_certifications")
            if isinstance(certs, list):
                tp.teaching_certifications = [
                    str(c).strip() for c in certs if str(c).strip()
                ][:20]
                changed.append("teaching_certifications")

            # --- Verification documents (base64 from signup step 2) ---
            # These three keys used to be dropped on the floor: nothing here
            # read them and nothing in accounts/ decoded base64 at all, so an
            # applicant could upload three files, be told it "speeds up
            # review", and have the bytes silently discarded — while the admin
            # was asked to approve them on the strength of those documents.
            for key in ("qualification_certificate", "id_proof_front", "id_proof_back"):
                if self._save_signup_document(tp, key, payload.get(key)):
                    changed.append(key)

            if changed:
                tp.save(update_fields=sorted(set(changed)))

            # One subject application so the review queue shows what they intend
            # to teach. The dashboard form lets them add more subjects later.
            ca = payload.get("course_application")
            if isinstance(ca, dict):
                subj = ca.get("subject")
                subj = subj.strip() if isinstance(subj, str) else ""
                if subj and subj in _valid_choices(TeacherCourseApplication, "subject"):
                    valid_boards  = {"cbse", "icse", "mbse", "nios", "other"}
                    valid_classes = {"1_5", "6_8", "9_10", "11_12", "ug", "pg"}
                    valid_streams = {"science", "commerce", "arts", "vocational", "general"}
                    boards  = [b for b in (ca.get("boards") or []) if b in valid_boards]
                    classes = [str(c) for c in (ca.get("classes") or []) if str(c) in valid_classes]
                    streams = [str(x) for x in (ca.get("streams") or []) if str(x) in valid_streams]
                    TeacherCourseApplication.objects.create(
                        teacher_profile=tp, subject=subj, boards=boards,
                        classes=classes, streams=streams,
                    )
        except Exception:
            # Best-effort only — never block signup on optional profile data.
            return
