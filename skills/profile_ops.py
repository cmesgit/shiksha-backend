"""
skills/profile_ops.py  ·  Guest-expert profile: single source of truth
─────────────────────────────────────────────────────────────────────────

The product spec lists exactly what a Skill-Dev guest expert must provide to
create their teaching profile:

    email + password (handled by accounts signup)
    full name, dob, phone number, profile picture        → SELF LearnerProfile
    subject selection with description                   → category + subject_description
    language preference                                  → languages
    about-you description                                → bio
    location of class (home / travel / online)           → class_mode (+ class_location)
    hourly fee                                           → hourly_rate

Those same fields are editable later from the dashboard. To guarantee the
signup flow and the dashboard editor can NEVER drift, both go through the
helpers here:

  • ``expert_missing`` / ``personal_missing``  — pure completeness checks
    (no Django needed, so they are trivially unit-testable).
  • ``apply_expert_fields`` / ``apply_personal_fields`` — write submitted
    values onto an ExpertProfile / LearnerProfile, with validation.
  • ``serialize_expert`` — the dict every profile endpoint returns, including
    the live ``is_complete`` + ``missing`` the dashboard gate reads.

WHY completeness matters operationally: the public directory only shows
experts with ``is_listed=True`` (skills/views.ExpertListView). We keep a
half-finished signup OFF the directory and force the profile screen until the
required fields are filled — see ExpertProfile.refresh_listing(), which flips
``is_listed`` on once ``is_complete`` becomes true.
"""

# ── Field keys (stable identifiers the frontend maps to human labels) ────────
EXPERT_REQUIRED = (
    "subject_description",   # subject selection + description
    "languages",             # language preference
    "bio",                   # about you
    "class_mode",            # location of class (mode)
    # "class_location" is required ONLY when class_mode is home/travel.
    # NOTE: "hourly_rate" is intentionally NOT required. Booking is free at
    # launch (toggled globally from admin via GlobalSettings), so an expert
    # must be able to get listed without setting a fee. The field still exists
    # on the model (dormant, defaults 0) for when paid mode is built later.
)
PERSONAL_REQUIRED = ("full_name", "date_of_birth", "phone", "profile_photo")

VALID_MODES = ("home", "travel", "online")
OFFLINE_MODES = ("home", "travel")


# ── Pure completeness checks (Django-free; safe to unit-test in isolation) ───
def expert_missing(
    *,
    category_id=None,
    has_subjects=False,
    subject_description="",
    languages=None,
    bio="",
    hourly_rate=0,
    class_mode="",
    class_location="",
):
    """Return the list of missing ExpertProfile-side required fields.

    `subject_description` is satisfied by EITHER a chosen category OR free-text
    description, so an expert who picks a category isn't forced to also type a
    description (and vice-versa)."""
    missing = []

    if not (category_id or has_subjects or (subject_description or "").strip()):
        missing.append("subject_description")

    if not (languages or []):
        missing.append("languages")

    if not (bio or "").strip():
        missing.append("bio")

    # hourly_rate is no longer part of completeness — booking is free at launch.

    if class_mode not in VALID_MODES:
        missing.append("class_mode")
    elif class_mode in OFFLINE_MODES and not (class_location or "").strip():
        missing.append("class_location")

    return missing


def personal_missing(
    *,
    full_name="",
    first_name="",
    last_name="",
    date_of_birth=None,
    phone="",
    profile_photo=None,
):
    """Return the list of missing SELF-learner personal fields. A full name is
    satisfied by either ``full_name`` or both ``first_name`` + ``last_name``."""
    missing = []

    name_ok = bool(
        (full_name or "").strip()
        or ((first_name or "").strip() and (last_name or "").strip())
    )
    if not name_ok:
        missing.append("full_name")
    if not date_of_birth:
        missing.append("date_of_birth")
    if not (phone or "").strip():
        missing.append("phone")
    if not profile_photo:
        missing.append("profile_photo")

    return missing


# ── Helpers used by the writers below ────────────────────────────────────────
def _coerce_languages(value):
    """Accept a list or a comma-separated string; return a clean list[str]
    (each trimmed, <=40 chars, max 10 entries)."""
    if value is None:
        return None
    if isinstance(value, str):
        value = [s.strip() for s in value.split(",") if s.strip()]
    if not isinstance(value, (list, tuple)):
        from rest_framework.exceptions import ValidationError
        raise ValidationError({"languages": "Must be a list of languages."})
    return [str(x).strip()[:40] for x in value if str(x).strip()][:10]


