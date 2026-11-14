import logging

from django.core.management import call_command
from django.utils.translation import gettext_lazy as _

from core.context import get_current_request_id
from core.errors import IntegrationContext, IntegrationError, IntegrationUnexpectedError
from core.models import Job, Notification

from .context import TaskContext
from .utils import reverse_job_detail

logger = logging.getLogger(__name__)


class JobLogStream:
    def __init__(self, job):
        self.job = job
        self.buffer = ""

    def write(self, message):
        self.buffer += message
        if "\n" in self.buffer:
            lines = self.buffer.split("\n")
            self.buffer = lines.pop()
            for line in lines:
                cleaned = line.strip()
                if cleaned:
                    self.job.append_log(cleaned)

    def flush(self):
        if self.buffer.strip():
            self.job.append_log(self.buffer.strip())
            self.buffer = ""


def sync_tenant_ldap_task(job_id: int, tenant_slug: str, user_id: int | None, tenant_id: int | None = None) -> None:
    """
    Asynchronously executes tenant LDAP directory synchronization.
    Runs the 'sync_tenant_ldap' management command and streams output to Job logs.
    """
    with TaskContext(tenant_id=tenant_id, user_id=user_id) as ctx:
        try:
            job = Job.objects.get(pk=job_id)
        except Job.DoesNotExist:
            logger.error("LDAP sync job %s not found.", job_id)
            return

        if not job.mark_running():
            logger.info("Job %s is no longer pending (cancelled?); skipping LDAP sync.", job_id)
            return
        job.append_log(f"Initializing LDAP directory sync for tenant: {tenant_slug}...")

        log_stream = JobLogStream(job)
        try:
            call_command("sync_tenant_ldap", tenant=tenant_slug, stdout=log_stream, stderr=log_stream)
            log_stream.flush()
            job.append_log("LDAP directory sync execution finished.")
            job.mark_completed(result={"status": "success"})
            Notification.objects.create(
                user=ctx.user,
                kind=Notification.KIND_PERSONAL,
                subject=_("LDAP Sync Complete"),
                message=_("LDAP directory sync for tenant '%(tenant)s' completed successfully.")
                % {"tenant": tenant_slug},
                level=Notification.LEVEL_SUCCESS,
                target_url=reverse_job_detail(job.pk),
            )
        except IntegrationError as exc:
            _record_failure(job, ctx.user, tenant_slug, exc, log_stream)
        # broad except: task-isolation: unknown LDAP task failures become a safe recorded failure
        except Exception as exc:
            request_id = get_current_request_id()
            error = IntegrationUnexpectedError(
                context=IntegrationContext(
                    provider="ldap",
                    operation="sync",
                    tenant_id=tenant_id,
                    actor_id=user_id,
                    request_id=str(request_id) if request_id else None,
                ),
                cause_type=type(exc).__name__,
            )
            _record_failure(job, ctx.user, tenant_slug, error, log_stream)


def _record_failure(job, user, tenant_slug, error, log_stream):
    log_stream.flush()
    extra = error.log_extra()
    logger.error(
        "LDAP sync failed at an external integration boundary integration=%s",
        extra["integration"],
        extra=extra,
    )
    context = error.context
    job.append_log(
        "Integration failure: "
        f"code={error.code}; disposition={error.disposition.value}; provider={context.provider}; "
        f"operation={context.operation}; tenant_id={context.tenant_id}; actor_id={context.actor_id}; "
        f"request_id={context.request_id}"
    )
    safe_message = error.display_message()
    job.mark_failed(safe_message)
    Notification.objects.create(
        user=user,
        kind=Notification.KIND_PERSONAL,
        subject=_("LDAP Sync Failed"),
        message=_("LDAP directory sync for tenant '%(tenant)s' failed: %(error)s")
        % {"tenant": tenant_slug, "error": safe_message},
        level=Notification.LEVEL_DANGER,
        target_url=reverse_job_detail(job.pk),
    )
