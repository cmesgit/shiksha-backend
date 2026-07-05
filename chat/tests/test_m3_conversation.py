# M3 §9 — Conversation generalization: ensure_room()/ensure_course_room(),
# context_type/context_id, the course_id back-compat property, and the
# unique-room-per-context constraint.
from django.db import IntegrityError, transaction
from django.test import TestCase

from chat import services
from chat.models import Conversation

from .factories import make_course


class EnsureRoomTest(TestCase):

    def test_ensure_room_creates_and_is_idempotent(self):
        conv1 = services.ensure_room("course", "abc-123", title="Physics")
        conv2 = services.ensure_room("course", "abc-123", title="Physics")
        self.assertEqual(conv1.id, conv2.id)
        self.assertEqual(conv1.kind, Conversation.KIND_ROOM)
        self.assertEqual(conv1.context_type, "course")
        self.assertEqual(conv1.context_id, "abc-123")

    def test_ensure_room_updates_title_on_existing_room(self):
        conv1 = services.ensure_room("course", "abc-999", title="Old Title")
        services.ensure_room("course", "abc-999", title="New Title")
        conv1.refresh_from_db()
        self.assertEqual(conv1.title, "New Title")

    def test_different_context_types_do_not_collide_on_the_same_id_string(self):
        """(context_type, context_id) together are the unique key, not
        context_id alone — a course and a counseling_case that happen to
        share an id string must not be treated as the same room."""
        conv_course = services.ensure_room("course", "shared-id", title="Course room")
        conv_other = services.ensure_room("counseling_case", "shared-id", title="Case room")
        self.assertNotEqual(conv_course.id, conv_other.id)

    def test_ensure_course_room_is_a_thin_wrapper_over_ensure_room(self):
        course = make_course()
        conv = services.ensure_course_room(course.id, title=course.title)
        self.assertEqual(conv.kind, Conversation.KIND_ROOM)
        self.assertEqual(conv.context_type, "course")
        self.assertEqual(conv.context_id, str(course.id))

        conv_via_generic = services.ensure_room("course", str(course.id))
        self.assertEqual(conv.id, conv_via_generic.id)


class CourseIdBackCompatTest(TestCase):

    def test_course_id_property_on_a_course_room(self):
        course = make_course()
        conv = services.ensure_course_room(course.id, title=course.title)
        self.assertEqual(conv.course_id, str(course.id))

    def test_course_id_property_is_none_for_non_course_context(self):
        conv = services.ensure_room("counseling_case", "case-1")
        self.assertIsNone(conv.course_id)

    def test_course_id_property_is_none_for_direct(self):
        conv = Conversation.objects.create(kind=Conversation.KIND_DIRECT, direct_key="L:1|T:2")
        self.assertIsNone(conv.course_id)


class UniqueRoomConstraintTest(TestCase):

    def test_unique_room_per_context_enforced_at_db_level(self):
        Conversation.objects.create(
            kind=Conversation.KIND_ROOM, context_type="course", context_id="dup",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Conversation.objects.create(
                    kind=Conversation.KIND_ROOM, context_type="course", context_id="dup",
                )

    def test_non_room_kinds_are_unconstrained_by_context(self):
        """The unique constraint is scoped to kind=ROOM — two SESSION rows
        (say) with the same context shouldn't be forced into collision by
        a constraint that was never meant for them."""
        Conversation.objects.create(kind=Conversation.KIND_SESSION, context_type="x", context_id="y")
        Conversation.objects.create(kind=Conversation.KIND_SESSION, context_type="x", context_id="y")
        self.assertEqual(
            Conversation.objects.filter(kind=Conversation.KIND_SESSION).count(), 2,
        )
