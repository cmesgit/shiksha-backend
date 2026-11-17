"""Redirects — admin CRUD + the public resolver the SPA calls before a 404.

design_handoff_admin_counsellor, Admin v2 Phase 5 ("SEO and redirects").

Two audiences, two permission classes:

  GET  /api/content/redirects/resolve/?path=   PUBLIC, unauthenticated.
       Every visitor who hits a dead link runs this. It must stay cheap and
       must never require a session.
  ...  /api/content/admin/redirects/           Admin CRUD.

⚠ The resolver is the ONE place hits are counted, and it counts with an
F()-expression UPDATE rather than read-modify-write — two visitors on the
same dead link at once would otherwise lose a count to the race.
"""
import logging

from django.db.models import F
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin

from .models import Redirect

logger = logging.getLogger(__name__)

# How many hops the resolver will follow when one redirect points at another.
# Chains happen legitimately (rename twice); loops happen by accident. This
# bounds both without forbidding the first.
MAX_HOPS = 5


class RedirectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Redirect
        fields = ("id", "from_path", "to_path", "permanent", "is_active",
                  "hits", "last_hit_at", "note", "created_at")
        read_only_fields = ("hits", "last_hit_at", "created_at")
        extra_kwargs = {
            # The model's unique=True would otherwise attach a UniqueValidator
            # that runs on the RAW path, before normalisation — see validate().
            "from_path": {"validators": []},
        }

    def validate(self, attrs):
        """Refuse the three rules that cannot work.

        Each is refused with its own message, because "invalid" on a two-field
        form tells an admin nothing about which half to change.
        """
        inst = self.instance
        src = Redirect.normalise(attrs.get("from_path", getattr(inst, "from_path", "")))
        dst_raw = attrs.get("to_path", getattr(inst, "to_path", ""))
        offsite = str(dst_raw).lower().startswith(("http://", "https://"))
        dst = dst_raw if offsite else Redirect.normalise(dst_raw)

        if not src or src == "/":
            raise serializers.ValidationError(
                {"from_path": "Give the old path, e.g. /courses/old-name. "
                              "The site root cannot be redirected."})

        # ⚠ Uniqueness is re-checked HERE, on the normalised value. The
        # model's `unique=True` gives DRF a UniqueValidator, but that runs
        # against the RAW input — so `/DUP/` passed validation, then `save()`
        # normalised it to `/dup`, collided, and returned a 500 to an admin
        # who had simply typed an existing rule with different casing.
        clash = Redirect.objects.filter(from_path=src)
        if inst is not None:
            clash = clash.exclude(pk=inst.pk)
        if clash.exists():
            raise serializers.ValidationError(
                {"from_path": f"{src} already redirects somewhere. "
                              "Edit that rule instead of adding a second one."})
        if not dst:
            raise serializers.ValidationError({"to_path": "Give a destination."})

        # 1. Self-redirect — the browser would loop on one rule.
        if not offsite and src == dst:
            raise serializers.ValidationError(
                {"to_path": "That is the same path — it would redirect to itself."})

        # 2. A loop through existing rules: the destination eventually comes
        #    back here. Walked rather than checked one level deep, because
        #    A→B plus an existing B→A is the shape people actually create.
        if not offsite:
            seen, cur = {src}, dst
            for _ in range(MAX_HOPS):
                nxt = (Redirect.objects
                       .filter(from_path=cur, is_active=True)
                       .exclude(pk=getattr(inst, "pk", None))
                       .values_list("to_path", flat=True)
                       .first())
                if not nxt:
                    break
                nxt = Redirect.normalise(nxt)
                if nxt in seen:
                    raise serializers.ValidationError(
                        {"to_path": f"That would create a loop: {cur} already "
                                    f"redirects back to {nxt}."})
                seen.add(nxt)
                cur = nxt

        attrs["from_path"] = src
        attrs["to_path"] = dst
        return attrs


class RedirectResolveView(APIView):
    """GET /api/content/redirects/resolve/?path=/old-path

        200 {"to": "/new-path", "permanent": true}
        404 {"detail": "No redirect."}

    ⚠ PUBLIC on purpose. The SPA's catch-all route calls this before it
    renders a 404, so it runs for anonymous visitors by definition. It
    returns only a destination — no admin metadata, no list, and no way to
    enumerate rules.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        path = Redirect.normalise(request.query_params.get("path", ""))
        if not path:
            return Response({"detail": "No redirect."},
                            status=status.HTTP_404_NOT_FOUND)

        hop, seen, permanent = path, {path}, True
        for _ in range(MAX_HOPS):
            row = (Redirect.objects
                   .filter(from_path=hop, is_active=True)
                   .only("id", "to_path", "permanent")
                   .first())
            if row is None:
                break
            permanent = row.permanent
            # Count the hit on the rule that actually matched.
            Redirect.objects.filter(pk=row.pk).update(
                hits=F("hits") + 1, last_hit_at=timezone.now())

            dest = row.to_path
            if str(dest).lower().startswith(("http://", "https://")):
                return Response({"to": dest, "permanent": permanent})
            dest = Redirect.normalise(dest)
            if dest in seen:
                # A loop that survived validation (rules edited independently).
                # Stop and send them to the last good hop rather than bouncing.
                logger.warning("redirect loop at %s", dest)
                return Response({"to": dest, "permanent": permanent})
            seen.add(dest)
            hop = dest

        if hop == path:
            return Response({"detail": "No redirect."},
                            status=status.HTTP_404_NOT_FOUND)
        return Response({"to": hop, "permanent": permanent})


class AdminRedirectListView(APIView):
    """GET (list) + POST (create) /api/content/admin/redirects/"""

    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = Redirect.objects.all()
        q = (request.query_params.get("q") or "").strip()
        if q:
            from django.db.models import Q
            qs = qs.filter(Q(from_path__icontains=q) | Q(to_path__icontains=q))
        return Response({"results": RedirectSerializer(qs, many=True).data,
                         "count": qs.count()})

    def post(self, request):
        ser = RedirectSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save(created_by=request.user)
        return Response(ser.data, status=status.HTTP_201_CREATED)


class AdminRedirectDetailView(APIView):
    """PATCH + DELETE /api/content/admin/redirects/<id>/"""

    permission_classes = [IsAuthenticated, IsAdmin]

    def _get(self, pk):
        return Redirect.objects.filter(pk=pk).first()

    def patch(self, request, pk):
        row = self._get(pk)
        if row is None:
            return Response({"detail": "Not found."},
                            status=status.HTTP_404_NOT_FOUND)
        ser = RedirectSerializer(row, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    def delete(self, request, pk):
        row = self._get(pk)
        if row is None:
            return Response({"detail": "Not found."},
                            status=status.HTTP_404_NOT_FOUND)
        row.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
