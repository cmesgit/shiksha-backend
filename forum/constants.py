# Fixed taxonomy for the forum redesign.
#
# Topics and categories are a stable, curated taxonomy rather than DB rows:
# the redesign starts with an empty content database, but the UI still needs a
# fixed set of topics to tag questions with and categories to browse. Question
# and follower counts for a category are computed on read (see views), so an
# empty DB simply reports zeros.

# The topic chips a question can be filed under (first == default "General").
FORUM_TOPICS = [
    "General",
    "Career Guidance",
    "Engineering Admissions",
    "Medical & NEET",
    "Study Abroad",
    "Competitive Exams",
    "Placements & Internships",
    "Skill Development",
    "Coding & Programming",
    "Interview Preparation",
    "Resume & CV",
    "Higher Studies",
    "Research & PhD",
    "Scholarships",
    "Education Loans",
    "Government Jobs",
    "Design Portfolios",
    "College Life",
    "Mental Health & Motivation",
]

# Browsable categories. Each maps to a topic; counts are computed on read.
FORUM_CATEGORIES = [
    {"id": "career",     "name": "Career Guidance",          "desc": "Choosing paths, switching fields, and long-term planning.",       "initials": "CG", "color": "#125027", "topic": "Career Guidance"},
    {"id": "eng",        "name": "Engineering Admissions",   "desc": "JEE, counselling, branch vs college and cut-offs.",               "initials": "EA", "color": "#0f8f7e", "topic": "Engineering Admissions"},
    {"id": "abroad",     "name": "Study Abroad",             "desc": "Applications, visas, funding and life overseas.",                 "initials": "SA", "color": "#6b58d3", "topic": "Study Abroad"},
    {"id": "exams",      "name": "Competitive Exams",        "desc": "GATE, UPSC, CAT and other national exams.",                       "initials": "CE", "color": "#ff8f01", "topic": "Competitive Exams"},
    {"id": "placements", "name": "Placements & Internships", "desc": "Resumes, interviews, campus and off-campus roles.",               "initials": "PI", "color": "#c0446b", "topic": "Placements & Internships"},
    {"id": "skills",     "name": "Skill Development",         "desc": "Upskilling, projects and self-study roadmaps.",                   "initials": "SD", "color": "#8a5a00", "topic": "Skill Development"},
    {"id": "scholar",    "name": "Scholarships",             "desc": "Grants, assistantships and funding routes.",                      "initials": "SC", "color": "#2f6db5", "topic": "Scholarships"},
    {"id": "design",     "name": "Design Portfolios",        "desc": "NID/NIFT prep, portfolios and creative careers.",                 "initials": "DP", "color": "#a23e9c", "topic": "Design Portfolios"},
]

FORUM_CATEGORIES_BY_ID = {c["id"]: c for c in FORUM_CATEGORIES}

# Deterministic avatar palette for user/space initials-avatars.
FORUM_PALETTE = ["#0f8f7e", "#6b58d3", "#ff8f01", "#125027", "#c0446b", "#8a5a00", "#2f6db5", "#a23e9c"]