def _resolve_category(value):
    """Resolve a category by UUID id or slug. Returns (category_or_None, found).
    ``found`` is False when a value was supplied but matched nothing — the
    caller decides whether that's an error."""
    if value in (None, ""):
        return None, True
    from .models import SkillCategory
    cat = (
        SkillCategory.objects.filter(slug=str(value)).first()
        or SkillCategory.objects.filter(id=value).first()
        if not _looks_like_uuid(value)
        else SkillCategory.objects.filter(id=value).first()
        or SkillCategory.objects.filter(slug=str(value)).first()
    )
    return cat, bool(cat)


def _looks_like_uuid(value):
    import uuid
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _parse_date(value):
    """Parse an ISO 'YYYY-MM-DD' string (or pass a date through)."""
    if value in (None, ""):
        return None
    import datetime
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        from rest_framework.exceptions import ValidationError
        raise ValidationError({"date_of_birth": "Use the date picker (YYYY-MM-DD)."})


# ── Writers (shared by signup + dashboard PATCH) ─────────────────────────────
def apply_expert_fields(ep, data, *, files=None):
    """Apply submitted ExpertProfile fields onto ``ep`` (not yet saved).

    Returns the list of model field names touched. Raises DRF ValidationError
    on bad input. ``hourly_rate`` is accepted in RUPEES and stored in paise.
    Location is NOT validated here — call ``validate_location`` after, so the
    rule fires whether or not class_mode was part of this request.
    """
    from rest_framework.exceptions import ValidationError
    files = files or {}
    fields = []

    if "category" in data:
        cat, found = _resolve_category(data.get("category"))
        if data.get("category") and not found:
            raise ValidationError({"category": "Unknown subject/category."})
        ep.category = cat
        fields.append("category")

    # Multiple subjects: a list of category slugs/ids. The M2M is set here
    # (the profile row always exists by edit time); `category` is synced to
    # the first entry so older consumers keep working.
    if "categories" in data:
        raw = data.get("categories")
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.split(",") if x.strip()]
        if not isinstance(raw, list):
            raise ValidationError({"categories": "Must be a list of subject slugs."})
        cats = []
        for v in raw:
            cat, found = _resolve_category(v)
            if not found:
                raise ValidationError({"categories": f"Unknown subject: {v}"})
            if cat:
                cats.append(cat)
        ep.categories.set(cats)
        ep.category = cats[0] if cats else ep.category
        fields.append("category")

    if "headline" in data:
        ep.headline = str(data["headline"])[:160]
        fields.append("headline")

    if "skill_tags" in data:
        tags = data["skill_tags"]
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if not isinstance(tags, (list, tuple)):
            raise ValidationError({"skill_tags": "Must be a list of skills."})
        ep.skill_tags = [str(t).strip()[:60] for t in tags if str(t).strip()][:20]
        fields.append("skill_tags")

    if "subject_description" in data:
        ep.subject_description = str(data["subject_description"])
        fields.append("subject_description")

    if "bio" in data:
        ep.bio = str(data["bio"])
        fields.append("bio")

    if "availability" in data:
        ep.availability = str(data["availability"])[:120]
        fields.append("availability")

    if "languages" in data:
        ep.languages = _coerce_languages(data["languages"]) or []
        fields.append("languages")

    if "hourly_rate" in data:
        try:
            rupees = int(data["hourly_rate"])
            if rupees < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValidationError({"hourly_rate": "Must be a whole number of rupees."})
        ep.hourly_rate = rupees * 100   # rupees → paise
        fields.append("hourly_rate")

    if "class_mode" in data:
        mode = data["class_mode"]
        if mode not in VALID_MODES:
            raise ValidationError({"class_mode": "Must be home, travel or online."})
        ep.class_mode = mode
        fields.append("class_mode")

    if "class_location" in data:
        ep.class_location = str(data["class_location"])[:255]
        fields.append("class_location")

    for f in ("pincode", "state", "district", "city"):
        if f in data:
            setattr(ep, f, str(data[f])[:150])
            fields.append(f)

    for f in ("latitude", "longitude"):
        if f in data and data[f] not in (None, ""):
            try:
                setattr(ep, f, float(data[f]))
                fields.append(f)
            except (TypeError, ValueError):
                raise ValidationError({f: "Must be a number."})

    # Photo (multipart only). Accept either `photo` or `expert_photo`.
    photo = files.get("photo") or files.get("expert_photo")
    if photo is not None:
        ep.photo = photo
        fields.append("photo")

    # Direct (P2P) payee details — the expert's own UPI; learners pay them.
    if "payment_upi" in data:
        ep.payment_upi = str(data["payment_upi"])[:120]
        fields.append("payment_upi")
    if "payment_name" in data:
        ep.payment_name = str(data["payment_name"])[:120]
        fields.append("payment_name")
    if "payment_note" in data:
        ep.payment_note = str(data["payment_note"])[:200]
        fields.append("payment_note")

    return fields


