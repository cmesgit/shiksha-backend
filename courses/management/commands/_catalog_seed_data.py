# PLACEMENT: backend/courses/management/commands/_catalog_seed_data.py
#
# Canonical, single-source-of-truth data for the Phase-D catalog seed
# commands. Django's management-command loader ignores modules whose name
# starts with "_", so this is a plain importable data module, NOT a command.
#
# Keeping categories, boards, competitive courses and featured cards in ONE
# place is deliberate: the 2026-07-27 near-miss (1,055 would-be duplicate rows)
# came from two code paths disagreeing about how a row was identified. Here the
# category slugs referenced by create_competitive_courses and seed_featured_cards
# are the very same strings seed_course_categories writes, so they cannot drift.
#
# Source data:
#   - shiksha-frontend/src/components/home/homeData.js  (FEATURED_COURSES)
#   - shiksha-frontend/src/components/Courses.jsx        (BOARD_OPTIONS)

# ---------------------------------------------------------------------------
# 1. CourseCategory rows.
#
# `group` is literally the homepage tab id the frontend already filters on
# (COURSE_TABS in homeData.js: boards / class8-12 / competitive).
#   - boards      → 2 generic buckets mirroring BOARD_GROUPS (central/state).
#   - class8-12   → one category per class level 8..12.
#   - competitive → the 7 exam tracks, one per competitive FEATURED_COURSES card.
# ---------------------------------------------------------------------------
CATEGORY_SEED = [
    # --- boards group ---
    # The slug stays "central-boards" even though the name now reads
    # "National". It is matched on by seed_course_categories (so a changed
    # slug would CREATE a duplicate rather than update this row) and it is a
    # public `?category=` filter value — see courses/views.py's catalog
    # filter. Same labels-only rule as Board.TYPE_CHOICES.
    {"slug": "central-boards", "name": "National Boards", "group": "boards",
     "icon": "book", "blurb": "National curriculum boards (CBSE, ICSE and more).",
     "display_order": 0},
    {"slug": "state-boards", "name": "State Boards", "group": "boards",
     "icon": "book", "blurb": "Regional curriculum boards (MBSE and more).",
     "display_order": 1},

    # --- class8-12 group ---
    {"slug": "class-8", "name": "Class 8", "group": "class8-12",
     "icon": "book", "blurb": "Class 8 foundation courses.", "display_order": 10},
    {"slug": "class-9", "name": "Class 9", "group": "class8-12",
     "icon": "book", "blurb": "Class 9 foundation courses.", "display_order": 11},
    {"slug": "class-10", "name": "Class 10", "group": "class8-12",
     "icon": "book", "blurb": "Class 10 board-prep courses.", "display_order": 12},
    {"slug": "class-11", "name": "Class 11", "group": "class8-12",
     "icon": "book", "blurb": "Class 11 Science / Commerce / Arts.", "display_order": 13},
    {"slug": "class-12", "name": "Class 12", "group": "class8-12",
     "icon": "book", "blurb": "Class 12 Science / Commerce / Arts.", "display_order": 14},

    # --- competitive group (one per competitive card in homeData.js) ---
    {"slug": "neet", "name": "NEET", "group": "competitive",
     "icon": "flask", "blurb": "Medical entrance (NEET).", "display_order": 20},
    {"slug": "upsc", "name": "UPSC & Civil Services", "group": "competitive",
     "icon": "book", "blurb": "UPSC and state civil-services exams.", "display_order": 21},
    {"slug": "jee", "name": "IIT-JEE", "group": "competitive",
     "icon": "calc", "blurb": "Engineering entrance (JEE Main & Advanced).", "display_order": 22},
    # Renamed from "SSC & Banking" now that banking has its own row below.
    # The SLUG stays "ssc" — it is the public `?category=` filter value, so
    # changing it would create a duplicate category and break saved links.
    {"slug": "ssc", "name": "SSC Exams", "group": "competitive",
     "icon": "book", "blurb": "SSC CGL, CHSL, MTS and allied recruitment exams.", "display_order": 23},
    {"slug": "defence", "name": "Defence Exams", "group": "competitive",
     "icon": "book", "blurb": "NDA, CDS and allied defence exams.", "display_order": 24},
    {"slug": "ca", "name": "CA", "group": "competitive",
     "icon": "calc", "blurb": "Chartered Accountancy programme.", "display_order": 25},
    {"slug": "olympiad", "name": "Olympiad & Foundation", "group": "competitive",
     "icon": "flask", "blurb": "Olympiads and early foundation tracks.", "display_order": 26},

    # --- competitive, added 2026-09-11 to fill out the exam menu ---
    {"slug": "banking", "name": "Banking Exams", "group": "competitive",
     "icon": "calc", "blurb": "IBPS, SBI and RBI recruitment exams.", "display_order": 27},
    {"slug": "railways", "name": "Railway Exams", "group": "competitive",
     "icon": "book", "blurb": "RRB NTPC, Group D and ALP.", "display_order": 28},
    {"slug": "state-psc", "name": "State PSC", "group": "competitive",
     "icon": "book", "blurb": "State public service commission exams.", "display_order": 29},
    {"slug": "clat", "name": "CLAT & Law", "group": "competitive",
     "icon": "book", "blurb": "CLAT, AILET and other law entrances.", "display_order": 30},
    {"slug": "cat", "name": "CAT & MBA", "group": "competitive",
     "icon": "calc", "blurb": "CAT, XAT and MBA entrance exams.", "display_order": 31},
    {"slug": "gate", "name": "GATE", "group": "competitive",
     "icon": "flask", "blurb": "Graduate Aptitude Test in Engineering.", "display_order": 32},
    {"slug": "ctet", "name": "CTET & TET", "group": "competitive",
     "icon": "book", "blurb": "Central and state teacher eligibility tests.", "display_order": 33},
    {"slug": "ugc-net", "name": "UGC NET", "group": "competitive",
     "icon": "book", "blurb": "UGC NET for lectureship and JRF.", "display_order": 34},
]

