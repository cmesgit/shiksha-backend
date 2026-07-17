# Static taxonomy + palette for the Explore document library.
#
# Categories are DB-backed (DocumentCategory, moderator-managed); these lists
# are the *filter* facets that stay stable (subjects, levels, languages, file
# types, sort orders, upload types) plus the deterministic contributor palette.
# The facets endpoint unions the DB categories with these.

# Deterministic avatar/tile palette (shared ShikshaCom greens + accents).
DOC_PALETTE = [
    "#125027", "#004a33", "#0f8f7e", "#1b9c85",
    "#2f6bd8", "#6b58d3", "#c2410c", "#e07900",
]

SUBJECTS = [
    "All", "Computer Science", "Electronics", "Mathematics", "Physics",
    "Chemistry", "Biology", "Economics", "Management", "Civil Engineering",
    "Mechanical Engineering", "Law", "Medicine", "General",
]

LEVELS = [
    "All", "School", "Higher Secondary", "Undergraduate",
    "Postgraduate", "Competitive Exams", "Research",
]

LANGUAGES = ["All", "English", "Hindi", "Marathi", "Bengali", "Tamil", "Other"]

FILETYPES = ["All", "PDF", "DOCX", "PPT", "XLSX", "Image"]

SORTS = ["Trending", "Latest", "Most Viewed", "Most Downloaded"]

DATE_RANGES = ["Any time", "Past 24 hours", "Past week", "Past month", "Past year"]

# Labels offered in the upload wizard's "type" step (map onto category slugs
# where one exists; free types are still allowed).
UPLOAD_TYPES = [
    "Research Paper", "Book", "Article", "Notes", "Study Material",
    "Presentation", "Assignment", "Question Paper", "Thesis", "Report", "Other",
]

# Hero "Trending:" chips on the Explore landing.
TREND_CHIPS = [
    "Machine Learning", "GATE", "Linear Algebra", "NEET",
    "Thermodynamics", "Economics", "Blockchain",
]

# Default category tiles seeded on first migration (slug, name, icon, color,
# blurb). Mirrors the delivered Explore.html "Browse by category" grid.
DEFAULT_CATEGORIES = [
    ("research-papers", "Research Papers", "📄", "#2f6bd8", "Peer-reviewed studies & journals"),
    ("books", "Books", "📚", "#125027", "Textbooks & reference reads"),
    ("articles", "Articles", "📰", "#0f8f7e", "Explainers & short reads"),
    ("notes", "Notes", "📝", "#e07900", "Handwritten & typed notes"),
    ("study-materials", "Study Materials", "📘", "#1b9c85", "Guides, summaries & kits"),
    ("presentations", "Presentations", "📊", "#6b58d3", "Slide decks & seminars"),
    ("assignments", "Assignments", "✍️", "#004a33", "Solved & sample submissions"),
    ("question-papers", "Question Papers", "❓", "#c2410c", "Previous years & mock tests"),
]
