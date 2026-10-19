"""Issue #913: the Change Log widget must hide what its option promises.

The widget's "Hide Internal Event Entries" option used to exclude
``extras.event`` change rows, a type the change log never records, so the feed
stayed dominated by background-job bookkeeping and system-generated entries.

These tests pin the corrected meaning of the option: with it enabled,
``core.job`` changes and changes without an acting user disappear next to the
legacy ``extras.event`` exclusion; with it disabled everything is visible
again; and the exclusion composes with the action and object-type filters
instead of replacing them.
"""

import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase
from django.utils import timezone

from core.managers import (
    set_current_all_accessible,
    set_current_membership,
    set_current_tenant,
    set_current_tenant_group,
)
from core.models import Job, ObjectChange
from core.tests.mixins import grant
from extras.dashboard.widgets import ChangelogWidget
from extras.models import Event
from itambox.middleware import set_current_user
from organization.models import Membership, Role, Tenant

User = get_user_model()


class ChangelogWidgetNoiseTests(TestCase):
    """The hide-internal-entries option hides job and system-generated rows."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Noise AG", slug="i913-noise")
        self.user = User.objects.create_user(username="i913-user", password="pw")
        role = Role.objects.create(
            tenant=self.tenant,
            name="Change log reader",
            permissions=["core.view_objectchange"],
        )
        grant(self.user, self.tenant, role)
        self.membership = Membership.objects.get(user=self.user, tenant=self.tenant)

        # Mirror CurrentUserMiddleware / TenantMiddleware so the widget reads the
        # real manager scope instead of an empty one.
        set_current_user(self.user)
        set_current_tenant(self.tenant)
        set_current_tenant_group(None)
        set_current_membership(self.membership)
        set_current_all_accessible(False)

        request = RequestFactory().get("/")
        request.user = self.user
        request.active_tenant = self.tenant
        request.active_tenant_group = None
        request.active_membership = self.membership
        request.active_all_accessible = False
        self.request = request

        tenant_ct = ContentType.objects.get_for_model(Tenant)
        job_ct = ContentType.objects.get_for_model(Job)
        event_ct = ContentType.objects.get_for_model(Event)

        self.human_update = self._change("human-tenant-updated", tenant_ct, self.user, action="update")
        self.human_create = self._change("human-tenant-created", tenant_ct, self.user, action="create")
        self.job_update = self._change("job-bookkeeping", job_ct, self.user, action="update")
        self.system_update = self._change("system-alert-sweep", tenant_ct, None, action="update")
        self.event_update = self._change("legacy-event-row", event_ct, self.user, action="update")

    def _change(self, repr_, content_type, user, *, action="update"):
        return ObjectChange._base_manager.create(
            tenant=self.tenant,
            user=user,
            user_name=user.username if user else "system",
            request_id=uuid.uuid4(),
            time=timezone.now() - timedelta(seconds=len(repr_)),
            action=action,
            changed_object_type=content_type,
            changed_object_id=self.tenant.pk,
            object_repr=repr_,
        )

    def _widget(self, **config):
        return ChangelogWidget({"config": config})

    def _visible_reprs(self, widget):
        return {change.object_repr for change in widget.get_context(self.request)["recent_changes"]}

    def test_option_enabled_hides_job_and_system_entries(self):
        visible = self._visible_reprs(self._widget(hide_event_noise=True))

        self.assertEqual(visible, {"human-tenant-updated", "human-tenant-created"})

    def test_option_defaults_to_hiding_noise(self):
        visible = self._visible_reprs(ChangelogWidget())

        self.assertEqual(visible, {"human-tenant-updated", "human-tenant-created"})

    def test_option_disabled_shows_job_and_system_entries_again(self):
        visible = self._visible_reprs(self._widget(hide_event_noise=False))

        self.assertEqual(
            visible,
            {
                "human-tenant-updated",
                "human-tenant-created",
                "job-bookkeeping",
                "system-alert-sweep",
                "legacy-event-row",
            },
        )

    def test_exclusion_composes_with_action_and_object_type_filters(self):
        # Without the noise exclusion this selection would return the job update
        # as well; without the action filter it would return the human creation.
        visible = self._visible_reprs(
            self._widget(
                hide_event_noise=True,
                action_filter="update",
                models=["organization.tenant", "core.job"],
            )
        )

        self.assertEqual(visible, {"human-tenant-updated"})

    def test_rendered_widget_shows_only_human_activity(self):
        """Assert the queryset contract through the rendered context, not query counts."""
        html = self._widget(hide_event_noise=True).render(self.request)

        self.assertIn("human-tenant-updated", html)
        self.assertIn("human-tenant-created", html)
        self.assertNotIn("job-bookkeeping", html)
        self.assertNotIn("system-alert-sweep", html)
        self.assertNotIn("legacy-event-row", html)

    def test_help_text_describes_what_is_hidden(self):
        help_text = str(ChangelogWidget.ConfigForm.base_fields["hide_event_noise"].help_text).lower()

        self.assertIn("system-generated", help_text)
        self.assertIn("background job", help_text)
