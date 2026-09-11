"""
skills/management/commands/seed_skill_categories.py

Seed the Skill Development category catalog.

    python manage.py seed_skill_categories              # dry run — prints the plan
    python manage.py seed_skill_categories --yes        # apply

Dry run by default, like the other prod-touching seeders in this repo.

WHY THIS COMMAND IS PURELY ADDITIVE
-----------------------------------
An earlier version ended with::

    SkillCategory.objects.exclude(slug__in=[...]).delete()

described as removing a garbage "sdsd" row. Against real data that is a
blind delete of every category an admin ever added through the Skill CMS.
Production carries four such rows — ``Business`` (3 experts, 1 listing),
``Sports`` (1 expert, 1 listing), ``General`` and ``Painting`` — and none of
their slugs appear in the list below in that exact form, because the CMS
derives a slug from the label and so capitalises it.

That delete could not have ended well:

* ``SkillListing.category`` is ``PROTECT``, so the two categories holding
  listings raise ``ProtectedError`` and abort the whole queryset delete.
* ``ExpertProfile.category`` is ``SET_NULL``, so had it got through, experts
  would have been silently unclassified — and ``sync_primary_category``'s
  docstring explains that a blank ``category`` drops an expert out of every
  category filter on the public directory.

So: this command never deletes, and never deactivates. Removing a category
is a deliberate admin action with data consequences, not a side effect of
re-seeding.

MATCHING IS CASE-INSENSITIVE ON THE SLUG
----------------------------------------
``get_or_create(slug="business")`` does NOT match the existing ``Business``
row — it creates a second one, and the directory then shows two Business
categories with the experts split across them. Every lookup here is
``slug__iexact``, and an existing row KEEPS ITS OWN SLUG.

That matters beyond tidiness: ``directory_views.py`` filters on
``category__slug`` straight from a query parameter, so a slug is a wire
value. Re-slugging ``Business`` to ``business`` would quietly break any
``?cat=Business`` link already shared or bookmarked.

WHAT IT WILL CHANGE ON AN EXISTING ROW
--------------------------------------
Deliberately very little, so re-running never fights an admin's own edits:

* ``label``   — only for a row whose label still matches ``relabel_if``.
* ``icon`` / ``color`` — only when the row has none set.
* ``order``   — always, since it is presentation only and the point of the
  catalog is a coherent reading order.

``is_active`` is never touched: a category an admin deliberately switched
off must stay off.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from skills.models import SkillCategory


# The catalog. `order` groups related skills so the browse rail and the
# signup picker read in a sensible sequence rather than alphabetically.
#
# Slugs for the eight original entries (coding/design/music/lang/business/
# exam/crafts/spoken) are kept exactly as they were, so any environment that
# already ran the old command matches them instead of gaining near-duplicates.
#
# `relabel_if` renames an existing row ONLY while its label is still the
# given string. "Football" is too narrow to be a category — an expert who
# coaches basketball has nowhere to go — but if someone later renames it by
# hand, a re-run leaves their choice alone.
CATEGORIES = [
    # ── Tech & digital ────────────────────────────────────────────────
    {"slug": "coding",          "label": "Coding & Web",        "icon": "▣", "color": "#1b9c85", "order": 10},
    {"slug": "computer-basics", "label": "Computer Basics",     "icon": "⌨", "color": "#0ea5e9", "order": 11},
    {"slug": "data-ai",         "label": "Data & AI",           "icon": "◈", "color": "#6366f1", "order": 12},

    # ── Creative ──────────────────────────────────────────────────────
    {"slug": "design",          "label": "Design & Art",        "icon": "✦", "color": "#a78bfa", "order": 20},
    {"slug": "photography",     "label": "Photography & Video", "icon": "◉", "color": "#ec4899", "order": 21},
    {"slug": "crafts",          "label": "Crafts & Handmade",   "icon": "✄", "color": "#d97757", "order": 22},
    {"slug": "painting",        "label": "Painting",            "icon": "❁", "color": "#f59e0b", "order": 23},

    # ── Music & performing ────────────────────────────────────────────
    {"slug": "music",           "label": "Music & Audio",       "icon": "♪", "color": "#ff8f01", "order": 30},
    {"slug": "dance",           "label": "Dance",               "icon": "❋", "color": "#f472b6", "order": 31},

    # ── Language & communication ──────────────────────────────────────
    {"slug": "lang",            "label": "Languages",           "icon": "ᴬ", "color": "#60a5fa", "order": 40},
    {"slug": "spoken-english",  "label": "Spoken English",      "icon": "❞", "color": "#38bdf8", "order": 41},
    {"slug": "spoken",          "label": "Public Speaking",     "icon": "❝", "color": "#1dcaab", "order": 42},

    # ── Business & career ─────────────────────────────────────────────
    {"slug": "business",        "label": "Business & Finance",  "icon": "₹", "color": "#f87171", "order": 50},
    {"slug": "marketing",       "label": "Digital Marketing",   "icon": "◎", "color": "#fb923c", "order": 51},
    {"slug": "exam",            "label": "Exam Prep",           "icon": "✎", "color": "#125027", "order": 52},

    # ── Sport & wellbeing ─────────────────────────────────────────────
    {"slug": "sports",          "label": "Sports & Fitness",    "icon": "⬗", "color": "#22c55e",
     "order": 60, "relabel_if": "Football"},
    {"slug": "yoga",            "label": "Yoga & Wellness",     "icon": "❖", "color": "#14b8a6", "order": 61},

    # ── Practical & vocational ────────────────────────────────────────
    {"slug": "cooking",         "label": "Cooking & Baking",    "icon": "♨", "color": "#ef4444", "order": 70},
    {"slug": "tailoring",       "label": "Tailoring & Fashion", "icon": "✁", "color": "#8b5cf6", "order": 71},
    {"slug": "beauty",          "label": "Beauty & Grooming",   "icon": "❀", "color": "#f43f5e", "order": 72},

    # Catch-all, deliberately last: an expert whose skill the catalog does
    # not carry still has somewhere to go rather than abandoning signup.
    {"slug": "general",         "label": "General",             "icon": "◌", "color": "#94a3b8", "order": 99},
]


class Command(BaseCommand):
    help = "Seed the Skill Development category catalog. Additive; never deletes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this the command only prints the plan.",
        )

    def handle(self, *args, **options):
        apply_changes = options["yes"]

        plan = []
        for spec in CATEGORIES:
            existing = SkillCategory.objects.filter(slug__iexact=spec["slug"]).first()
            if existing is None:
                plan.append(("create", spec, None, []))
                continue

            changes = []
            if spec.get("relabel_if") and existing.label == spec["relabel_if"]:
                changes.append(("label", existing.label, spec["label"]))
            if not existing.icon and spec["icon"]:
                changes.append(("icon", existing.icon, spec["icon"]))
            if not existing.color and spec["color"]:
                changes.append(("color", existing.color, spec["color"]))
            if existing.order != spec["order"]:
                changes.append(("order", existing.order, spec["order"]))
            plan.append(("update" if changes else "keep", spec, existing, changes))

        creates = [p for p in plan if p[0] == "create"]
        updates = [p for p in plan if p[0] == "update"]
        keeps = [p for p in plan if p[0] == "keep"]

        for kind, spec, existing, changes in plan:
            if kind == "create":
                self.stdout.write(f"  create   {spec['slug']:16} {spec['label']}")
            elif kind == "update":
                detail = ", ".join(f"{f}: {old!r} → {new!r}" for f, old, new in changes)
                self.stdout.write(
                    f"  update   {existing.slug:16} {existing.label}  ({detail})"
                )
            else:
                self.stdout.write(f"  keep     {existing.slug:16} {existing.label}")

        # Anything already in the table that this catalog does not describe is
        # reported and then left completely alone — see the module docstring.
        known = {s.lower() for s in (c["slug"] for c in CATEGORIES)}
        extra = [c for c in SkillCategory.objects.all() if c.slug.lower() not in known]
        if extra:
            self.stdout.write("\n  Not in this catalog, left untouched:")
            for c in extra:
                self.stdout.write(
                    f"    {c.slug:16} {c.label}  "
                    f"(experts={c.experts.count()}, listings={c.listings.count()})"
                )

        self.stdout.write(
            f"\n{len(creates)} to create, {len(updates)} to update, "
            f"{len(keeps)} already correct, {len(extra)} untouched."
        )

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "\nDry run — nothing written. Re-run with --yes to apply."
            ))
            return

        with transaction.atomic():
            for kind, spec, existing, changes in plan:
                if kind == "create":
                    SkillCategory.objects.create(
                        slug=spec["slug"], label=spec["label"], icon=spec["icon"],
                        color=spec["color"], order=spec["order"], is_active=True,
                    )
                elif kind == "update":
                    for field, _old, new in changes:
                        setattr(existing, field, new)
                    # Note the absent `slug` and `is_active`: an existing row
                    # keeps both, for the reasons in the module docstring.
                    existing.save(update_fields=[f for f, _o, _n in changes])

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. {len(creates)} created, {len(updates)} updated. Nothing deleted."
        ))
