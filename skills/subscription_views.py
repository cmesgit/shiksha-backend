"""
skills/subscription_views.py — the guest-expert advertising subscription API.

Expert-facing (the logged-in expert):
    GET    /skill/subscription/                 → status + what to do next
    POST   /skill/subscription/                 → subscribe / start a period
    POST   /skill/subscription/submit-payment/  → attach UPI proof (paid mode)
    DELETE /skill/subscription/                 → cancel (stops ads, decays reach)

Admin:
    GET  /skill/admin/ad-subscriptions/                  → approval queue
    POST /skill/admin/ad-subscriptions/<id>/approve/     → activate 30 days
    POST /skill/admin/ad-subscriptions/<id>/reject/      → back to pending

Phased billing (GlobalSettings.effective_mode):
  • FREE phase → POST subscribes instantly + free; everyone is advertised anyway.
  • PAID phase → POST creates a pending record and returns the platform UPI;
                 the expert submits a reference; an admin approves to go live.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status as drf
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from accounts.permissions import IsAdmin
from .models import ExpertProfile
from .subscription_models import (
    ExpertAdSubscription, SKILL_AD_MONTHLY_PAISE, SKILL_AD_PERIOD_DAYS,
)


def _expert_for(user):
    ep = ExpertProfile.objects.filter(teacher_profile__user=user).first()
    if not ep:
        raise PermissionDenied("No expert profile found for this account.")
    return ep


def _billing_free():
    try:
        from global_settings.models import GlobalSettings
        return GlobalSettings.load().effective_mode == GlobalSettings.PAYMENT_FREE
    except Exception:
        return True


def _platform_upi():
    try:
        from global_settings.models import GlobalSettings
        gs = GlobalSettings.load()
        return {"vpa": gs.upi_id, "payee_name": gs.upi_payee_name}
    except Exception:
        return {"vpa": "", "payee_name": ""}


def _serialize_sub(ep):
    sub = getattr(ep, "ad_subscription", None)
    free = _billing_free()
    data = {
        "billing_mode":  "free" if free else "paid",
        "price_rupees":  SKILL_AD_MONTHLY_PAISE // 100,
        "period_days":   SKILL_AD_PERIOD_DAYS,
        "is_advertised": ep.is_advertised(),
        "is_featured":   ep.is_featured,
        "reach_count":   ep.reach_count,
        "status":        sub.status if sub else "none",
        "plan":          sub.plan if sub else None,
        "auto_renew":    sub.auto_renew if sub else None,
        "period_end":    sub.current_period_end if sub else None,
        "active":        bool(sub and sub.is_currently_active()),
    }
    # In paid mode an expert with no live subscription needs the payee details.
    if not free and not data["active"]:
        data["pay_to_platform"] = _platform_upi()
    return data


class ExpertSubscriptionView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        ep = _expert_for(request.user)
        return Response(_serialize_sub(ep))

    def post(self, request):
        """Subscribe / start a period."""
        ep = _expert_for(request.user)
        sub, _ = ExpertAdSubscription.objects.get_or_create(expert=ep)

        if _billing_free():
            # Free launch phase: advertise instantly, no payment.
            sub.amount = 0
            sub.activate(free=True)
            return Response(
                {"detail": "You're advertised — free during launch.", **_serialize_sub(ep)},
                status=drf.HTTP_200_OK,
            )

        # Paid phase: park as pending and hand back the platform payee details.
        sub.plan = ExpertAdSubscription.PLAN_MONTHLY
        sub.amount = SKILL_AD_MONTHLY_PAISE
        sub.status = ExpertAdSubscription.STATUS_PENDING
        sub.auto_renew = True
        sub.save(update_fields=["plan", "amount", "status", "auto_renew", "updated_at"])
        return Response(
            {
                "detail": "Subscription started. Pay the platform UPI and submit your reference.",
                "pay_to_platform": _platform_upi(),
                **_serialize_sub(ep),
            },
            status=drf.HTTP_201_CREATED,
        )

    def delete(self, request):
        """Cancel — stops advertising and decays reach."""
        ep = _expert_for(request.user)
        sub = getattr(ep, "ad_subscription", None)
        if not sub or sub.status in (
            ExpertAdSubscription.STATUS_CANCELLED, ExpertAdSubscription.STATUS_EXPIRED
        ):
            return Response({"detail": "No active subscription.", **_serialize_sub(ep)})
        sub.cancel()
        return Response({"detail": "Subscription cancelled.", **_serialize_sub(ep)})


class ExpertSubscriptionSubmitPaymentView(APIView):
    """POST /skill/subscription/submit-payment/  (paid mode)"""
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        ep = _expert_for(request.user)
        sub = getattr(ep, "ad_subscription", None)
        if not sub:
            raise NotFound("Start a subscription first.")
        ref = (request.data.get("upi_reference") or "").strip()
        if not ref:
            raise ValidationError({"upi_reference": "Payment reference (UTR) is required."})
        sub.upi_reference = ref[:40]
        sub.payer_vpa = (request.data.get("payer_vpa") or "").strip()[:120]
        sub.note = (request.data.get("note") or "").strip()
        if "receipt" in request.FILES:
            sub.receipt = request.FILES["receipt"]
        sub.status = ExpertAdSubscription.STATUS_SUBMITTED
        sub.save(update_fields=["upi_reference", "payer_vpa", "note", "receipt",
                                "status", "updated_at"])
        return Response(
            {"detail": "Payment submitted — awaiting verification.", **_serialize_sub(ep)}
        )


# ── Admin ──────────────────────────────────────────────────────────────────

def _admin_row(sub):
    ep = sub.expert
    return {
        "id":            str(sub.id),
        "expert_id":     str(ep.id),
        "expert_name":   ep.display_name(),
        "status":        sub.status,
        "amount_rupees": sub.amount // 100,
        "upi_reference": sub.upi_reference,
        "payer_vpa":     sub.payer_vpa,
        "receipt":       sub.receipt.url if sub.receipt else None,
        "period_end":    sub.current_period_end,
        "created_at":    sub.created_at,
        "updated_at":    sub.updated_at,
    }


class AdminAdSubscriptionQueueView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = ExpertAdSubscription.objects.select_related("expert__teacher_profile__user")
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        else:
            qs = qs.filter(status__in=[
                ExpertAdSubscription.STATUS_SUBMITTED,
                ExpertAdSubscription.STATUS_PENDING,
            ])
        return Response([_admin_row(s) for s in qs])


class AdminAdSubscriptionApproveView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, sub_id):
        sub = ExpertAdSubscription.objects.filter(id=sub_id).first()
        if not sub:
            raise NotFound("Subscription not found.")
        sub.activate(days=SKILL_AD_PERIOD_DAYS, reviewer=request.user)
        return Response({"ok": True, **_admin_row(sub)})


class AdminAdSubscriptionRejectView(APIView):
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request, sub_id):
        sub = ExpertAdSubscription.objects.filter(id=sub_id).first()
        if not sub:
            raise NotFound("Subscription not found.")
        sub.status = ExpertAdSubscription.STATUS_PENDING
        sub.note = (request.data.get("reason") or sub.note)
        sub.save(update_fields=["status", "note", "updated_at"])
        return Response({"ok": True, **_admin_row(sub)})
