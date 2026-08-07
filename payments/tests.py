from unittest.mock import patch

from django.test import TestCase

from accounts.models import User
from courses.models import Batch, Course
from payments.models import Order
from payments.services import create_order


class CreateOrderAmountTest(TestCase):
    """Course.price / Batch.price_override are stored in paise already —
    create_order must pass that value straight through to Razorpay, not
    multiply it by 100 again."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="learner", email="learner@test.com", password="x"
        )
        # ₹14,999 course, stored in paise per Course.price's convention.
        cls.course = Course.objects.create(title="NEET Crash Course", price=1499900)

    def _fake_order(self, order_id="order_fake123"):
        return patch("payments.services.client.order.create", return_value={"id": order_id})

    def test_order_amount_matches_course_price_in_paise(self):
        with self._fake_order() as mock_create:
            order = create_order(user=self.user, course=self.course)

        mock_create.assert_called_once_with({
            "amount": 1499900,
            "currency": "INR",
            "payment_capture": 1,
        })
        self.assertEqual(order.amount, 1499900)
        self.assertEqual(Order.objects.get(pk=order.pk).amount, 1499900)

    def test_order_amount_honours_batch_price_override(self):
        batch = Batch.objects.create(
            course=self.course, name="Batch A", code="A1", price_override=999900
        )

        with self._fake_order() as mock_create:
            order = create_order(user=self.user, course=self.course, batch=batch)

        mock_create.assert_called_once_with({
            "amount": 999900,
            "currency": "INR",
            "payment_capture": 1,
        })
        self.assertEqual(order.amount, 999900)

    def test_order_amount_falls_back_to_course_price_when_batch_has_no_override(self):
        batch = Batch.objects.create(course=self.course, name="Batch B", code="B1")

        with self._fake_order() as mock_create:
            order = create_order(user=self.user, course=self.course, batch=batch)

        mock_create.assert_called_once_with({
            "amount": 1499900,
            "currency": "INR",
            "payment_capture": 1,
        })
        self.assertEqual(order.amount, 1499900)
