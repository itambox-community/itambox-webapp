import logging

from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _

from assets.models import AssetRequest
from core.models import Notification
from core.tasks.context import TaskContext
from core.tenant_scope import tenant_notification_recipients

logger = logging.getLogger(__name__)
User = get_user_model()


def notify_new_request_task(request_id):
    # Resolve the request unscoped first to learn its tenant, then run the rest
    # under that tenant's context: the fetch is tenant-scoped, the change-log is
    # attributed correctly, and — critically — recipients are limited to that
    # tenant's staff. Previously this ran with no TaskContext and notified every
    # platform-wide is_staff user, leaking one tenant's requests to all others.
    request = AssetRequest.all_objects.filter(pk=request_id).first()
    if request is None:
        return

    with TaskContext(tenant_id=request.tenant_id, user_id=None):
        instance = AssetRequest.objects.filter(pk=request_id).first()
        if instance is None or instance.parent is not None:
            return

        admins = [user for user in tenant_notification_recipients(instance.tenant) if user.is_staff]
        notifications = [
            Notification(
                user=admin,
                subject=_("New Asset Request from %(requester)s") % {"requester": instance.requester},
                message=_("%(requester)s has requested %(item)s.")
                % {"requester": instance.requester, "item": instance},
                level=Notification.LEVEL_INFO,
                target_url=instance.get_absolute_url(),
                kind=Notification.KIND_TENANT,
                tenant_id=instance.tenant_id,
            )
            for admin in admins
        ]
        if notifications:
            Notification.objects.bulk_create(notifications)
