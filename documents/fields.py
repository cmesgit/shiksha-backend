"""The top level of the Explore tag path: field › subject › branch.

`constants.SUBJECT_TOPICS` has always held two levels — a subject and its
topics. The Explore redesign browses by three, so this module adds the level
above and nothing else; `taxonomy.subject_groups()` keeps doing the two-level
work and this wraps it.

Why a static map rather than a table: the same reason `SUBJECT_TOPICS` is a
static map. A field is not something an uploader invents — there are a handful
of them and they order the browse panel. Subjects and branches *are* invented
by uploaders, with no approval, and those keep coming from live data.

⚠ The design's own browse panel showed six fields (Engineering, Social
Sciences, Sciences, Humanities, Commerce, Exam Prep), but that was its sample
data, drawn from eight sample documents. The real catalogue has 39 subjects and
three of them fit none of the six. Rather than file Nursing under "Sciences" or
Skill Development under "Humanities" — which would be a quiet lie in a
navigation control — two more fields carry them. If you remove either, every
subject it holds falls through to UNFILED and stops being browsable by field.
"""
from .constants import SUBJECT_TOPICS

ENGINEERING = "Engineering"
SCIENCES = "Sciences"
SOCIAL_SCIENCES = "Social Sciences"
HUMANITIES = "Humanities"
COMMERCE = "Commerce"
EXAM_PREP = "Exam Prep"
HEALTH = "Health"
OTHER = "Other"

# Browse-panel order. Not alphabetical — the design showed the busiest fields
# first, and the two added fields go last so the six from the design keep their
# positions.
FIELD_ORDER = [
    ENGINEERING,
    SOCIAL_SCIENCES,
    SCIENCES,
    HUMANITIES,
    COMMERCE,
    EXAM_PREP,
    HEALTH,
    OTHER,
]

# Every key here must exist in SUBJECT_TOPICS; the test asserts both directions,
# so adding a subject there without one here fails loudly instead of silently
# dropping it out of the field row.
#
# Two placements are fixed by the design itself, not chosen here: the design's
# browse panel listed Computer Science under "SUBJECT IN ENGINEERING", and its
# sample tag path read "Social Sciences › Sociology › Rural sociology".
SUBJECT_FIELDS = {
    # Engineering
    "Civil Engineering": ENGINEERING,
    "Mechanical Engineering": ENGINEERING,
    "Electrical Engineering": ENGINEERING,
    "Chemical Engineering": ENGINEERING,
    "Computer Science": ENGINEERING,
    "Electronics": ENGINEERING,
    "Architecture": ENGINEERING,
    # Sciences
    "Physics": SCIENCES,
    "Chemistry": SCIENCES,
    "Biology": SCIENCES,
    "Mathematics": SCIENCES,
    "Statistics": SCIENCES,
    "Environmental Science": SCIENCES,
    "Agriculture": SCIENCES,
    # Social Sciences
    "Economics": SOCIAL_SCIENCES,
    "Political Science": SOCIAL_SCIENCES,
    "Sociology": SOCIAL_SCIENCES,
    "Psychology": SOCIAL_SCIENCES,
    "Geography": SOCIAL_SCIENCES,
    "Law": SOCIAL_SCIENCES,
    "Social Work": SOCIAL_SCIENCES,
    "Education": SOCIAL_SCIENCES,
    # Humanities
    "History": HUMANITIES,
    "Philosophy": HUMANITIES,
    "English": HUMANITIES,
    "Languages": HUMANITIES,
    "Fine Arts": HUMANITIES,
    "Media and Communication": HUMANITIES,
    # Commerce
    "Commerce": COMMERCE,
    "Accounting": COMMERCE,
    "Finance": COMMERCE,
    "Management": COMMERCE,
    # Exam Prep
    "Competitive Exams": EXAM_PREP,
    # Health — Medicine, Nursing and Pharmacy are not "Sciences" to anyone
    # looking for them, and filing them there makes the Sciences row mean two
    # different things.
    "Medicine": HEALTH,
    "Nursing": HEALTH,
    "Pharmacy": HEALTH,
    # Other — the catch-all subjects that predate the field level. "General" is
    # the default subject on older uploads, so this field is not empty on day
    # one and must stay in the browse panel.
    "General": OTHER,
    "Skill Development": OTHER,
    "Sports and Physical Education": OTHER,
}

# Where a subject invented by an uploader lands until someone maps it. It is a
# real browsable field, not a null — an unmapped subject must still be
# reachable, or uploading under a new subject name would hide the upload.
UNFILED = OTHER

_BY_CASEFOLD = {s.casefold(): f for s, f in SUBJECT_FIELDS.items()}


def field_for_subject(subject):
    """The field a subject belongs to. Never returns empty.

    Case- and whitespace-insensitive, because `Document.subject` is free text
    and has always been: "  biology " and "Biology" are the same subject to
    every other part of Explore, and they have to be the same here too or the
    field row double-counts.
    """
    if not subject:
        return UNFILED
    return _BY_CASEFOLD.get(subject.strip().casefold(), UNFILED)


def field_groups(triples):
    """The three-level browse tree, as a list of fields in FIELD_ORDER.

    `triples` is an iterable of (field, subject, branch) from the documents a
    viewer can actually see — the same contract as `taxonomy.subject_groups()`,
    one level deeper. Live data extends the static tree; it never shrinks it,
    so a field or subject with nothing in it still renders with a zero count
    rather than vanishing between page loads.
    """
    groups = {name: {} for name in FIELD_ORDER}
    for subject, branches in SUBJECT_TOPICS.items():
        groups.setdefault(field_for_subject(subject), {})[subject] = list(branches)

    # Index the canonical spellings so live rows fold into the static tree
    # instead of appearing beside it as near-duplicates.
    canonical = {
        subject.casefold(): (field, subject)
        for field, subjects in groups.items()
        for subject in subjects
    }

    for field, subject, branch in triples:
        subject = (subject or "").strip()
        if not subject or subject.casefold() == "all":
            continue
        field = (field or "").strip() or field_for_subject(subject)
        known = canonical.get(subject.casefold())
        if known:
            field, subject = known
        else:
            canonical[subject.casefold()] = (field, subject)
        branches = groups.setdefault(field, {}).setdefault(subject, [])
        branch = (branch or "").strip()
        if branch and branch.casefold() not in {b.casefold() for b in branches}:
            branches.append(branch)

    ordered = FIELD_ORDER + [f for f in groups if f not in FIELD_ORDER]
    return [
        {
            "field": name,
            "subjects": [
                {"subject": subject, "branches": branches}
                for subject, branches in groups[name].items()
            ],
        }
        for name in ordered
        if name in groups
    ]
