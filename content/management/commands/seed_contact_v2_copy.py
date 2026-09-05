# PLACEMENT: backend/content/management/commands/seed_contact_v2_copy.py
#
#     python manage.py seed_contact_v2_copy          # dry run (default)
#     python manage.py seed_contact_v2_copy --yes    # actually write
#
# Moves the `contact_hero` CMS section onto the redesigned /contact copy.
#
# WHY THIS EXISTS
#
# The redesigned Contact page (design handoff "ShikshaContact.html") is wired to
# the same `contact_hero` block and `contact_card` list items the page it
# replaces already used, following the house replace-if-present convention. That
# section has LIVE rows in production holding the previous page's words —
# heading "Contact ShikshaCom", subhead "Get in touch with us! …". So the moment
# the new page ships, production renders the new layout with the old copy: the
# hero reads "Contact ShikshaCom" instead of "We would love to hear from you."
#
# That is the wiring working correctly, not a bug — and this command is the
# other half of the change. Exactly the same situation as the /about redesign;
# see seed_about_v2_copy.py, which this mirrors deliberately.
#
# DESIGN NOTES
#
# * Dry run by default. Nothing is written without --yes.
# * Idempotent — a second run reports "Nothing to do".
# * One transaction. The rollback is a `raise` out of transaction.atomic(),
#   NOT transaction.savepoint(): savepoint() returns None in autocommit mode and
#   the matching savepoint_rollback() does nothing, so a "dry run" built on it
#   commits every row it claims to be discarding. This repo has shipped that
#   bug before (see seed_academy_launch.py's module docstring).
# * Cards are matched on `order`, which is what the four live rows are keyed by
#   (0-3). A missing row is created; an extra row beyond the four is left alone
#   and reported, never deleted — an editor may have added a third office.

from django.core.management.base import BaseCommand
from django.db import transaction

from content.models import (
    HomeContentBlock, HomeListItem, HomeListVariant, HomeSection, PublishStatus,
)

# The one address confirmed to exist. The design handoff also invented
# admissions@ / support@ / partners@ / careers@; those are deliberately not
# seeded anywhere — see Contact.jsx's header.
CONTACT_EMAIL = "info@shikshacom.com"

BLOCK = {
    "eyebrow": "Contact ShikshaCom",
    # The newline is the design's <br>. A CMS CharField cannot carry markup, so
    # the convention (established on /about) is a newline that withBreaks()
    # renders. It round-trips through the DB as \n.
    "heading": "We would love to\nhear",
    "heading_secondary": "from you.",
    "subhead": (
        "Whether it is a question about a course, help with your account, or a "
        "partnership you would like to discuss — send us a message and a real "
        "person from our team will get back to you, usually within one working day."
    ),
}

# Note the en-dashes in the postcodes and the dropped hyphen in "House No.
# 1473A" — the live rows use "House No. - 1473A" and a plain hyphen. Those are
# real copy changes from the handoff, not typos here.
CARDS = [
    {
        "order": 0,
        "icon": "location",
        "title": "Head Office",
        "body": "House No. 1473A<br>Maruti Vihar<br>Gurgaon, Haryana – 122002",
        "subtitle": "",
    },
    {
        "order": 1,
        # NOT "location": the design gives the regional office its own building
        # glyph. Contact.jsx's CARD_GLYPHS maps this key; an unknown value falls
        # back to the same-index default, so a stale row degrades to the pin
        # rather than to a broken icon.
        "icon": "building",
        "title": "Regional Office",
        "body": "Hualngohmun Vengchhak<br>Near World Bank Road<br>Aizawl, Mizoram – 796005",
        "subtitle": "",
    },
    {
        "order": 2,
        "icon": "email",
        "title": "Email",
        "body": CONTACT_EMAIL,
        # Renders as the card's <small>. The design has this line on the email
        # card only.
        "subtitle": "We reply within one working day.",
    },
    {
        "order": 3,
        "icon": "phone",
        "title": "Phone",
        "body": (
            "+0124-4255138 (Haryana)<br>+0389-2300225 (Mizoram)"
            "<br>+91 3893570403 (Mizoram)"
        ),
        "subtitle": "",
    },
]


def _short(value, width=58):
    text = str(value).replace("\n", "\\n")
    return text if len(text) <= width else text[: width - 1] + "…"


def _pair(old, new, width=58):
    return f"{_short(old, width)!r} -> {_short(new, width)!r}"


class Command(BaseCommand):
    help = "Move the contact_hero CMS section onto the redesigned /contact copy."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually write. Without this the command only reports.",
        )

    def handle(self, *args, **options):
        write = options["yes"]
        changes = []

        with transaction.atomic():
            changes += self._sync_block()
            changes += self._sync_cards()
            self._report_extras()

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
                self.stdout.write(self.style.WARNING(
                    f"DRY RUN — {len(changes)} change(s) above were NOT written. "
                    f"Re-run with --yes to apply."
                ))
                raise _DryRun()

            self.stdout.write(self.style.SUCCESS(f"Wrote {len(changes)} change(s)."))

    # ── the header block ──────────────────────────────────────────

    def _sync_block(self):
        changes = []
        block, created = HomeContentBlock.objects.get_or_create(
            section=HomeSection.CONTACT_HERO,
            defaults={"status": PublishStatus.PUBLISHED},
        )
        if created:
            changes.append("contact_hero: created the block (none existed)")

        for field, value in BLOCK.items():
            current = getattr(block, field) or ""
            if current != value:
                changes.append(f"contact_hero.{field}: {_pair(current, value)}")
                setattr(block, field, value)
        block.save()
        return changes

    # ── the four detail cards ─────────────────────────────────────

    def _sync_cards(self):
        changes = []
        for spec in CARDS:
            row = HomeListItem.objects.filter(
                section=HomeSection.CONTACT_HERO,
                variant=HomeListVariant.CONTACT_CARD,
                order=spec["order"],
            ).first()

            if row is None:
                row = HomeListItem(
                    section=HomeSection.CONTACT_HERO,
                    variant=HomeListVariant.CONTACT_CARD,
                    order=spec["order"],
                    status=PublishStatus.PUBLISHED,
                )
                changes.append(
                    f"contact_card[{spec['order']}]: created ({spec['title']})"
                )

            for field in ("icon", "title", "body", "subtitle"):
                current = getattr(row, field) or ""
                if current != spec[field]:
                    changes.append(
                        f"contact_card[{spec['order']}].{field}: "
                        f"{_pair(current, spec[field])}"
                    )
                    setattr(row, field, spec[field])
            row.save()
        return changes

    # ── anything the design has no slot for ───────────────────────

    def _report_extras(self):
        """A fifth card is not an error — an editor may have added an office.

        The page renders every row it is given, cycling the four accent
        gradients, so extras display fine. Reported so nobody has to wonder
        whether this command silently dropped one. Never deleted.
        """
        extras = HomeListItem.objects.filter(
            section=HomeSection.CONTACT_HERO,
            variant=HomeListVariant.CONTACT_CARD,
            order__gte=len(CARDS),
        )
        if not extras.exists():
            return
        self.stdout.write(self.style.WARNING(
            f"\n{extras.count()} extra contact_card row(s) beyond the "
            f"{len(CARDS)} this design specifies — left untouched:"
        ))
        for row in extras.order_by("order"):
            self.stdout.write(f"    · #{row.pk} order={row.order} {row.title!r}")


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
