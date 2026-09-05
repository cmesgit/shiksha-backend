# PLACEMENT: backend/content/management/commands/seed_about_v2_copy.py
#
# Moves the five about_* CMS sections onto the redesigned /about page's copy.
#
#     python manage.py seed_about_v2_copy            # dry run (default)
#     python manage.py seed_about_v2_copy --yes      # actually write
#     python manage.py seed_about_v2_copy --yes --clear-photos
#
# Why this exists
# ---------------
# shiksha-frontend's AboutUs.jsx replaced About2.jsx with a new design, and it
# is wired to the same five sections the old page used: about_hero,
# about_vision, about_mission, about_values, about_why. That wiring is
# "replace-if-present" — a CMS row always beats the hardcoded fallback.
#
# The rows in the database still hold the PREVIOUS design's words. So the live
# page renders the new layout with the old copy:
#
#     hero      h1  "About Us"                    not  "Building better
#                                                       learning for every
#                                                       student."
#     vision    h2  "Our Vision"                  not  "A future where
#                                                       learning reaches
#                                                       everyone."
#     values    h2  "Our Value"                   not  "What we believe
#                                                       shapes how we teach."
#
# That is the wiring working correctly, not a bug — but it is not what anyone
# approved, so this command exists to close the gap in one transaction instead
# of asking someone to retype 30 fields across five admin screens.
#
# It ALSO fills in two things the old design had no field for, which is what
# makes the new page fully editable rather than half-editable:
#
#   * about_vision's five list rows get a `title`. They carry only a `body`
#     today, because the old design rendered them as bare bullets. The new
#     design's initiative cards have a heading AND a body, so without this the
#     headings stay frozen in the bundle.
#   * about_mission's four pillar rows get a `body`, for the same reason in
#     reverse — they carry only a `title`.
#
# Safety model (same as seed_about_images / seed_homepage_defaults)
# ----------------------------------------------------------------
# * Dry run by default. Nothing is written without --yes.
# * One transaction. A failure part-way leaves the CMS exactly as it was.
# * Idempotent: re-running after a successful run reports 0 changes.
# * Reports every field it would change, old value -> new value, so the diff is
#   reviewable before it happens.
#
# What it deliberately does NOT do
# --------------------------------
# * It does not touch about_values' three `bullet` rows ("Digital Mode of
#   Learning"). The new design has no section for them, so they are dead
#   content — but deleting an editor's words is not this command's call. It
#   reports them and leaves them alone.
# * It does not clear about_values' image unless --clear-photos is passed.
#   That row points at studio.jpeg, and AboutUs.jsx honours a CMS photo over
#   its own illustration (the handoff explicitly marks that as the swap point),
#   so today the section shows the photograph rather than the new artwork.
#   Which of those is wanted is a design decision, not a migration.

from django.core.management.base import BaseCommand
from django.db import transaction

from content.models import HomeContentBlock, HomeListItem, HomeListVariant, HomeSection

