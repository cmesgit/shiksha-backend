# PLACEMENT: backend/backend/counseling/guide_import.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/counseling/guide_import.py
#
# Converts the approved career-guidance .docx sources into the render-ready
# block tree the frontend already understands. Pure functions, no ORM — the
# management command and the (future) admin upload endpoint both call
# parse_docx() so there is exactly ONE parser.
#
# Why stdlib zipfile + ElementTree and not python-docx: python-docx is not
# installed on the droplet, nothing there automates pip install, and these
# documents need no feature beyond styles/paragraphs/tables/numbering. One
# less production dependency for a job that runs a handful of times.
#
# ── Why chapter structure is DECLARED, not detected ──────────────────
# The ten source documents were hand-authored over time and their Word
# outline levels are not semantically usable. Measured, not guessed:
#
#   • "New DOCX Document.docx" uses `heading 1` for 2,175 paragraphs —
#     real chapters ("1: Overview of Higher Education in India") sit at
#     the same level as "Quick Facts" and "Placements & Career Support".
#   • "career guidance after class 10.docx" is missing its chapter-1
#     heading entirely, marks chapters with `heading 2`, and marks
#     SUBsections ("3.1 Science Stream") with `heading 1`.
#   • "commerce.docx" numbers chapters 4–9 but leaves 1–3 unnumbered;
#     "science.docx" drops the number on chapter 2 only.
#   • "pg.docx"/"ug.docx" have `heading 1` + `heading 3` and no
#     `heading 2` at all.
#
# So every heuristic that keys off outline level or numbering produces
# garbage on at least three documents. Instead the manifest names each
# guide's chapter headings explicitly (there are only ten documents, and
# they change rarely), and `propose_structure()` generates a first draft
# of that list for a human to check into the manifest. Detection is an
# authoring aid; the manifest is the source of truth.

from __future__ import annotations

import hashlib
import re
import zipfile
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: Heading text prefixed with one of these is a CALLOUT (Remember/Tip/
#: Warning/Example box), at any outline level. Narrow and specific on
#: purpose — most emoji in this corpus (📖 "Overview", ✅ "Eligibility",
#: 📄 "Documents Required", ...) just decorate an ordinary subsection
#: heading and must fall through to add_subheading(), not add_callout().
CALLOUT_MARKERS = ("📌", "💡", "⚠️", "⚠", "🎯")

#: Broad emoji/symbol strip for TOC MATCHING only (via norm(), below) —
#: covers every decorative marker actually observed in this corpus (📅 🌐
#: 🏛 ✅ 📚 📋 📄 📖 🌍 plus the callout set), so a TOC line restating
#: "References" matches a real "📚 References" heading regardless of which
#: icon the author put on it. Deliberately NOT used to decide callout-ness
#: in the main parse loop — that stays keyed off CALLOUT_MARKERS above.
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF️]+"
)

#: Section-kind classifiers, applied to the section title in order.
KIND_PATTERNS = (
    ("worksheet", re.compile(r"^(worksheet|activit(y|ies)|self[\s-]?assessment|exercise)", re.I)),
    ("action_plan", re.compile(r"^(action plan|my (career )?action plan|student action plan|my plan|\d+[\s-]day)", re.I)),
    # "Parents & Guardians Guide" as well as "Parent & Guardian Guide" —
    # the two school-level documents disagree on the plural.
    ("parent_guide", re.compile(r"parents?\s*(&|and|/)?\s*guardians?", re.I)),
    ("faq", re.compile(r"(frequently asked question|^faqs?\b)", re.I)),
    ("references", re.compile(r"^references?$", re.I)),
)

URL_RE = re.compile(r"https?://[^\s<>()\[\]]+")

#: Splits "1. Introduction2. Popular Courses3. ..." — some documents
#: emit their whole table of contents as ONE paragraph with no line
#: breaks between entries, so per-paragraph norm() matching never fires.
#: The negative lookbehind matters: without it, "10. Final Thoughts"
#: also matches the lookahead starting at its SECOND digit ("0. Final
#: Thoughts" looks like a valid "\d{1,2}\.\s" on its own), so any
#: two-digit chapter number gets sliced into "1" + "0. Title".
_TOC_FRAGMENT = re.compile(r"(?<!\d)(?=\d{1,2}\.\s)")


