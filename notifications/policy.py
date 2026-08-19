# PLACEMENT: backend/backend/notifications/policy.py   (NEW FILE)
# DEPLOY:    /app/shiksha-backend/notifications/policy.py
#
# THE channel-routing matrix. One place answers "when X happens, which
# channels fire?" — call sites just emit a verb; they never decide
# email/SMS/push themselves. Changing product policy = editing this file,
# no view surgery.
#
# Channel levels
# ──────────────
#   OFF       never sent on this channel for this verb
#   OPT_OUT   sent by default; the user can disable it in
#             /api/notifications/preferences/ (per-channel switch or by
#             muting the verb's category)
#   REQUIRED  always sent — transactional/compliance messages the user
#             cannot turn off (booking confirmations, cancellations,
#             payment receipts). Under TRAI rules these are
#             "transactional/service-implicit" SMS: they may be delivered
#             even to DND numbers *only because* they are consent-implied
#             consequences of an action the user took. Never put anything
#             promotional at this level.
#
# in-app + WebSocket are ALWAYS on for every verb (that's the bell); the
# matrix below only governs the three "away from the app" channels.
#
# sms_template is the key into settings.SMS_TEMPLATES — every SMS body in
# India must byte-match a DLT-registered template, so SMS is *never*
# freeform title/body text (see notifications/sms.py + NOTIFICATIONS_DESIGN.md).

OFF = "off"
OPT_OUT = "opt_out"
REQUIRED = "required"

# Preference categories the user can mute (maps 1:n onto verbs). Shown by
# the preferences endpoint so the frontends can render toggles.
CATEGORIES = [
    "bookings",    # session/appointment lifecycle
    "reminders",   # upcoming-session reminders
    "classes",     # live class started / group invites
    "learning",    # assignments, quizzes, materials
    "social",      # forum + chat
    "payments",    # receipts and payment status
    "account",     # onboarding, approvals, security
    "announcements",  # Stage D (CC-015) — course Announcements (BROADCAST)
    "support",         # Stage D (CC-022) — Academic Support ticket activity
]

_DEFAULT = {"category": "social", "email": OFF, "sms": OFF, "push": OPT_OUT,
            "sms_template": None}

