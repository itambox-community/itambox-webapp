from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from core.tests.mixins import grant
from organization.models import Role, Tenant
from users.models import Token

User = get_user_model()

QUERY = "{ __typename }"


class GraphQLThrottleTests(TestCase):
    """GET and POST queries draw on one shared request budget (#729)."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(username="gql_throttle", email="gql_throttle@example.com", password="pw")
        self.url = reverse("graphql")
        rates = {"user": "3/min", "anon": "3/min"}
        for cls in (UserRateThrottle, AnonRateThrottle):
            patcher = mock.patch.object(cls, "THROTTLE_RATES", rates)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _get(self, client=None):
        return (client or self.client).get(self.url, {"query": QUERY})

    def test_session_get_queries_are_throttled(self):
        self.client.force_login(self.user)
        statuses = [self._get().status_code for _ in range(5)]
        self.assertEqual(statuses[:3], [200, 200, 200])
        self.assertEqual(statuses[3:], [429, 429])
        self.assertIn("throttled", self._get().json()["errors"][0]["message"])

    def test_get_and_post_share_one_budget(self):
        self.client.force_login(self.user)
        self.assertEqual(self._get().status_code, 200)
        self.assertEqual(self._get().status_code, 200)
        post = self.client.post(self.url, data={"query": QUERY}, content_type="application/json")
        self.assertEqual(post.status_code, 200)
        self.assertEqual(self._get().status_code, 429)
        post = self.client.post(self.url, data={"query": QUERY}, content_type="application/json")
        self.assertEqual(post.status_code, 429)

    def test_unauthenticated_get_is_still_redirected_to_login(self):
        response = self._get()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_token_authenticated_post_still_works_and_is_throttled(self):
        tenant = Tenant.objects.create(name="GQL Throttle Tenant", slug="gql-throttle-tenant")
        role = Role.objects.create(tenant=tenant, name="GQL Throttle Role", permissions=["assets.view_asset"])
        grant(self.user, tenant, role)
        token = Token.objects.create(user=self.user)
        headers = {"HTTP_AUTHORIZATION": f"Token {token.key}"}
        statuses = [
            self.client.post(self.url, data={"query": QUERY}, content_type="application/json", **headers).status_code
            for _ in range(4)
        ]
        self.assertEqual(statuses, [200, 200, 200, 429])

    def test_graphiql_shell_request_does_not_consume_budget(self):
        self.client.force_login(self.user)
        for _ in range(6):
            self.assertEqual(self.client.get(self.url, headers={"accept": "text/html"}).status_code, 200)
        self.assertEqual(self._get().status_code, 200)

    def test_complexity_protection_still_applies_to_get(self):
        self.client.force_login(self.user)
        deep = "{ " * 40 + "__typename" + " }" * 40
        response = self.client.get(self.url, {"query": deep})
        self.assertEqual(response.status_code, 400)