# ── Section blocks ───────────────────────────────────────────────────────────
# `heading` carries a newline where the design has a <br>. AboutUs.jsx's
# withBreaks() turns it back into one; a heading with no newline just wraps.
BLOCKS = {
    HomeSection.ABOUT_HERO: {
        "eyebrow": "About ShikshaCom",
        "heading": "Building better learning\nfor",
        "heading_secondary": "every student.",
        "subhead": (
            "Through innovative technology, our intelligent platform empowers "
            "individuals to achieve their full potential and contribute "
            "positively to society — bringing structured, accessible education "
            "within reach of every learner."
        ),
        "cta_primary_label": "Why choose us",
        "cta_primary_href": "#ap-why",
        "cta_secondary_label": "Our vision",
        "cta_secondary_href": "#ap-vision",
    },
    HomeSection.ABOUT_VISION: {
        "eyebrow": "Our Vision",
        "heading": "A future where learning\nreaches",
        "heading_secondary": "everyone.",
        "subhead": (
            "At ShikshaCom, our vision is to provide learners with the skills "
            "and knowledge they need to thrive in the modern world. We aim to "
            "make education accessible, engaging, and effective for everyone, "
            "regardless of their background or location."
        ),
        "extra": {"list_label": "Five ways we get there — scroll to explore ↓"},
    },
    HomeSection.ABOUT_MISSION: {
        "eyebrow": "Our Mission",
        "heading": "What drives",
        "heading_secondary": "everything we do",
        "subhead": (
            "At ShikshaCom, our mission is to deliver high-quality, accessible "
            "education using innovative technology and expert guidance. We are "
            "committed to empowering learners of all ages and backgrounds to "
            "achieve their full potential."
        ),
    },
    HomeSection.ABOUT_VALUES: {
        "eyebrow": "Our Value",
        "heading": "What we believe shapes",
        "heading_secondary": "how we teach.",
        "subhead": (
            "At ShikshaCom, our values are the foundation of everything we do. "
            "These values guide our decisions and inspire our team to create a "
            "positive impact in the world of education."
        ),
        "extra": {"list_label": "Our core values"},
    },
    HomeSection.ABOUT_WHY: {
        "eyebrow": "Why ShikshaCom",
        "heading": "Why choose",
        "heading_secondary": "ShikshaCom?",
        "subhead": (
            "At ShikshaCom, we offer a unique learning experience designed to "
            "meet the needs of modern learners. Choose ShikshaCom for education "
            "that is effective, enjoyable, and accessible from anywhere."
        ),
    },
}

# ── List rows ────────────────────────────────────────────────────────────────
# Matched to existing rows by (section, variant, order) — never by primary key,
# which differs between dev and production. A row that does not exist is
# created; the create path is what makes this useful on dev, where the about_*
# sections may be empty.
#
# `icon` values are keys into AboutUs.jsx's ICONS registry
# (src/components/about/aboutIcons.jsx). An unrecognised key falls back to the
# hardcoded icon at the same index rather than rendering a hole, so a typo here
# degrades quietly — check the page, not just this command's output.
ITEMS = {
    # Hero — the five floating ecosystem nodes. These rows exist on production
    # as image-only stickers (the old design's artwork row); the new design has
    # no image slot for them, so they need title + subtitle before AboutUs.jsx
    # will use them at all. Until then it renders its own five labels.
    (HomeSection.ABOUT_HERO, HomeListVariant.STICKER): [
        {"icon": "book", "title": "Knowledge", "subtitle": "Structured content"},
        {"icon": "people", "title": "Learners", "subtitle": "Every background"},
        {"icon": "target", "title": "Goals", "subtitle": "Real outcomes"},
        {"icon": "bulb", "title": "Ideas", "subtitle": "Curiosity first"},
        {"icon": "monitor", "title": "Online", "subtitle": "Learn anywhere"},
    ],
    # Vision — the five scroll-stacking initiative cards. Bodies already match
    # production; the titles and the "Initiative 0N" kickers are new.
    (HomeSection.ABOUT_VISION, HomeListVariant.DEFAULT): [
        {
            "icon": "init1",
            "title": "Technology-led learning",
            "subtitle": "Initiative 01",
            "body": (
                "Leveraging technology like online learning platforms, mobile "
                "schools, and digital resources to reach students in remote areas."
            ),
        },
        {
            "icon": "init2",
            "title": "Knowledge for remote areas",
            "subtitle": "Initiative 02",
            "body": (
                "Enabling individuals in remote areas to acquire knowledge and "
                "skills for personal growth."
            ),
        },
        {
            "icon": "init3",
            "title": "Learning for everyone",
            "subtitle": "Initiative 03",
            "body": "Supporting learners of all backgrounds, abilities, and learning styles.",
        },
        {
            "icon": "init4",
            "title": "Career awareness",
            "subtitle": "Initiative 04",
            "body": "Raising awareness of non-traditional and diverse career opportunities.",
        },
        {
            "icon": "init5",
            "title": "Classroom at your doorstep",
            "subtitle": "Initiative 05",
            "body": "Bringing the classroom to your doorstep.",
        },
    ],
    # Mission — the four rotating cards. Titles and icons already match
    # production; only the supporting line under each is new.
    (HomeSection.ABOUT_MISSION, HomeListVariant.PILLAR): [
        {
            "icon": "users",
            "title": "Fostering a supportive community",
            "body": "Bringing learners and mentors together so no one has to study alone.",
        },
        {
            "icon": "wrench",
            "title": "Leveraging cutting-edge tools",
            "body": "Modern learning technology that makes lessons clearer and more engaging.",
        },
        {
            "icon": "sprout",
            "title": "Making education inclusive, effective, and transformative",
            "body": (
                "Learning that works for real students and changes what's "
                "possible for them."
            ),
        },
        {
            "icon": "home",
            "title": "Bridging the gap in educational access between urban and rural settings",
            "body": "Closing the distance so where you live no longer limits how you learn.",
        },
    ],
    # Values — the four timeline steps. Already correct on production; listed so
    # a dev database with empty about_* sections ends up matching production.
    (HomeSection.ABOUT_VALUES, HomeListVariant.DEFAULT): [
        {"icon": "medal", "title": "Commitment to Excellence",
         "body": "Ensures we deliver the highest quality education."},
        {"icon": "value2", "title": "Quality",
         "body": "In our content and services empowers learners to succeed."},
        {"icon": "value3", "title": "Inclusivity",
         "body": "Welcomes and supports learners from all backgrounds."},
        {"icon": "value4", "title": "Innovation",
         "body": "Drives us to continuously improve and adapt."},
    ],
    # Why — the four cards. Already correct on production; same reason as above.
    (HomeSection.ABOUT_WHY, HomeListVariant.NUMBERED): [
        {"icon": "layers", "title": "Interactive Courses",
         "body": "Engage students with multimedia content, quizzes, and real-world projects."},
        {"icon": "video", "title": "Live Classes",
         "body": ("Direct interaction with expert instructors, fostering a "
                  "supportive learning environment.")},
        {"icon": "gauge", "title": "Personalized Dashboards",
         "body": "Track your progress and get content tailored to your learning pace and goals."},
        {"icon": "chat", "title": "Vibrant Community",
         "body": "Connect with peers, share knowledge, and grow together in a thriving forum."},
    ],
}


