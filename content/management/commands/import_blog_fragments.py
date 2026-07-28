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
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from content.models import BlogPost, ClassLevel, PublishStatus, Subject

H1_RX = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
P_RX = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
TAG_RX = re.compile(r"<[^>]+>")

VALID_SUBJECTS = {c.value for c in Subject}
VALID_CLASSES = {c.value for c in ClassLevel}

# ── Cover thumbnails ────────────────────────────────────────────────────────
# The 114 legacy blog cards each shipped a bundled thumbnail under
# shiksha-frontend/src/assets/. The fragment tree carries no images, so this
# command locates the matching thumbnail by (class_level, subject, chapter) and
# copies it into MEDIA_ROOT, then sets BlogPost.cover — otherwise every card
# falls back to a flat green gradient (COURSES_BLOGS_CMS_PLAN §6 trap 1).
#
# Layout differs by class (verified 1:1 against the frontend's blogsData.js
# import map — all 114 resolve exactly):
#   class 8/9 → assets/blog/blog-class<N>/<subject>/<chapter>.(jpg|png)
#   class 10  → per-subject folders (geography lives in its own blog-class10/):
CLASS10_COVER_DIRS = {
    "geography": "blog-class10",
    "history": "blog/History",
    "economics": "blog/Economics",
    "political-science": "blog/Political Science",
    "science": "blog/Science",
}
COVER_EXTS = (".jpg", ".png")

# Frontend `subject` folder ↔ content.Subject enum. blogsData.js uses
# "political-science"; the enum value matches, and "civics" is a class-8-only
# folder that has no enum member (posts store it as GENERAL) — resolve covers
# off the slug segment, which is the authoritative key, not the enum.


def _relative_cover_path(class_level, subject_seg, chapter):
    """(class, subject-slug, chapter) → assets-relative dir, or None if N/A.
    subject_seg is the raw slug segment (e.g. 'political-science', 'civics')."""
    if chapter is None or class_level not in {"8", "9", "10"}:
        return None
    if class_level in ("8", "9"):
        return f"blog/blog-class{class_level}/{subject_seg}"
    return CLASS10_COVER_DIRS.get(subject_seg)


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
        parser.add_argument(
            "--assets-root",
            default=str(Path(settings.BASE_DIR).parent / "shiksha-frontend" / "src" / "assets"),
            help=("Directory holding the bundled blog thumbnails "
                  "(shiksha-frontend/src/assets). Set to '' to skip cover import."),
        )

    def handle(self, *args, root, dry_run, update, draft, assets_root, **options):
        base = Path(root).resolve()
        if not base.is_dir():
            raise CommandError(f"Not a directory: {base}")

        assets_base = Path(assets_root).resolve() if assets_root else None
        if assets_base and not assets_base.is_dir():
            self.stdout.write(self.style.WARNING(
                f"assets-root not found ({assets_base}) — covers will be skipped."
            ))
            assets_base = None

        files = sorted(base.rglob("*.html"))
        if not files:
            raise CommandError(f"No .html fragments found under {base}")

        created = updated = skipped = covers_set = covers_missing = 0
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

            cover_src = self._find_cover(slug, parts, assets_base)

            if dry_run:
                action = "UPDATE" if existing else "CREATE"
                self.stdout.write(f"[dry-run] {action}  {slug}  ← {title!r}")
                if assets_base is not None:
                    if cover_src is not None:
                        rel = cover_src.relative_to(assets_base).as_posix()
                        self.stdout.write(f"            cover ← {rel}")
                        covers_set += 1
                    elif parts["chapter"] is not None:
                        self.stdout.write(self.style.WARNING(
                            "            cover ← (none found)"
                        ))
                        covers_missing += 1
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
                post = existing
                updated += 1
            else:
                post = BlogPost.objects.create(slug=slug, **fields)
                created += 1

            # Cover-image step (write mode only). Deterministic dest path keyed
            # on the stable file mtime + slug, so re-runs neither duplicate the
            # media file nor churn the cover field.
            if cover_src is not None:
                if self._copy_cover(post, cover_src, mtime):
                    covers_set += 1
            elif assets_base is not None and parts["chapter"] is not None:
                covers_missing += 1

        cover_line = (
            f" covers_set={covers_set} covers_missing={covers_missing}"
            if assets_base is not None else " (covers skipped — no assets-root)"
        )
        self.stdout.write(self.style.SUCCESS(
            f"Done. created={created} updated={updated} skipped={skipped}{cover_line} "
            f"(of {len(files)} fragments)"
            + (" [dry run — nothing written]" if dry_run else "")
        ))

    def _find_cover(self, slug, parts, assets_base):
        """Locate the bundled thumbnail for this post, or None. Keyed on the
        slug's own segments (the authoritative class/subject/chapter)."""
        if assets_base is None:
            return None
        segs = slug.split("/")
        subject_seg = segs[1] if len(segs) >= 3 else parts["subject"]
        rel_dir = _relative_cover_path(parts["class_level"], subject_seg, parts["chapter"])
        if rel_dir is None:
            return None
        for ext in COVER_EXTS:
            candidate = assets_base / rel_dir / f"{parts['chapter']}{ext}"
            if candidate.is_file():
                return candidate
        return None

    def _copy_cover(self, post, source, mtime):
        """Copy `source` into MEDIA_ROOT under a deterministic name and set
        post.cover. Returns True if the cover ended up set. Idempotent: an
        already-present dest file is reused, and the cover field is only
        re-saved when it actually changes."""
        rel_name = (
            f"content/blog/{mtime:%Y}/{mtime:%m}/"
            f"{post.slug.replace('/', '-')}{source.suffix.lower()}"
        )
        dest_abs = Path(settings.MEDIA_ROOT) / rel_name
        if not dest_abs.exists():
            dest_abs.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest_abs)
        if post.cover.name != rel_name:
            post.cover.name = rel_name
            post.save(update_fields=["cover"])
        return True

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