# ---------------------------------------------------------------------------
# 2. Board rows — the school-education half of the navbar mega-menu.
#
# `slug` == the frontend's board `id` and is a WIRE VALUE: it is lowercased
# into the public `?group=`/`?board=` query params and into saved homepage CMS
# `link_state` rows. Never change a slug to match a renamed board; rename the
# `name` only. (Same rule as Board.TYPE_CHOICES — see courses/models.py.)
#
# CBSE and MBSE are the only two LIVE boards and already exist as real rows, so
# they are flagged pre_existing and seed_boards will never re-create them.
# Everything else is is_active=False → the nav renders an inert "Coming Soon"
# row and the catalog renders a locked chip with a "Notify me" capture
# (BoardNotifyRequest).
#
# ── Why the STATE names carry their state ──────────────────────────────────
# MBSE (Mizoram) and MBOSE (Meghalaya) are one letter apart, and BSEB/BSEH/
# BSEAP are barely more distinguishable at a glance. An abbreviation alone
# does not identify a board to the parent reading this menu, and the mobile
# drawer FLATTENS every board tab into one list (Navbar.jsx does
# `cat.tabs.flatMap(t => t.links)`), so the tab heading is not there to
# disambiguate either. The state is therefore part of the name.
#
# ⚠ The " · " separator is load-bearing: courses/views.py's
# `_board_class_links` splits on it to keep per-class rows short
# ("Class 9 · MBSE", not "Class 9 · MBSE · Mizoram"). Keep the abbreviation
# FIRST and use " · " as the separator, or class labels grow and re-break the
# nav-panel wrapping fixed on 2026-08-27.
#
# ── What is deliberately NOT here ──────────────────────────────────────────
# * IB and Cambridge/CAIE — international boards, not Indian ones. This menu
#   is scoped to India.
# * AISSCE — that is the NAME OF CBSE'S CLASS 12 EXAM, not a board. Listing it
#   beside CBSE advertised the same board twice.
# * ICSE — also not a board. The board is CISCE; ICSE and ISC are its two
#   certificates. A separate "icse" row would have sat next to the real
#   `cisce` row as a silent duplicate.
# * COHSEM — Manipur's higher-secondary council, a second body for a state
#   BOSEM already covers. This list is one row per state, the same way Assam's
#   SEBA+AHSEC collapse to ASSEB and Odisha's BSE+CHSE collapse to BSE Odisha.
# * Arunachal Pradesh, Sikkim, Puducherry, Chandigarh, Ladakh, Andaman &
#   Nicobar, Dadra & Nagar Haveli, Lakshadweep — these have no school board of
#   their own; their schools sit under CBSE. A row for them would promise a
#   syllabus that does not exist.
# ---------------------------------------------------------------------------
BOARD_SEED = [
    # slug, name, board_type, is_active, pre_existing

    # --- National boards (board_type CENTRAL, displayed as "National") ---
    # India has exactly three national-level school boards. No state suffix
    # here: their reach IS national, so qualifying them would be noise.
    ("cbse", "CBSE", "CENTRAL", True, True),
    ("cisce", "CISCE", "CENTRAL", False, True),
    ("nios", "NIOS", "CENTRAL", False, False),

    # --- State boards, MBSE first (the live one), then by state name ---
    ("mbse", "MBSE · Mizoram", "STATE", True, True),
    ("bseap", "BSEAP · Andhra Pradesh", "STATE", False, False),
    ("asseb", "ASSEB · Assam", "STATE", False, False),
    ("bseb", "BSEB · Bihar", "STATE", False, False),
    ("cgbse", "CGBSE · Chhattisgarh", "STATE", False, False),
    ("dbse", "DBSE · Delhi", "STATE", False, False),
    ("gbshse", "GBSHSE · Goa", "STATE", False, False),
    ("gseb", "GSEB · Gujarat", "STATE", False, False),
    ("bseh", "BSEH · Haryana", "STATE", False, False),
    ("hpbose", "HPBOSE · Himachal Pradesh", "STATE", False, False),
    ("jkbose", "JKBOSE · Jammu & Kashmir", "STATE", False, False),
    ("jac", "JAC · Jharkhand", "STATE", False, False),
    ("kseab", "KSEAB · Karnataka", "STATE", False, False),
    ("kbpe", "KBPE · Kerala", "STATE", False, False),
    ("mpbse", "MPBSE · Madhya Pradesh", "STATE", False, False),
    ("msbshse", "MSBSHSE · Maharashtra", "STATE", False, False),
    ("bosem", "BOSEM · Manipur", "STATE", False, False),
    ("mbose", "MBOSE · Meghalaya", "STATE", False, False),
    ("nbse", "NBSE · Nagaland", "STATE", False, False),
    ("bseodisha", "BSE · Odisha", "STATE", False, False),
    ("pseb", "PSEB · Punjab", "STATE", False, False),
    ("rbse", "RBSE · Rajasthan", "STATE", False, False),
    ("tnbse", "TNBSE · Tamil Nadu", "STATE", False, False),
    ("tsbse", "TSBSE · Telangana", "STATE", False, False),
    ("tbse", "TBSE · Tripura", "STATE", False, False),
    ("upmsp", "UPMSP · Uttar Pradesh", "STATE", False, False),
    ("ubse", "UBSE · Uttarakhand", "STATE", False, False),
    ("wbbse", "WBBSE · West Bengal", "STATE", False, False),
]

