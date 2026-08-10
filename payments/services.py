import razorpay
from django.conf import settings
from .models import Order


client = razorpay.Client(
    auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
)


def create_order(*, user, course, batch=None):
    # Course.price / Batch.price_override are already stored in paise —
    # Razorpay's `amount` is paise too, so no conversion belongs here.
    amount = batch.effective_price if batch is not None else course.price

    rp_order = client.order.create({
        "amount": amount,
        "currency": "INR",
        "payment_capture": 1,
    })

    return Order.objects.create(
        user=user,
        course=course,
        razorpay_order_id=rp_order["id"],
        amount=amount,
        status=Order.STATUS_CREATED,
    )