#: A short, closed set of trailing TOC words that several documents list
#: without ever giving them a real heading in the body (the "References"
#: list, where present, is just URLs with no heading of its own; a
#: "Module Summary" that only exists as the TOC's own wrap-up label).
#: Tolerated anywhere in a blob so one un-headed trailer doesn't sink an
#: otherwise-clear TOC-blob match.
_TOC_FILLER_WORDS = {"references", "module summary", "contents"}


def _strip_trailing_fillers(key: str) -> str:
    """Peel known filler phrases off the END of a normalised string,
    longest first. The LAST fragment of a squashed TOC blob has no
    delimiter between the true final chapter and whatever trails it —
    "10. Final Thoughts Module Summary References" splits (on digits)
    into one fragment, "final thoughts module summary references" once
    normalised — so the fragment as a whole never equals a heading key.
    Stripping known trailers back to "final thoughts" is what lets it
    resolve."""
    changed = True
    while changed:
        changed = False
        for filler in sorted(_TOC_FILLER_WORDS, key=len, reverse=True):
            if key == filler:
                key, changed = "", True
            elif key.endswith(" " + filler):
                key, changed = key[: -(len(filler) + 1)].strip(), True
    return key


def _fragment_is_known(fragment: str, heading_keys: set) -> bool:
    key = norm(fragment)
    return (
        key in heading_keys
        or key in _TOC_FILLER_WORDS
        or _strip_trailing_fillers(key) in heading_keys
    )


def _is_toc_blob(text: str, heading_keys: set) -> bool:
    """A single paragraph that IS an entire concatenated table of
    contents (secondary school.docx and admission-India-style docs keep
    each entry on its own line; arts.docx instead runs them all
    together). Require at least 3 fragments and every one to resolve to
    a known heading, so a normal sentence that happens to contain one
    number never trips this."""
    fragments = [f.strip() for f in _TOC_FRAGMENT.split(text) if f.strip()]
    if len(fragments) < 3:
        return False
    return all(_fragment_is_known(f, heading_keys) for f in fragments)

_NUM_PREFIX = re.compile(r"^\d{1,2}(\.\d{1,3})*\s*[:.\)]?\s*")
_WS = re.compile(r"\s+")


# ─────────────────────────────────────────────────────────────────
#  Text helpers
# ─────────────────────────────────────────────────────────────────

_CHAPTER_WORD = re.compile(r"^chapter\s+(?=\d)", re.I)


def norm(text: str) -> str:
    """Loose key, for matching a table-of-contents entry against the
    heading it points at: collapse whitespace, drop a leading "Chapter"
    word and/or section number, strip callout markers, casefold. Lets
    "1.Introduction" (no space — how it appears in one document's TOC)
    match the "1. Introduction" heading, and lets a TOC's "8. Stream to
    Career Pathways" match the real heading "Chapter 8: Stream to Career
    Pathways" — the TOC entries never repeat the word "Chapter" even
    when the heading itself has it.

    Deliberately NOT used for chapter matching: it maps both
    "1. Introduction" and "8.1 Introduction" to "introduction", and
    higher schoo.docx contains both."""
    t = _WS.sub(" ", (text or "").replace("\xa0", " ")).strip()
    t = _EMOJI_RE.sub("", t).strip()
    t = _CHAPTER_WORD.sub("", t)
    t = _NUM_PREFIX.sub("", t)
    return t.casefold().strip(" .:-–—")


def match_key(text: str) -> str:
    """Strict key for manifest chapter matching. Keeps the section number,
    which is the only thing distinguishing "1. Introduction" from
    "8.1 Introduction" in higher schoo.docx. Manifest entries are copied
    verbatim from the source, so exact-after-normalisation is right."""
    t = _WS.sub(" ", (text or "").replace("\xa0", " ")).strip()
    return t.casefold().strip(" .:-–—")


