# PLACEMENT: backend/content/management/commands/import_blog_fragments.py
#
# One-shot migration of the legacy chapters into the CMS.
#
# The frontend's scripts/extract-blogs.mjs already converted the 114 JSX
# chapters into static HTML fragments laid out as
#     <root>/class-9/economics/chapter-1.html
# (the same tree served from VITE_BLOG_CDN_BASE / public/blog-content).
# This command walks that tree and creates published BlogPosts:
#
#     python manage.py import_blog_fragments /path/to/blog-content
#     python manage.py import_blog_fragments /path/to/blog-content --dry-run
#     python manage.py import_blog_fragments /path/to/blog-content --update
#
# • slug           ← relative path without .html
# • class/subject/chapter ← parsed from the slug (class-9/economics/chapter-1)
# • title          ← first <h1> in the fragment (fallback: slug)
# • excerpt        ← first <p> text, trimmed to 240 chars
# • trusted_html   ← True (first-party build artifact; sanitizer skipped so
#                    the fragment renders pixel-identical)
# • status         ← published, publish_at = file mtime
#
# Idempotent: existing slugs are skipped unless --update is passed.

import datetime
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from content.models import BlogPost, ClassLevel, PublishStatus, Subject

H1_RX = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
P_RX = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
TAG_RX = re.compile(r"<[^>]+>")

VALID_SUBJECTS = {c.value for c in Subject}
VALID_CLASSES = {c.value for c in ClassLevel}


def _text(html_fragment, limit=None):
    text = TAG_RX.sub(" ", html_fragment or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip() if limit else text


class Command(BaseCommand):
    help = "Import legacy static blog fragments (<slug>.html tree) as BlogPosts."

    def add_arguments(self, parser):
        parser.add_argument("root", help="Directory containing the fragment tree.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would happen; write nothing.")
        parser.add_argument("--update", action="store_true",
                            help="Overwrite body/title of existing slugs.")
        parser.add_argument("--draft", action="store_true",
                            help="Import as drafts instead of published.")

    def handle(self, *args, root, dry_run, update, draft, **options):
        base = Path(root).resolve()
        if not base.is_dir():
            raise CommandError(f"Not a directory: {base}")

        files = sorted(base.rglob("*.html"))
        if not files:
            raise CommandError(f"No .html fragments found under {base}")

        created = updated = skipped = 0
        for file in files:
            slug = file.relative_to(base).with_suffix("").as_posix().lower()
            html = file.read_text(encoding="utf-8", errors="replace")

            parts = self._parse_slug(slug)

            h1 = H1_RX.search(html)
            title = _text(h1.group(1)) if h1 else slug.replace("/", " · ").title()
            first_p = P_RX.search(html)
            excerpt = _text(first_p.group(1), 240) if first_p else ""

            mtime = datetime.datetime.fromtimestamp(file.stat().st_mtime)
            if timezone.is_naive(mtime):
                mtime = timezone.make_aware(mtime)

            existing = BlogPost.objects.filter(slug=slug).first()
            if existing and not update:
                skipped += 1
                continue

            if dry_run:
                action = "UPDATE" if existing else "CREATE"
                self.stdout.write(f"[dry-run] {action}  {slug}  ← {title!r}")
                created += 0 if existing else 1
                updated += 1 if existing else 0
                continue

            fields = dict(
                title=title[:300],
                excerpt=excerpt,
                body_html=html,
                trusted_html=True,   # first-party build artifact
                class_level=parts["class_level"],
                subject=parts["subject"],
                chapter_number=parts["chapter"],
                status=PublishStatus.DRAFT if draft else PublishStatus.PUBLISHED,
                publish_at=mtime,
            )
            if existing:
                for key, value in fields.items():
                    setattr(existing, key, value)
                existing.save()
                updated += 1
            else:
                BlogPost.objects.create(slug=slug, **fields)
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. created={created} updated={updated} skipped={skipped} "
            f"(of {len(files)} fragments)"
            + (" [dry run — nothing written]" if dry_run else "")
        ))

    @staticmethod
    def _parse_slug(slug):
        """class-9/economics/chapter-1 → class_level/subject/chapter."""
        cls, subject, chapter = ClassLevel.GENERAL, Subject.GENERAL, None
        for seg in slug.split("/"):
            cm = re.fullmatch(r"class-(\d{1,2})", seg)
            if cm and cm.group(1) in VALID_CLASSES:
                cls = cm.group(1)
                continue
            ch = re.fullmatch(r"chapter-(\d{1,3})", seg)
            if ch:
                chapter = int(ch.group(1))
                continue
            if seg in VALID_SUBJECTS:
                subject = seg
        return {"class_level": cls, "subject": subject, "chapter": chapter}
