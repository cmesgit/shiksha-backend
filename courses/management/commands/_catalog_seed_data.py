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
    {"slug": "central-boards", "name": "Central Boards", "group": "boards",
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
    {"slug": "ssc", "name": "SSC & Banking", "group": "competitive",
     "icon": "book", "blurb": "SSC, banking and government recruitment exams.", "display_order": 23},
    {"slug": "defence", "name": "Defence Exams", "group": "competitive",
     "icon": "book", "blurb": "NDA, CDS and allied defence exams.", "display_order": 24},
    {"slug": "ca", "name": "CA", "group": "competitive",
     "icon": "calc", "blurb": "Chartered Accountancy programme.", "display_order": 25},
    {"slug": "olympiad", "name": "Olympiad & Foundation", "group": "competitive",
     "icon": "flask", "blurb": "Olympiads and early foundation tracks.", "display_order": 26},
]

# ---------------------------------------------------------------------------
# 2. Board rows, transcribed 1:1 from BOARD_OPTIONS in Courses.jsx.
#
# `slug` == the frontend's board `id` (the key Phase-E will match on, per the
# plan: "Match boards by slug, not display name"). CBSE + MBSE are the only two
# live boards today and are almost certainly already present as real rows
# (import_static_course_content requires them); they are flagged pre_existing so
# seed_boards never risks a duplicate. Everything else is inactive → the public
# site renders it "Coming Soon".
# ---------------------------------------------------------------------------
BOARD_SEED = [
    # slug, name, board_type, is_active, pre_existing
    ("cbse", "CBSE", "CENTRAL", True, True),
    ("icse", "ICSE", "CENTRAL", False, False),
    ("ib", "IB", "CENTRAL", False, False),
    ("nios", "NIOS", "CENTRAL", False, False),
    ("aissce", "AISSCE", "CENTRAL", False, False),

    ("mbse", "MBSE", "STATE", True, True),
    ("bseap", "BSEAP", "STATE", False, False),
    ("asseb", "ASSEB", "STATE", False, False),
    ("bseb", "BSEB", "STATE", False, False),
    ("cgbse", "CGBSE", "STATE", False, False),
    ("gbshse", "GBSHSE", "STATE", False, False),
    ("gseb", "GSEB", "STATE", False, False),
    ("bseh", "BSEH", "STATE", False, False),
    ("hpbose", "HPBOSE", "STATE", False, False),
    ("jac", "JAC", "STATE", False, False),
    ("kseab", "KSEAB", "STATE", False, False),
    ("kbpe", "KBPE", "STATE", False, False),
    ("mpbse", "MPBSE", "STATE", False, False),
    ("msbshse", "MSBSHSE", "STATE", False, False),
    ("bosem", "BOSEM", "STATE", False, False),
    ("cohsem", "COHSEM", "STATE", False, False),
    ("mbose", "MBOSE", "STATE", False, False),
    ("nbse", "NBSE", "STATE", False, False),
    ("bseodisha", "BSE Odisha", "STATE", False, False),
    ("pseb", "PSEB", "STATE", False, False),
    ("rbse", "RBSE", "STATE", False, False),
    ("tnbse", "TNBSE", "STATE", False, False),
    ("tsbse", "TSBSE", "STATE", False, False),
    ("tbse", "TBSE", "STATE", False, False),
    ("upmsp", "UPMSP", "STATE", False, False),
    ("ubse", "UBSE", "STATE", False, False),
    ("wbbse", "WBBSE", "STATE", False, False),
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
