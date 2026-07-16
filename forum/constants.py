# Fixed taxonomy for the forum redesign.
#
# Categories are now moderator-managed DB rows (see forum.models.ForumCategory)
# rather than a hardcoded list. Topics remain a fixed set of tag chips a
# question can be filed under; ListTopicsView unions this list with any
# active category's topic so newly created categories stay taggable.

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

# Deterministic avatar palette for user/space initials-avatars.
FORUM_PALETTE = ["#0f8f7e", "#6b58d3", "#ff8f01", "#125027", "#c0446b", "#8a5a00", "#2f6db5", "#a23e9c"]