def _para_text(p: ET.Element) -> str:
    """Visible text of a <w:p>, honouring tabs and line breaks. Skips text
    inside deleted revisions and field instructions (a TOC field's
    instrText would otherwise leak the whole table of contents in)."""
    out = []
    for node in p.iter():
        tag = node.tag
        if tag == W + "t":
            out.append(node.text or "")
        elif tag == W + "tab":
            out.append("\t")
        elif tag in (W + "br", W + "cr"):
            out.append("\n")
    return _WS.sub(" ", "".join(out).replace("\xa0", " ")).strip()


def _iter_body(body: ET.Element):
    """Yield paragraphs and tables in document order, descending through
    the structured-document-tag and revision wrappers Word inserts (a
    generated table of contents lives inside <w:sdt>, so an iteration
    that only looks at direct children silently drops content)."""
    for el in body:
        tag = el.tag
        if tag in (W + "p", W + "tbl"):
            yield el
        elif tag in (W + "sdt", W + "ins", W + "customXml"):
            content = el.find(W + "sdtContent")
            yield from _iter_body(content if content is not None else el)


# ─────────────────────────────────────────────────────────────────
#  Style + numbering maps
# ─────────────────────────────────────────────────────────────────

def _style_map(z: zipfile.ZipFile) -> dict:
    """styleId → lowercased style name. The numeric ids differ per
    document, so nothing may key off them directly."""
    out = {}
    try:
        root = ET.fromstring(z.read("word/styles.xml"))
    except KeyError:
        return out
    for style in root.iter(W + "style"):
        sid = style.get(W + "styleId")
        name = style.find(W + "name")
        if sid is not None and name is not None:
            out[sid] = (name.get(W + "val") or "").strip().lower()
    return out


def _heading_level(p: ET.Element, styles: dict) -> int | None:
    """Outline level of a paragraph, or None when it isn't a heading."""
    pPr = p.find(W + "pPr")
    if pPr is None:
        return None
    pStyle = pPr.find(W + "pStyle")
    if pStyle is None:
        return None
    name = styles.get(pStyle.get(W + "val") or "", "")
    if "heading" not in name:
        return None
    digits = re.sub(r"\D", "", name)
    return int(digits) if digits else 9


def _list_info(p: ET.Element):
    """(numId, ilvl) when the paragraph is a list item, else None."""
    pPr = p.find(W + "pPr")
    if pPr is None:
        return None
    numPr = pPr.find(W + "numPr")
    if numPr is None:
        return None
    ilvl = numPr.find(W + "ilvl")
    numid = numPr.find(W + "numId")
    return (
        (numid.get(W + "val") if numid is not None else "0"),
        int((ilvl.get(W + "val") if ilvl is not None else 0) or 0),
    )


def _ordered_num_ids(z: zipfile.ZipFile) -> set:
    """numIds whose level-0 format is decimal — i.e. numbered, not bulleted.
    Lets the renderer pick <ol> vs <ul> instead of guessing."""
    out = set()
    try:
        root = ET.fromstring(z.read("word/numbering.xml"))
    except KeyError:
        return out
    abstract_ordered = set()
    for an in root.iter(W + "abstractNum"):
        aid = an.get(W + "abstractNumId")
        for lvl in an.iter(W + "lvl"):
            if (lvl.get(W + "ilvl") or "0") != "0":
                continue
            fmt = lvl.find(W + "numFmt")
            if fmt is not None and (fmt.get(W + "val") or "") not in ("bullet", "none"):
                abstract_ordered.add(aid)
    for num in root.iter(W + "num"):
        ref = num.find(W + "abstractNumId")
        if ref is not None and ref.get(W + "val") in abstract_ordered:
            out.add(num.get(W + "numId"))
    return out


# ─────────────────────────────────────────────────────────────────
#  Tables
# ─────────────────────────────────────────────────────────────────

