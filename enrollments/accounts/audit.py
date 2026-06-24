from accounts.models import AuthEvent


def get_client_ip(request):
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def log_auth_event(request, event_type, user=None):
    """
    Lightweight audit logger for authentication-related events.
    NEVER log secrets (passwords, tokens).
    """
    AuthEvent.objects.create(
        user=user,
        event_type=event_type,
        ip_address=get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
    )