def _short(value, width=58):
    """One-line preview of a field value for the change report."""
    text = str(value).replace("\n", "\\n")
    return text if len(text) <= width else text[: width - 1] + "…"


def _pair(old, new, width=58):
    """Preview an old -> new field change.

    Truncating both sides independently is worse than useless when the edit is
    at the end of a long string: about_hero.subhead only gains a trailing
    clause, so a naive head-truncation renders it as 'x…' -> 'x…' and the dry
    run reports a real change as though it were a no-op. When the two share a
    long prefix, skip past it and show the part that actually differs.
    """
    a, b = str(old).replace("\n", "\\n"), str(new).replace("\n", "\\n")
    if len(a) <= width and len(b) <= width:
        return repr(a), repr(b)

    common = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        common += 1

    # Only worth eliding when the shared head is long enough to be the reason
    # both sides look identical.
    if common >= width - 12:
        cut = max(0, common - 8)
        return repr("…" + _short(a[cut:], width)), repr("…" + _short(b[cut:], width))
    return repr(_short(a, width)), repr(_short(b, width))


class Command(BaseCommand):
    help = "Move the five about_* CMS sections onto the redesigned /about copy."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this the command only reports.",
        )
        parser.add_argument(
            "--clear-photos", action="store_true",
            help=(
                "Also clear image/image_url on the about_* blocks, so the new "
                "design's own illustrations show instead of the old "
                "photographs. Affects about_values (studio.jpeg) today."
            ),
        )

    def handle(self, *args, **options):
        write = options["yes"]
        clear_photos = options["clear_photos"]
        changes = []

        with transaction.atomic():
            changes += self._sync_blocks(clear_photos)
            changes += self._sync_items()
            self._report_orphans()

            if not changes:
                self.stdout.write(self.style.SUCCESS(
                    "Nothing to do — the CMS already matches the redesigned page."
                ))
                return

            self.stdout.write("")
            for line in changes:
                self.stdout.write(f"  {line}")
            self.stdout.write("")

            if not write:
                # A rollback has to be a raise out of atomic(). Django's
                # transaction.savepoint() is a silent no-op in autocommit mode,
                # so a "dry run" built on savepoint_rollback() commits every row
                # it claims to be discarding — this repo has been bitten by that
                # exact bug before (seed_academy_launch's module docstring).
                self.stdout.write(self.style.WARNING(
                    f"DRY RUN — {len(changes)} change(s) above were NOT written. "
                    f"Re-run with --yes to apply."
                ))
                raise _DryRun()

            self.stdout.write(self.style.SUCCESS(
                f"Wrote {len(changes)} change(s)."
            ))

    # ── blocks ──────────────────────────────────────────────────────────────
    def _sync_blocks(self, clear_photos):
        changes = []
        for section, fields in BLOCKS.items():
            block, created = HomeContentBlock.objects.get_or_create(section=section)
            if created:
                changes.append(f"[create] block {section}")

            for name, new in fields.items():
                old = getattr(block, name)
                if old == new:
                    continue
                was, now = _pair(old, new)
                changes.append(f"[block ] {section}.{name}: {was} -> {now}")
                setattr(block, name, new)

            if clear_photos and (block.image or block.image_url):
                changes.append(
                    f"[block ] {section}: clearing photo "
                    f"({_short(block.image_url or block.image.name)})"
                )
                block.image = None
                block.image_url = ""

            block.save()
        return changes

    # ── list rows ───────────────────────────────────────────────────────────
    def _sync_items(self):
        changes = []
        for (section, variant), rows in ITEMS.items():
            existing = list(
                HomeListItem.objects.filter(section=section, variant=variant).order_by("order", "id")
            )
            for index, spec in enumerate(rows):
                if index < len(existing):
                    item = existing[index]
                else:
                    item = HomeListItem(section=section, variant=variant, order=index)
                    changes.append(f"[create] {section}/{variant} #{index}")

                for name, new in spec.items():
                    old = getattr(item, name)
                    if old == new:
                        continue
                    was, now = _pair(old, new)
                    changes.append(
                        f"[item  ] {section}/{variant} #{index}.{name}: {was} -> {now}"
                    )
                    setattr(item, name, new)

                if item.order != index:
                    changes.append(
                        f"[item  ] {section}/{variant} #{index}.order: "
                        f"{item.order} -> {index}"
                    )
                    item.order = index

                item.save()

            surplus = existing[len(rows):]
            if surplus:
                # Left in place on purpose — an editor may have added a sixth
                # card deliberately, and AboutUs.jsx renders extra rows rather
                # than dropping them.
                self.stdout.write(self.style.WARNING(
                    f"  note: {section}/{variant} has {len(surplus)} row(s) beyond "
                    f"the {len(rows)} this command manages. Left untouched; the "
                    f"page will render them."
                ))
        return changes

    def _report_orphans(self):
        """Content the redesign has no home for. Reported, never deleted."""
        orphans = HomeListItem.objects.filter(
            section=HomeSection.ABOUT_VALUES, variant=HomeListVariant.BULLET,
        )
        count = orphans.count()
        if not count:
            return
        self.stdout.write(self.style.WARNING(
            f"\n  {count} 'Digital Mode of Learning' bullet row(s) on about_values "
            f"have no section in the redesigned page and are not rendered anywhere. "
            f"Left in the database — delete them in the admin if they are done with:"
        ))
        for row in orphans.order_by("order"):
            self.stdout.write(f"    · #{row.pk} {_short(row.body)!r}")


class _DryRun(Exception):
    """Raised to roll the transaction back after a dry run."""


# Django prints the traceback of an exception escaping handle(), which would
# make a successful dry run look like a crash. Swallow just this one.
_original_handle = Command.handle


def _handle(self, *args, **options):
    try:
        return _original_handle(self, *args, **options)
    except _DryRun:
        return None


Command.handle = _handle