POLICY = {
    # ── Counseling bookings ────────────────────────────────────────────
    "counseling.booked":       {"category": "bookings", "email": REQUIRED, "sms": REQUIRED, "push": REQUIRED, "sms_template": "booking_confirmed"},
    "counseling.cancelled":    {"category": "bookings", "email": REQUIRED, "sms": REQUIRED, "push": REQUIRED, "sms_template": "booking_cancelled"},
    "counseling.meeting_link": {"category": "bookings", "email": OPT_OUT,  "sms": OFF,      "push": REQUIRED},
    "counseling.report":       {"category": "bookings", "email": REQUIRED, "sms": OFF,      "push": OPT_OUT},
    "counseling.assessment":   {"category": "bookings", "email": OPT_OUT,  "sms": OFF,      "push": OPT_OUT},
    "counseling.reminder_24h": {"category": "reminders", "email": OPT_OUT, "sms": OFF,      "push": REQUIRED},
    "counseling.reminder_1h":  {"category": "reminders", "email": OFF,     "sms": REQUIRED, "push": REQUIRED, "sms_template": "session_reminder"},
    # counselor onboarding (account-level, email only)
    "counseling.application":  {"category": "account", "email": REQUIRED, "sms": OFF, "push": OFF},
    "counseling.approved":     {"category": "account", "email": REQUIRED, "sms": OFF, "push": OFF},
    "counseling.rejected":     {"category": "account", "email": REQUIRED, "sms": OFF, "push": OFF},

    # ── Private 1-on-1 sessions (sessions_app.PrivateSession) ──────────
    "session.requested":   {"category": "bookings", "email": OPT_OUT,  "sms": OFF,      "push": REQUIRED},
    "session.approved":    {"category": "bookings", "email": REQUIRED, "sms": REQUIRED, "push": REQUIRED, "sms_template": "booking_confirmed"},
    "session.declined":    {"category": "bookings", "email": OPT_OUT,  "sms": OFF,      "push": REQUIRED},
    "session.rescheduled": {"category": "bookings", "email": REQUIRED, "sms": REQUIRED, "push": REQUIRED, "sms_template": "booking_rescheduled"},
    "session.cancelled":   {"category": "bookings", "email": REQUIRED, "sms": REQUIRED, "push": REQUIRED, "sms_template": "booking_cancelled"},
    "session.reminder_24h": {"category": "reminders", "email": OPT_OUT, "sms": OFF,      "push": REQUIRED},
    "session.reminder_1h":  {"category": "reminders", "email": OFF,     "sms": REQUIRED, "push": REQUIRED, "sms_template": "session_reminder"},

    # ── Skill Dev sessions (skills.SkillSession) ───────────────────────
    # Deliberately mirrors the session.* block above rather than inventing a
    # second policy shape: a Skill Dev booking is the same promise to the
    # same person as a private-session booking, so it earns the same
    # channels. The one divergence is skill.paid — see below.
    "skill.requested":            {"category": "bookings", "email": OPT_OUT,  "sms": OFF,      "push": REQUIRED},
    "skill.confirmed":            {"category": "bookings", "email": REQUIRED, "sms": REQUIRED, "push": REQUIRED, "sms_template": "booking_confirmed"},
    "skill.declined":             {"category": "bookings", "email": OPT_OUT,  "sms": OFF,      "push": REQUIRED},
    "skill.cancelled":            {"category": "bookings", "email": REQUIRED, "sms": REQUIRED, "push": REQUIRED, "sms_template": "booking_cancelled"},
    "skill.reschedule_proposed":  {"category": "bookings", "email": REQUIRED, "sms": REQUIRED, "push": REQUIRED, "sms_template": "booking_rescheduled"},
    # The two reschedule *responses* land on the expert, who is the party
    # already sitting in the app waiting on an answer — no SMS spend.
    "skill.reschedule_confirmed": {"category": "bookings", "email": OPT_OUT,  "sms": OFF,      "push": REQUIRED},
    "skill.reschedule_declined":  {"category": "bookings", "email": OPT_OUT,  "sms": OFF,      "push": REQUIRED},
    "skill.completed":            {"category": "bookings", "email": OFF,      "sms": OFF,      "push": OPT_OUT},
    # Skill payment is direct P2P (skills/views.py CreateOrderView) — this is
    # the expert *asserting* they were paid, not a platform-observed receipt,
    # so it must not go out as a REQUIRED transactional confirmation the way
    # payments.receipt does. In-app + opt-out push only.
    "skill.paid":                 {"category": "payments", "email": OFF,      "sms": OFF,      "push": OPT_OUT},

    # ── Group sessions ─────────────────────────────────────────────────
    "group.invite":       {"category": "classes",   "email": OPT_OUT, "sms": OFF,     "push": REQUIRED},
    "group.cancelled":    {"category": "classes",   "email": OPT_OUT, "sms": OFF,     "push": REQUIRED},
    "group.reminder_24h": {"category": "reminders", "email": OPT_OUT, "sms": OFF,     "push": REQUIRED},
    "group.reminder_1h":  {"category": "reminders", "email": OFF,     "sms": OPT_OUT, "push": REQUIRED, "sms_template": "session_reminder"},
    "livestream.reminder_24h": {"category": "reminders", "email": OPT_OUT, "sms": OFF,     "push": REQUIRED},
    "livestream.reminder_1h":  {"category": "reminders", "email": OFF,     "sms": REQUIRED, "push": REQUIRED, "sms_template": "session_reminder"},

    # ── Live classes / learning ────────────────────────────────────────
    "livestream.started":  {"category": "classes",  "email": OFF,     "sms": OFF, "push": REQUIRED},
    "assignment.posted":   {"category": "learning", "email": OFF,     "sms": OFF, "push": OPT_OUT},
    "assignment.graded":   {"category": "learning", "email": OPT_OUT, "sms": OFF, "push": OPT_OUT},
    # TEACHER-facing: a student turned work in. Declared explicitly rather
    # than left to fall through to _DEFAULT, so the channel choice is a
    # decision and not an accident. Email OFF deliberately — a class of 40
    # submitting near a deadline would be 40 emails; push is opt-out so it
    # arrives by default but can be silenced.
    "assignment.submitted": {"category": "learning", "email": OFF,    "sms": OFF, "push": OPT_OUT},
    "quiz.posted":         {"category": "learning", "email": OFF,     "sms": OFF, "push": OPT_OUT},
    "quiz.deadline":       {"category": "learning", "email": OPT_OUT, "sms": OFF, "push": REQUIRED},
    # Teacher-triggered nudge for students who haven't attempted a
    # published quiz yet (Quiz Analytics "Send reminder" action).
    "quiz.reminder":       {"category": "learning", "email": OPT_OUT, "sms": OFF, "push": REQUIRED},
    "materials.uploaded":  {"category": "learning", "email": OFF,     "sms": OFF, "push": OPT_OUT},

    # ── Social (high volume — NEVER email/SMS these) ───────────────────
    "chat.message":    {"category": "social", "email": OFF, "sms": OFF, "push": OPT_OUT},
    "forum.reply":     {"category": "social", "email": OFF, "sms": OFF, "push": OPT_OUT},
    "forum.accepted":  {"category": "social", "email": OFF, "sms": OFF, "push": OPT_OUT},
    "forum.upvote":    {"category": "social", "email": OFF, "sms": OFF, "push": OFF},

    # ── Communication Center closure — Stage D (CC-015/022/023) ────────
    # Announcements: a course teacher posting to their whole class is a
    # deliberate, low-frequency broadcast (not per-message chatter like
    # chat.message above), so push defaults REQUIRED rather than OPT_OUT —
    # the same "this actually matters, don't let it get lost" reasoning as
    # session/booking reminders. Still no email/SMS: those channels are
    # reserved for things that matter even when the person is away from
    # the app entirely, and an in-course announcement doesn't rise to that
    # bar on its own.
    "announcement.posted": {"category": "announcements", "email": OFF, "sms": OFF, "push": REQUIRED},
    # A support-ticket reply is the one place a learner/teacher is actively
    # waiting on a person, so — like booking confirmations — it's REQUIRED,
    # not muteable via a category toggle.
    "support.reply":        {"category": "support", "email": OFF, "sms": OFF, "push": REQUIRED},

    # ── Money ──────────────────────────────────────────────────────────
    # Email receipt is the legal record. SMS deliberately OPT_OUT, not
    # REQUIRED: the user's bank/UPI app already sends a debit SMS, so a
    # platform SMS is a paid duplicate. Flip to REQUIRED only if support
    # tickets show users missing payment confirmations.
    "payments.receipt": {"category": "payments", "email": REQUIRED, "sms": OPT_OUT, "push": REQUIRED, "sms_template": "payment_receipt"},
    "payments.failed":  {"category": "payments", "email": OPT_OUT,  "sms": OFF,     "push": REQUIRED},

    # ── Enrollment / account ───────────────────────────────────────────
    "enrollment.approved": {"category": "account", "email": REQUIRED, "sms": OPT_OUT, "push": REQUIRED, "sms_template": "enrollment_approved"},
    "enrollment.rejected": {"category": "account", "email": REQUIRED, "sms": OFF,     "push": REQUIRED},
}


def for_verb(verb):
    """Policy row for a verb; unknown verbs fall back to in-app-only-ish
    defaults (push opt-out, no email/SMS) so a new verb can never
    accidentally spend SMS money before it's added here."""
    row = POLICY.get(verb)
    if row is None:
        return dict(_DEFAULT)
    merged = dict(_DEFAULT)
    merged.update(row)
    return merged