# Changes seed_boards must make to rows that ALREADY EXIST. Kept separate, and
# applied only under --apply-curation, because seed_boards' safety story is
# "never mutate an existing board" — that guard is what stopped the 2026-07-27
# duplicate-CBSE incident and must not be softened into a general update pass.
# This is an explicit, reviewable allow-list keyed on slug: a board not named
# here can never be touched, whatever BOARD_SEED says.
BOARD_CURATION = [
    # slug, field, old value (asserted before writing), new value, why
    ("mbse", "name", "MBSE", "MBSE · Mizoram",
     "MBSE and Meghalaya's MBOSE are indistinguishable at a glance once the "
     "mobile drawer flattens both board tabs into one list."),
    ("cisce", "is_active", True, False,
     "CISCE owns ZERO courses, so the live row was a clickable nav entry that "
     "landed on an empty catalog. Coming Soon is the honest state, and it "
     "turns the row into a Notify-me capture instead of a dead end."),
]

# ---------------------------------------------------------------------------
# 3. The 7 competitive courses (kind=COACHING, status=COMING_SOON), carrying
# the marketing copy + tutor names already written in FEATURED_COURSES.
# `slug` is fixed (not auto-derived) so re-runs match deterministically, and
# `category` is the CourseCategory.slug above.
# ---------------------------------------------------------------------------
COMPETITIVE_COURSE_SEED = [
    {"slug": "neet-preparation", "title": "NEET Preparation", "category": "neet",
     "level": "Medical", "tutor": "Dr. D. Ralte",
     "fact": "Live + Recorded · Launching soon"},
    {"slug": "upsc-civil-services", "title": "UPSC & Civil Services", "category": "upsc",
     "level": "Civil Services", "tutor": "K. Zoramthanga",
     "fact": "Live + Recorded · Launching soon"},
    {"slug": "iit-jee-preparation", "title": "IIT-JEE Preparation", "category": "jee",
     "level": "Engineering", "tutor": "A. Sharma",
     "fact": "Live + Recorded · Launching soon"},
    {"slug": "government-exams", "title": "Government Exams", "category": "ssc",
     "level": "SSC · Banking", "tutor": "T. Lalhmingthanga",
     "fact": "Live + Recorded · Launching soon"},
    {"slug": "defence-exams", "title": "Defence Exams", "category": "defence",
     "level": "NDA · CDS", "tutor": "Maj. R. Singh (Retd.)",
     "fact": "Live + Recorded · Launching soon"},
    {"slug": "ca-program", "title": "CA Program", "category": "ca",
     "level": "Accountancy", "tutor": "CA V. Malsawma",
     "fact": "Live + Recorded · Launching soon"},
    {"slug": "olympiad-foundation", "title": "Olympiad & Foundation", "category": "olympiad",
     "level": "Olympiads", "tutor": "R. Vanlalhriati",
     "fact": "Live + Recorded · Launching soon"},

    # --- added 2026-09-11 ---------------------------------------------------
    # Titles are deliberately SHORT. These render as nav rows inside a
    # `minmax(190px, 1fr)` column that also has to fit a "Coming Soon" chip
    # (SiteNav.css) — "SSC · CGL, CHSL & MTS" wraps to two lines there, so the
    # detail lives in the category blurb and the course description instead.
    #
    # `tutor` is left blank on every row below: unlike the original seven,
    # these have no mentor assigned yet, and inventing a name would put a
    # fictional person's byline on a public catalog card.
    #
    # ⚠ "Government Exams" (slug government-exams) OVERLAPS the SSC / Banking /
    # Railway rows added here. It is kept anyway because it backs homepage
    # ShowcaseCourse order 14 — retiring it would blank a live homepage card.
    # Worth a content decision later; not one to make silently here.
    {"slug": "ssc-exams", "title": "SSC Exams", "category": "ssc",
     "level": "SSC", "tutor": "",
     "fact": "CGL · CHSL · MTS · Launching soon"},
    {"slug": "banking-exams", "title": "Banking Exams", "category": "banking",
     "level": "Banking", "tutor": "",
     "fact": "IBPS · SBI · RBI · Launching soon"},
    {"slug": "railway-exams", "title": "Railway Exams", "category": "railways",
     "level": "Railways", "tutor": "",
     "fact": "RRB NTPC · Group D · ALP · Launching soon"},
    {"slug": "state-psc", "title": "State PSC", "category": "state-psc",
     "level": "State Services", "tutor": "",
     "fact": "State civil services · Launching soon"},
    {"slug": "clat-law", "title": "CLAT & Law", "category": "clat",
     "level": "Law", "tutor": "",
     "fact": "CLAT · AILET · Launching soon"},
    {"slug": "cat-mba", "title": "CAT & MBA", "category": "cat",
     "level": "Management", "tutor": "",
     "fact": "CAT · XAT · Launching soon"},
    {"slug": "gate", "title": "GATE", "category": "gate",
     "level": "Engineering", "tutor": "",
     "fact": "All branches · Launching soon"},
    {"slug": "ctet-tet", "title": "CTET & TET", "category": "ctet",
     "level": "Teaching", "tutor": "",
     "fact": "CTET · State TET · Launching soon"},
    {"slug": "ugc-net", "title": "UGC NET", "category": "ugc-net",
     "level": "Lectureship", "tutor": "",
     "fact": "Paper I & II · Launching soon"},
]

