"""content/ai_views.py — server-side OpenAI proxy for the public General
Studies page (shiksha-frontend/src/components/GeneralStudies.jsx). That
component used to call OpenAI directly from the browser with
`dangerouslyAllowBrowser: true`, shipping `VITE_OPENAI_API_KEY` in the
public JS bundle to every visitor. This endpoint holds the real key
server-side; the frontend keeps its own prompt-building and response-
parsing unchanged, it just POSTs the prompt here instead of calling OpenAI
directly.
"""
import logging

import requests
from django.conf import settings
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 4000


class GeneralStudiesAIView(APIView):
    """POST /api/content/ai/general-studies/  {prompt} -> {text}

    Public — /general-studies has no login gate, so this can't require
    IsAuthenticated. Cost-sensitive (real OpenAI spend per call with no
    auth gate), hence the tight throttle scope.
    """
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "general_studies_ai"

    def post(self, request):
        prompt = (request.data.get("prompt") or "").strip()
        if not prompt:
            raise ValidationError("prompt is required.")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValidationError(f"prompt too long (max {MAX_PROMPT_CHARS} characters).")

        api_key = getattr(settings, "OPENAI_API_KEY", "") or ""
        if not api_key:
            raise ValidationError("AI assistant is not configured right now.")

        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-3.5-turbo",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1000,
                    "temperature": 0.7,
                },
                timeout=30,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            logger.exception("General Studies AI proxy call failed")
            raise ValidationError("AI assistant is temporarily unavailable. Try again shortly.")

        return Response({"text": text})
