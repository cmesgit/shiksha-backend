def push_ws_notification(user_id, data):
    """
    Push real-time notification to a user via WebSocket.
    Uses Celery for async processing.
    Safe to call from anywhere — never breaks if it fails.
    """
    try:
        from livestream.tasks import push_ws_notification_task
        push_ws_notification_task.delay(str(user_id), data)
    except Exception:
        # Fallback to synchronous if Celery is not available
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    f'notifications_{user_id}',
                    {
                        'type': 'send_notification',
                        'data': data
                    }
                )
        except Exception:
            pass