def _table_rows(tbl: ET.Element) -> list:
    """<w:tbl> → list of row lists. Horizontal spans repeat their value
    across the spanned columns and vertically-merged continuation cells
    inherit from the row above, so every row comes out the same width and
    the frontend can render a plain <table> with no span logic."""
    rows, carry = [], {}
    for tr in tbl.findall(W + "tr"):
        row, col = [], 0
        for tc in tr.findall(W + "tc"):
            tcPr = tc.find(W + "tcPr")
            span = 1
            merged_continuation = False
            if tcPr is not None:
                grid = tcPr.find(W + "gridSpan")
                if grid is not None:
                    span = int(grid.get(W + "val") or 1)
                vm = tcPr.find(W + "vMerge")
                if vm is not None and (vm.get(W + "val") or "continue") == "continue":
                    merged_continuation = True
            text = "\n".join(
                t for t in (_para_text(p) for p in tc.findall(W + "p")) if t
            )
            if merged_continuation and not text:
                text = carry.get(col, "")
            else:
                carry[col] = text
            for _ in range(span):
                row.append(text)
                col += 1
        rows.append(row)
    width = max((len(r) for r in rows), default=0)
    for r in rows:
        r.extend([""] * (width - len(r)))
    return [r for r in rows if any(c.strip() for c in r)]


def _looks_like_glance(rows: list) -> bool:
    """A compact 2-column spec sheet — "Particular | Information",
    "Category | Information", "Topic | Details" — that belongs in the
    guide header rather than the body.

    Strictly 2 columns on purpose. The 3-column tables that open several
    of these documents ("Option | Best For | Leads to", "Field of Study |
    Popular Courses | Duration") are real body content and lifting them
    into the header would silently delete them from the chapter."""
    if not (2 <= len(rows) <= 14):
        return False
    if len(rows[0]) != 2:
        return False
    return all(len(c) <= 200 for r in rows for c in r)


# ─────────────────────────────────────────────────────────────────
#  Block builder
# ─────────────────────────────────────────────────────────────────

class _Blocks:
    """Accumulates blocks for one section, merging consecutive list items
    and folding paragraphs that follow a callout heading into its body."""

    def __init__(self, ordered_num_ids: set):
        self.blocks: list = []
        self._ordered = ordered_num_ids
        self._list_key = None          # (numId, ilvl) of the run being built
        self._open_callout = None      # callout awaiting its body text

    # -- lists ------------------------------------------------------
    def add_list_item(self, key, text):
        if not text:
            return
        self._open_callout = None
        num_id, ilvl = key
        if self._list_key is not None and self._list_key[0] == num_id:
            block = self.blocks[-1]
            if ilvl > self._list_key[1] and block["items"]:
                # Nest under the previous item rather than flattening.
                prev = block["items"][-1]
                if isinstance(prev, str):
                    prev = {"text": prev, "children": []}
                    block["items"][-1] = prev
                prev.setdefault("children", []).append(text)
                return
            block["items"].append(text)
            self._list_key = (num_id, ilvl)
            return
        block = {"t": "list", "items": [text]}
        if num_id in self._ordered:
            block["ordered"] = True
        self.blocks.append(block)
        self._list_key = key

    # -- paragraphs -------------------------------------------------
    def add_paragraph(self, text):
        self._list_key = None
        if not text:
            return
        if self._open_callout is not None:
            # The source documents put the callout's prose in the
            # paragraphs AFTER the "📌 Remember" heading. The previous
            # importer left `body` empty and leaked the prose into a
            # sibling <p>; absorb it instead.
            existing = self._open_callout["body"]
            self._open_callout["body"] = f"{existing}\n\n{text}".strip() if existing else text
            return
        for url in URL_RE.findall(text):
            if text.strip() == url:
                self.blocks.append({"t": "ref", "url": url, "label": self._last_label()})
                return
        self.blocks.append({"t": "p", "text": text})

    def _last_label(self):
        for block in reversed(self.blocks):
            if block.get("t") == "p":
                return block["text"][:160]
        return ""

    # -- headings / callouts / tables --------------------------------
    def add_callout(self, title):
        self._list_key = None
        stripped = title
        for marker in CALLOUT_MARKERS:
            stripped = stripped.replace(marker, "")
        block = {"t": "tip", "title": stripped.strip(" :-–—") or "Remember", "body": ""}
        self.blocks.append(block)
        self._open_callout = block

    def add_subheading(self, text, level):
        self._list_key = None
        self._open_callout = None
        if text:
            self.blocks.append({"t": "h3", "text": text, "level": level})

    def add_table(self, rows):
        self._list_key = None
        self._open_callout = None
        if rows:
            self.blocks.append({"t": "table", "rows": rows})

    def finish(self):
        # A callout that never received body prose was being used as a
        # plain heading (its content is the table or list that follows).
        # Demote it to h3 rather than emitting an empty tip card — the
        # previous importer shipped 96 tips with empty bodies.
        out = []
        for b in self.blocks:
            if b.get("t") == "tip" and not (b.get("body") or "").strip():
                if b.get("title"):
                    out.append({"t": "h3", "text": b["title"], "level": 3})
                continue
            out.append(b)
        return out


