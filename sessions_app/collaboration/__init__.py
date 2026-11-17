"""Shiksha Remote collaboration control plane (RC-006).

Spec: ``docs/architecture/shiksha-remote/`` — `api-contracts.md` §Participant
and capture APIs is the frozen wire contract for everything in this package.

Everything here is gated on ``GlobalSettings.collaboration_bindings_enabled``,
which ships **False**. While it is off every route answers **404**: the
feature is absent, not merely forbidden.

🔴 Do not confuse that flag with ``live_remote_access_enabled``, which
defaults **True** and governs the dormant legacy teacher→student path in
``sessions_app/remote_control_views.py``. See the help text on both fields.
"""
