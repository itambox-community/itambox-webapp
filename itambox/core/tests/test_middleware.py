from importlib import import_module

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase

from itambox.middleware import (
    CurrentUserMiddleware,
    TenantMiddleware,
    get_current_request_id,
    get_current_user,
)
from organization.models import Tenant

User = get_user_model()


class CurrentUserMiddlewareTestCase(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username="testuser", password="password123", is_superuser=True)

    def test_current_user_middleware_contextvars(self):
        """Test that CurrentUserMiddleware correctly sets and cleans up request user and request ID."""
        request = self.factory.get("/")
        request.user = self.user

        middleware = CurrentUserMiddleware(get_response=lambda r: None)
        middleware.process_request(request)

        self.assertEqual(get_current_user(), self.user)
        self.assertIsNotNone(get_current_request_id())

        middleware.process_response(request, None)
        self.assertIsNone(get_current_user())
        self.assertIsNone(get_current_request_id())


class ScopeSwitchNoticeTests(TestCase):
    """The scope switcher drops a tenant filter that the new tenant/group scope
    already defines; the notice marker keeps that reset visible as a message."""

    def setUp(self):
        self.factory = RequestFactory()
        self.session_store = import_module(settings.SESSION_ENGINE).SessionStore
        self.user = User.objects.create_superuser(
            username="notice-superuser",
            email="notice-superuser@example.com",
            password="pw",
        )
        self.tenant = Tenant.objects.create(name="Notice Tenant", slug="notice-tenant")

    def _run(self, query, session=None):
        request = self.factory.get("/" + query)
        request.user = self.user
        request.session = session if session is not None else self.session_store()
        request._messages = FallbackStorage(request)
        CurrentUserMiddleware(get_response=lambda r: None).process_request(request)
        TenantMiddleware(get_response=lambda r: None).process_request(request)
        return request

    def test_scope_switch_notice_is_emitted(self):
        request = self._run(f"?switch_tenant={self.tenant.pk}&scope_notice=filters")

        stored = [str(message) for message in request._messages]
        self.assertEqual(len(stored), 1)
        self.assertIn("tenant filter was reset", stored[0])

    def test_notice_requires_the_switch_parameter(self):
        request = self._run("?scope_notice=filters")

        self.assertEqual(list(request._messages), [])

    def test_no_notice_without_the_marker(self):
        request = self._run(f"?switch_tenant={self.tenant.pk}")

        self.assertEqual(list(request._messages), [])

    def test_reloading_a_switch_url_does_not_replay_the_notice(self):
        session = self.session_store()
        switch_url = f"?switch_tenant={self.tenant.pk}&scope_notice=filters"

        first = self._run(switch_url, session)
        self.assertEqual(len(list(first._messages)), 1)

        reloaded = self._run(switch_url, session)

        self.assertEqual(list(reloaded._messages), [])

    def test_notice_is_silent_when_the_session_already_holds_the_scope(self):
        session = self.session_store()
        # The resolver stores integers while switch parameters arrive as strings.
        session["active_tenant_id"] = self.tenant.pk
        session.save()

        request = self._run(f"?switch_tenant={self.tenant.pk}&scope_notice=filters", session)

        self.assertEqual(list(request._messages), [])

    def test_notice_is_silent_when_the_target_keeps_the_tenant_filter(self):
        request = self._run(f"?switch_all_accessible=1&tenant={self.tenant.pk}&scope_notice=filters")

        self.assertEqual(list(request._messages), [])


class TenantMiddlewareExceptionSafetyTests(TestCase):
    def setUp(self):
        from core.managers import set_current_tenant, set_current_tenant_group

        self.tenant = Tenant.objects.create(name="Safety Tenant", slug="safety-tenant")
        self.user = User.objects.create_user(username="safety_user", password="pw", is_superuser=True)
        self.prev_tenant = Tenant.objects.create(name="Prev Tenant", slug="prev-tenant")
        set_current_tenant(self.prev_tenant)
        self.addCleanup(set_current_tenant, None)
        self.addCleanup(set_current_tenant_group, None)

    def _request(self):
        request = RequestFactory().get(f"/?switch_tenant={self.tenant.pk}")
        request.user = self.user
        request.session = import_module(settings.SESSION_ENGINE).SessionStore()
        request._messages = FallbackStorage(request)
        return request

    def test_base_exception_restores_previous_scope(self):
        from core.managers import get_current_tenant

        class Cancelled(BaseException):
            pass

        def view(request):
            self.assertEqual(get_current_tenant(), self.tenant)
            raise Cancelled

        with self.assertRaises(Cancelled):
            TenantMiddleware(view)(self._request())
        self.assertEqual(get_current_tenant(), self.prev_tenant)

    def test_failure_inside_process_request_restores_previous_scope(self):
        from unittest import mock

        from core.managers import get_current_tenant, set_current_tenant

        middleware = TenantMiddleware(lambda r: None)

        def boom(self_, request):
            set_current_tenant(self.tenant)
            raise KeyboardInterrupt

        with mock.patch.object(TenantMiddleware, "process_request", boom):
            with self.assertRaises(KeyboardInterrupt):
                middleware(self._request())
        self.assertEqual(get_current_tenant(), self.prev_tenant)

    def test_normal_response_restores_previous_scope(self):
        from core.managers import get_current_tenant

        sentinel = object()
        self.assertIs(TenantMiddleware(lambda r: sentinel)(self._request()), sentinel)
        self.assertEqual(get_current_tenant(), self.prev_tenant)