# ─────────────────────────────────────────────────────────────────
#  Main entry point
# ─────────────────────────────────────────────────────────────────

def parse_docx(fileobj, spec: dict | None = None) -> dict:
    """Parse one .docx into {title, glance, chapters, sections, stats}.

    `spec` is the manifest entry for this document. Keys read here:
      chapter_titles   ordered list of the headings that open each chapter.
                       Either a plain string (used as both the match and
                       the chapter title) or {"match": ..., "title": ...}
                       when the source has no chapter-level heading and
                       the chapter must be opened on its first subsection
                       — e.g. New DOCX Document.docx chapter 8 begins at
                       "8.1 Overview of Fees, Scholarships & Financial
                       Assistance" with no "8: Fees" heading above it.
                       Absent/empty → a flat guide with no chapters.
      section_levels   outline levels treated as section headings
                       (default [1, 2]). Deeper headings become `h3`
                       blocks inside the current section.
      lift_glance      lift a leading spec table into `glance` (default True).
      drop_toc         strip the leading table of contents (default True).
    """
    spec = spec or {}
    raw = fileobj.read() if hasattr(fileobj, "read") else fileobj
    sha = hashlib.sha256(raw).hexdigest()

    import io
    z = zipfile.ZipFile(io.BytesIO(raw))
    styles = _style_map(z)
    ordered_ids = _ordered_num_ids(z)
    body = ET.fromstring(z.read("word/document.xml")).find(W + "body")
    elements = list(_iter_body(body))

    section_levels = set(spec.get("section_levels") or [1, 2])

    # chapter_titles entries are either "Heading text" or
    # {"match": "Heading text", "title": "Display title"}.
    chapter_specs = []
    for entry in spec.get("chapter_titles") or []:
        if isinstance(entry, dict):
            raw_match = entry.get("match") or entry.get("title") or ""
            display = entry.get("title") or raw_match
        else:
            raw_match = display = entry
        key = match_key(raw_match)
        if key:
            chapter_specs.append((key, display))
    chapter_lookup = {key: i for i, (key, _) in enumerate(chapter_specs)}

    # Pass 1 — every heading in the document, and where real chapter
    # content begins, for TOC detection.
    #
    # The TOC-drop window can't be "before the first STYLED heading": a
    # document's title/subtitle are themselves headings (secondary
    # school.docx opens on a "CAREER GUIDANCE" / "Secondary School
    # (Classes 9-10)" heading pair, immediately followed by 13 lines of
    # plain, UNstyled paragraphs restating every chapter title — the
    # actual TOC), and some documents open with several empty decorative
    # headings before the title. The reliable boundary is the first
    # occurrence of a heading whose text matches a DECLARED chapter —
    # TOC entries are never heading-styled themselves, so they can never
    # be mistaken for that boundary.
    # Seed with the manifest's own chapter display titles too: chapters
    # opened on a {match, title} entry (no chapter-level heading exists in
    # the source at all) still get restated in the document's own TOC,
    # under a title that will never otherwise appear as a real heading.
    heading_keys = {norm(title) for _, title in chapter_specs}
    # A handful of documents restate a chapter in the TOC under wording
    # that never appears as a real heading anywhere in the body (an
    # abbreviated or lightly-retyped title) — e.g. New DOCX Document.docx's
    # TOC says "4. Regulatory Bodies & Accreditation" where the real
    # heading is "4: Regulatory Bodies AND Accreditation". Rather than
    # fuzzy-matching (risking eating real prose), each is named explicitly
    # per document after a human read the dry-run diff.
    heading_keys |= {norm(t) for t in (spec.get("toc_extra") or [])}
    heading_keys |= _TOC_FILLER_WORDS
    first_heading_idx = toc_window_end = None
    for i, el in enumerate(elements):
        if el.tag != W + "p":
            continue
        if _heading_level(el, styles) is None:
            continue
        text = _para_text(el)
        if not text:
            continue
        heading_keys.add(norm(text))
        if first_heading_idx is None:
            first_heading_idx = i
        if toc_window_end is None and match_key(text) in chapter_lookup:
            toc_window_end = i
    if first_heading_idx is None:
        first_heading_idx = 0
    if toc_window_end is None:
        toc_window_end = first_heading_idx

    stats = {"toc_dropped": 0, "chapters_matched": 0, "empty_headings": 0}

    doc_title = spec.get("title") or ""
    glance: list = []
    chapters: list = []
    sections: list = []
    current = None   # the section being filled
    seen_chapter = set()

    def open_section(title, level, chapter_index):
        nonlocal current
        close_section()
        kind, audience = "content", "student"
        for candidate, pattern in KIND_PATTERNS:
            if pattern.search(title or ""):
                kind = candidate
                break
        # A section with no classifier of its own inherits its chapter's,
        # so every subsection of "12: Activities & Worksheets" lands in the
        # Worksheets tab and every subsection of "10: Parents & Guardians
        # Guide" in the parents tab — the sub-headings themselves are
        # named things like "Activity 1" or "Supporting Your Child" and
        # would otherwise classify as plain content.
        if kind == "content" and chapter_index is not None and chapters:
            kind = chapters[chapter_index].get("_kind", "content")
        if kind == "parent_guide":
            audience = "parent"
        current = {
            "title": title,
            "level": level,
            "kind": kind,
            "audience": audience,
            "chapter_index": chapter_index,
            "_acc": _Blocks(ordered_ids),
        }

    def close_section():
        nonlocal current
        if current is None:
            return
        blocks = current.pop("_acc").finish()
        # A titled-but-empty section renders as a blank card in the
        # reader — most often the document's own top-level title heading,
        # whose "body" was nothing but the table of contents the TOC-drop
        # logic above just correctly removed. Require actual content.
        if blocks:
            current["blocks"] = blocks
            sections.append(current)
        current = None

    chapter_index = None
    drop_toc = spec.get("drop_toc", True)
    lift_glance = spec.get("lift_glance", True)

    for i, el in enumerate(elements):
        # ── tables ───────────────────────────────────────────────
        if el.tag == W + "tbl":
            rows = _table_rows(el)
            if not rows:
                continue
            # Only the FIRST chapter may donate a header spec sheet; the
            # later "Quick Information" tables in career-guidance-after-
            # class-10 are per-stream and belong where they are.
            if (
                lift_glance
                and not glance
                and (chapter_index is None or chapter_index == 0)
                and _looks_like_glance(rows)
            ):
                glance = rows
                continue
            if current is None:
                open_section("", 1, chapter_index)
            current["_acc"].add_table(rows)
            continue

        text = _para_text(el)
        level = _heading_level(el, styles)

        # ── headings ─────────────────────────────────────────────
        if level is not None:
            if not text:
                stats["empty_headings"] += 1
                continue
            # Chapter boundary?
            idx = chapter_lookup.get(match_key(text))
            if idx is not None and idx not in seen_chapter:
                seen_chapter.add(idx)
                close_section()
                display = chapter_specs[idx][1]
                chapter_index = len(chapters)
                ch_kind = "content"
                for candidate, pattern in KIND_PATTERNS:
                    if pattern.search(display):
                        ch_kind = candidate
                        break
                chapters.append({
                    "number": len(chapters) + 1,
                    "title": display,
                    "_kind": ch_kind,
                })
                stats["chapters_matched"] += 1
                # A dict entry means this heading is really the chapter's
                # first subsection, so keep it as a section too.
                if match_key(display) != match_key(text):
                    open_section(text, level, chapter_index)
                continue

            if text.lstrip().startswith(CALLOUT_MARKERS):
                if current is None:
                    open_section("", level, chapter_index)
                current["_acc"].add_callout(text)
                continue

            if level in section_levels:
                open_section(text, level, chapter_index)
            else:
                if current is None:
                    open_section("", level, chapter_index)
                current["_acc"].add_subheading(text, level)
            continue

        # ── body text ────────────────────────────────────────────
        if not text:
            continue

        # Table of contents: entries sit before real chapter content
        # starts and restate the document's own heading texts. Bounding
        # the window at `toc_window_end` rather than `first_heading_idx`
        # matters because a title/subtitle pair is itself heading-styled
        # and sits BEFORE the injected TOC in some documents. Anything
        # else in that window (a genuine intro paragraph) is kept.
        if drop_toc and i < toc_window_end and norm(text) in heading_keys:
            stats["toc_dropped"] += 1
            continue
        if drop_toc and norm(text) in ("references", "contents", "table of contents"):
            if _list_info(el) is not None or i < toc_window_end:
                stats["toc_dropped"] += 1
                continue
        if drop_toc and i < toc_window_end and _is_toc_blob(text, heading_keys):
            stats["toc_dropped"] += 1
            continue

        if current is None:
            open_section("", 1, chapter_index)

        info = _list_info(el)
        if info is not None:
            current["_acc"].add_list_item(info, text)
        else:
            current["_acc"].add_paragraph(text)

    close_section()

    for ch in chapters:
        ch["kind"] = ch.pop("_kind", "content")

    stats.update(
        sections=len(sections),
        chapters=len(chapters),
        blocks=sum(len(s["blocks"]) for s in sections),
        chapters_declared=len(chapter_specs),
        chars=sum(
            len(b.get("text", "")) + len(b.get("body", ""))
            for s in sections for b in s["blocks"]
        ),
    )
    block_types: dict = {}
    for s in sections:
        for b in s["blocks"]:
            block_types[b["t"]] = block_types.get(b["t"], 0) + 1
    stats["block_types"] = block_types

    return {
        "title": doc_title,
        "sha256": sha,
        "glance": glance,
        "chapters": chapters,
        "sections": sections,
        "stats": stats,
    }


