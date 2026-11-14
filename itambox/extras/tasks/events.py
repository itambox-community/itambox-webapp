"""Background tasks for the event outbox (issue #1091)."""

from extras.services.events import process_event, recover_pending_events

EVENT_RECOVERY_TASK_PATH = "extras.tasks.events.recover_pending_events_task"


def process_event_task(event_id):
    """Post-commit hint: process one event; the coordinator recovers a lost hint."""
    return process_event(event_id)


def recover_pending_events_task():
    """One-minute coordinator over due pending events (SKIP LOCKED claims)."""
    return recover_pending_events()