def validate_location(ep):
    """Exact location is required when teaching offline (home / travel)."""
    if ep.class_mode in OFFLINE_MODES and not (ep.class_location or "").strip():
        from rest_framework.exceptions import ValidationError
        raise ValidationError({
            "class_location":
            "Tell learners where the class is held (required for "
            "'at my place' / 'I can travel')."
        })


def apply_personal_fields(learner, data, *, files=None):
    """Apply name / dob / phone / photo onto the SELF LearnerProfile.

    Returns the list of touched field names. If only ``full_name`` is given we
    also derive first/last so the model's save() keeps full_name coherent."""
    files = files or {}
    fields = []

    has_first = "first_name" in data
    has_last = "last_name" in data
    if has_first:
        learner.first_name = str(data["first_name"])[:100]
        fields.append("first_name")
    if has_last:
        learner.last_name = str(data["last_name"])[:100]
        fields.append("last_name")

    if "full_name" in data:
        full = str(data["full_name"]).strip()[:255]
        learner.full_name = full
        fields.append("full_name")
        # Derive first/last only if the caller didn't send them explicitly,
        # so the model's save() (which rebuilds full_name from first+last when
        # either is present) doesn't clobber the name they typed.
        if full and not (has_first or has_last):
            parts = full.split(None, 1)
            learner.first_name = parts[0][:100]
            learner.last_name = (parts[1] if len(parts) > 1 else "")[:100]
            fields += ["first_name", "last_name"]

    if "phone" in data:
        learner.phone = str(data["phone"]).strip()[:20]
        fields.append("phone")

    if "date_of_birth" in data:
        learner.date_of_birth = _parse_date(data["date_of_birth"])
        fields.append("date_of_birth")

    photo = files.get("profile_photo") or files.get("photo")
    if photo is not None:
        learner.profile_photo = photo
        fields.append("profile_photo")

    return list(dict.fromkeys(fields))


# ── Read model (what every profile endpoint returns) ─────────────────────────
def serialize_expert(ep):
    """The editable profile + live completeness, for GET responses and the
    dashboard gate. Personal fields are read from the SELF learner."""
    # SELF only — writing/reading the expert's own personal fields.
    lp = ep.teacher_profile.user.self_learner_profile()
    comp = ep.completeness()
    return {
        # subject / teaching
        "category":            ep.category.slug if ep.category_id else "",
        "categories":          _all_category_slugs(ep),
        "subjects":            _all_category_labels(ep),
        "headline":            ep.headline,
        "skill_tags":          ep.skill_tags or [],
        "subject_description": ep.subject_description,
        "bio":                 ep.bio,
        "languages":           ep.languages or [],
        "availability":        ep.availability,
        "hourly_rate":         ep.hourly_rate // 100,     # paise → rupees
        # location
        "class_mode":          ep.class_mode,
        "class_location":      ep.class_location,
        "pincode":             ep.pincode,
        "state":               ep.state,
        "district":            ep.district,
        "city":                ep.city,
        "latitude":            ep.latitude,
        "longitude":           ep.longitude,
        # personal (SELF learner)
        "full_name":           (lp.full_name if lp else "") or "",
        "phone":               (lp.phone if lp else "") or "",
        "date_of_birth":       (lp.date_of_birth.isoformat() if (lp and lp.date_of_birth) else ""),
        "photo":               (lp.profile_photo.url if (lp and lp.profile_photo) else
                                (ep.photo.url if ep.photo else "")),
        # payment (P2P)
        "payment_upi":         ep.payment_upi,
        "payment_name":        ep.payment_name,
        "payment_note":        ep.payment_note,
        # status the dashboard gate reads
        "is_listed":           ep.is_listed,
        "is_advertised":       ep.is_advertised(),
        "reach_count":         ep.reach_count,
        # public stats shown on the "My Course" header
        "rating":              float(ep.rating) if ep.rating is not None else None,
        "sessions_count":      ep.sessions_count,
        "is_complete":         comp["is_complete"],
        "missing":             comp["missing"],
        # intro video (advertising clip, not a listing requirement)
        "intro_video_status":      ep.intro_video_status,
        "intro_video_embed_url":  ep.intro_video_embed_url(),
    }


def _all_categories(ep):
    """Primary category + M2M categories, de-duplicated, primary first."""
    seen, out = set(), []
    if ep.category_id and ep.category:
        seen.add(ep.category_id); out.append(ep.category)
    if ep.pk:
        for c in ep.categories.all():
            if c.id not in seen:
                seen.add(c.id); out.append(c)
    return out


def _all_category_slugs(ep):
    return [c.slug for c in _all_categories(ep)]


def _all_category_labels(ep):
    return [c.label for c in _all_categories(ep)]