# ─────────────────────────────────────────────────────────────────
#  Structure proposal (authoring aid for the manifest)
# ─────────────────────────────────────────────────────────────────

def propose_structure(fileobj) -> dict:
    """Suggest a `chapter_titles` list for the manifest.

    Heuristic: headings whose leading number continues the sequence
    1, 2, 3, … Enumerated lists inside a chapter restart at 1 and are
    therefore rejected. This is right on most of the corpus and wrong on
    the documents that drop or omit chapter numbers, which is exactly why
    the output is a proposal for a human to edit rather than something
    the importer trusts. Never called during a real import.
    """
    raw = fileobj.read() if hasattr(fileobj, "read") else fileobj
    import io
    z = zipfile.ZipFile(io.BytesIO(raw))
    styles = _style_map(z)
    body = ET.fromstring(z.read("word/document.xml")).find(W + "body")

    headings = []
    for el in _iter_body(body):
        if el.tag != W + "p":
            continue
        level = _heading_level(el, styles)
        if level is None:
            continue
        text = _para_text(el)
        if text:
            headings.append((level, text))

    numbered = re.compile(r"^(\d{1,2})\s*[:.\)]\s*(?!\d)")
    proposed, want = [], 1
    for level, text in headings:
        m = numbered.match(text)
        if m and int(m.group(1)) == want:
            proposed.append({"level": level, "title": text})
            want += 1

    levels = sorted({h[0] for h in headings})
    return {
        "chapter_titles": [p["title"] for p in proposed],
        "chapter_levels": sorted({p["level"] for p in proposed}),
        "heading_levels_present": levels,
        "heading_count": len(headings),
    }
