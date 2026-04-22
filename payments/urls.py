from django.urls import path
from .webhooks import razorpay_webhook
from .views import AdminOrderListView

urlpatterns = [
    path("webhook/", razorpay_webhook),
    path("admin/orders/", AdminOrderListView.as_view()),
]