# ---------------------------------------------------------------------------
# 4. The 18 homepage FEATURED_COURSES cards → ShowcaseCourse rows.
#
# `order` mirrors the homeData.js array index. Each card carries only curation
# fields (the price/title/thumbnail are derived server-side from the target by
# PublicFeaturedView) plus a `target` describing what it points at:
#
#   {"academic": (class_level, stream_or_None)}  → real CBSE Course (course FK)
#   {"competitive": "<course-slug>"}             → competitive Course (course FK)
#   {"board": "<board-slug>"}                    → Board (board FK), explore card
#
# `academic` cards are matched to the already-live CBSE course for that
# class+stream (never created here — that's import_static_course_content's job).
# ---------------------------------------------------------------------------
CLASS_FACT = "1 Year · Online · Full access"

FEATURED_CARD_SEED = [
    # order 0-8: Class 8-12 (academic, CBSE)
    {"order": 0, "level_label": "Foundation", "ribbon": "", 
     "fact_line": CLASS_FACT, "gradient_css": "rgba(15,157,107,0.72),rgba(11,91,62,0.88)",
     "icon": "book", "categories": ["class8-12"], "target": {"academic": (8, None)}},
    {"order": 1, "level_label": "Foundation", "ribbon": "", 
     "fact_line": CLASS_FACT, "gradient_css": "rgba(20,184,160,0.72),rgba(11,91,62,0.88)",
     "icon": "book", "categories": ["class8-12"], "target": {"academic": (9, None)}},
    {"order": 2, "level_label": "Foundation", "ribbon": "Bestseller", 
     "fact_line": CLASS_FACT, "gradient_css": "rgba(255,178,29,0.72),rgba(242,140,15,0.88)",
     "icon": "book", "categories": ["class8-12"], "target": {"academic": (10, None)}},
    {"order": 3, "level_label": "Science", "ribbon": "", 
     "fact_line": CLASS_FACT, "gradient_css": "rgba(255,122,69,0.72),rgba(225,77,42,0.88)",
     "icon": "flask", "categories": ["class8-12"], "target": {"academic": (11, "SCIENCE")}},
    {"order": 4, "level_label": "Commerce", "ribbon": "", 
     "fact_line": CLASS_FACT, "gradient_css": "rgba(124,92,252,0.72),rgba(75,52,199,0.88)",
     "icon": "calc", "categories": ["class8-12"], "target": {"academic": (11, "COMMERCE")}},
    {"order": 5, "level_label": "Arts", "ribbon": "", 
     "fact_line": CLASS_FACT, "gradient_css": "rgba(236,78,134,0.72),rgba(193,58,104,0.88)",
     "icon": "book", "categories": ["class8-12"], "target": {"academic": (11, "ARTS")}},
    {"order": 6, "level_label": "Science", "ribbon": "New", 
     "fact_line": CLASS_FACT, "gradient_css": "rgba(15,157,107,0.72),rgba(20,184,160,0.88)",
     "icon": "flask", "categories": ["class8-12"], "target": {"academic": (12, "SCIENCE")}},
    {"order": 7, "level_label": "Commerce", "ribbon": "", 
     "fact_line": CLASS_FACT, "gradient_css": "rgba(255,178,29,0.72),rgba(224,139,18,0.88)",
     "icon": "calc", "categories": ["class8-12"], "target": {"academic": (12, "COMMERCE")}},
    {"order": 8, "level_label": "Arts", "ribbon": "", 
     "fact_line": CLASS_FACT, "gradient_css": "rgba(59,130,246,0.72),rgba(29,78,216,0.88)",
     "icon": "book", "categories": ["class8-12"], "target": {"academic": (12, "ARTS")}},

    # order 9-10: Boards (explore cards → Board FK)
    {"order": 9, "level_label": "National Board", "ribbon": "Popular", 
     "fact_line": "Expert Faculty · Classes 8–12",
     "gradient_css": "rgba(15,157,107,0.72),rgba(11,91,62,0.88)", "icon": "book",
     "categories": ["boards"], "is_explore_card": True,
     "link_path": "/courses",
     "link_state": {"selectedBoardGroup": "central", "selectedBoard": "cbse"},
     "target": {"board": "cbse"}},
    {"order": 10, "level_label": "Regional", "ribbon": "", 
     "fact_line": "MBSE & more",
     "gradient_css": "rgba(20,184,160,0.72),rgba(11,91,62,0.88)", "icon": "book",
     "categories": ["boards"], "is_explore_card": True,
     "link_path": "/courses",
     "link_state": {"selectedBoardGroup": "state", "selectedBoard": "mbse"},
     "target": {"board": "mbse"}},

    # order 11-17: Competitive (→ competitive Course FK, COMING_SOON)
    {"order": 11, "level_label": "Medical", "ribbon": "Popular", 
     "fact_line": "Live + Recorded · Launching soon", "tutor_name": "Dr. D. Ralte",
     "gradient_css": "rgba(236,78,134,0.72),rgba(193,58,104,0.88)", "icon": "flask",
     "categories": ["competitive"], "target": {"competitive": "neet-preparation"}},
    {"order": 12, "level_label": "Civil Services", "ribbon": "", 
     "fact_line": "Live + Recorded · Launching soon", "tutor_name": "K. Zoramthanga",
     "gradient_css": "rgba(255,178,29,0.72),rgba(242,140,15,0.88)", "icon": "book",
     "categories": ["competitive"], "target": {"competitive": "upsc-civil-services"}},
    {"order": 13, "level_label": "Engineering", "ribbon": "", 
     "fact_line": "Live + Recorded · Launching soon", "tutor_name": "A. Sharma",
     "gradient_css": "rgba(124,92,252,0.72),rgba(75,52,199,0.88)", "icon": "calc",
     "categories": ["competitive"], "target": {"competitive": "iit-jee-preparation"}},
    {"order": 14, "level_label": "SSC · Banking", "ribbon": "", 
     "fact_line": "Live + Recorded · Launching soon", "tutor_name": "T. Lalhmingthanga",
     "gradient_css": "rgba(20,184,160,0.72),rgba(11,91,62,0.88)", "icon": "book",
     "categories": ["competitive"], "target": {"competitive": "government-exams"}},
    {"order": 15, "level_label": "NDA · CDS", "ribbon": "", 
     "fact_line": "Live + Recorded · Launching soon", "tutor_name": "Maj. R. Singh (Retd.)",
     "gradient_css": "rgba(59,130,246,0.72),rgba(29,78,216,0.88)", "icon": "book",
     "categories": ["competitive"], "target": {"competitive": "defence-exams"}},
    {"order": 16, "level_label": "Accountancy", "ribbon": "", 
     "fact_line": "Live + Recorded · Launching soon", "tutor_name": "CA V. Malsawma",
     "gradient_css": "rgba(15,157,107,0.72),rgba(20,184,160,0.88)", "icon": "calc",
     "categories": ["competitive"], "target": {"competitive": "ca-program"}},
    {"order": 17, "level_label": "Olympiads", "ribbon": "", 
     "fact_line": "Live + Recorded · Launching soon", "tutor_name": "R. Vanlalhriati",
     "gradient_css": "rgba(255,122,69,0.72),rgba(225,77,42,0.88)", "icon": "flask",
     "categories": ["competitive"], "target": {"competitive": "olympiad-foundation"}},
]
