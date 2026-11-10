from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from itambox.ratelimit import RateLimitMiddleware

User = get_user_model()


@override_settings(
    # Keep limits at their production defaults but make them explicit so the
    # test is independent of any environment overrides.
    RATELIMIT_LIMIT=5,
    RATELIMIT_PERIOD=60,
    RATELIMIT_CACHE="default",
)
class AuthenticatedRateLimitTestCase(TestCase):
    """
    Regression for the removed authenticated bypass in RateLimitMiddleware.

    Previously the middleware short-circuited for any authenticated user,
    letting a logged-in account hammer email-sending endpoints
    (/accounts/password_reset/, /organization/invite-user/, ...) without limit.
    The bypass is gone: sensitive paths are now IP-rate-limited regardless of
    auth, so an authenticated client must eventually receive HTTP 429.
    """

    # A sensitive path covered by RateLimitMiddleware.rate_limited_paths. GET is
    # enough — the middleware limits by path prefix, independent of HTTP method.
    # /accounts/login/ is Django's default LoginView: it renders the login form
    # (200) even for an authenticated user and needs no tenant context, so the
    # request reaches the rate-limit counter without the view 500-ing.
    LIMITED_PATH = "/accounts/login/"

    def setUp(self):
        # Counters live in the 'default' cache (LocMemCache under tests); clear
        # so counts start fresh and don't leak in from another test.
        caches["default"].clear()
        self.user = User.objects.create_user(username="ratelimit_user", password="password123")
        self.client.force_login(self.user)

    def test_authenticated_user_is_eventually_rate_limited(self):
        # Hammer the path well past the limit (5/period). With the bypass gone,
        # an authenticated user must hit 429.
        statuses = [self.client.get(self.LIMITED_PATH).status_code for _ in range(10)]
        self.assertIn(
            429,
            statuses,
            msg=(
                "Authenticated user was never rate limited on "
                f"{self.LIMITED_PATH}; the auth bypass appears to still exist. "
                f"Statuses: {statuses}"
            ),
        )

    def test_bypass_does_not_reset_counter_for_authenticated_user(self):
        # The 6th request (count already == limit) must be the 429, proving the
        # counter is being incremented for the authenticated user rather than
        # skipped.
        for _ in range(5):
            self.assertNotEqual(self.client.get(self.LIMITED_PATH).status_code, 429)
        self.assertEqual(self.client.get(self.LIMITED_PATH).status_code, 429)


@override_settings(
    RATELIMIT_LIMIT=5,
    RATELIMIT_PERIOD=60,
    RATELIMIT_CACHE="default",
)
class RateLimitCacheOutageTestCase(TestCase):
    """
    Regression: RateLimitMiddleware must fail OPEN when the cache backend
    (Redis/Valkey) is unreachable, not propagate the exception into a 500.

    Before the fix, rl_cache.get()/add()/incr() were called with no try/except
    around them, so a cache-backend outage on a rate-limited path (login,
    password reset, invite) 500'd every request across all tenants.
    """

    LIMITED_PATH = "/accounts/login/"

    def test_cache_outage_fails_open_not_500(self):
        broken_cache = mock.Mock()
        broken_cache.get.side_effect = ConnectionError("cache backend unreachable")

        with mock.patch("itambox.ratelimit._get_cache", return_value=broken_cache):
            response = self.client.get(self.LIMITED_PATH)

        self.assertEqual(
            response.status_code,
            200,
            msg=(
                "A cache backend outage must not 500 a rate-limited request; "
                f"got {response.status_code} instead of the expected fail-open 200."
            ),
        )

    def test_counter_failure_fails_open_and_is_logged(self):
        broken_cache = mock.Mock()
        broken_cache.add.side_effect = ConnectionError("cache backend unreachable")

        with mock.patch("itambox.ratelimit._get_cache", return_value=broken_cache):
            with self.assertLogs("itambox.ratelimit", level="ERROR") as logs:
                response = self.client.get(self.LIMITED_PATH)

        self.assertEqual(response.status_code, 200)
        self.assertIn("failing open", logs.output[0])


@override_settings(RATELIMIT_LIMIT=1, RATELIMIT_PERIOD=60, RATELIMIT_CACHE="default")
class RateLimitAtomicAdmissionTestCase(SimpleTestCase):
    """Admission must come from the atomic add()/incr() result, never a prior read."""

    PATH = "/accounts/login/"

    def _run(self, fake_cache):
        downstream = mock.Mock(return_value=HttpResponse("ok"))
        request = RequestFactory().get(self.PATH)
        with mock.patch("itambox.ratelimit._get_cache", return_value=fake_cache):
            response = RateLimitMiddleware(downstream)(request)
        return response, downstream

    def test_initialization_loser_is_counted_and_denied(self):
        cache = mock.Mock()
        cache.get.return_value = None  # a stale read must not matter
        cache.add.return_value = False  # another request initialized the counter
        cache.incr.return_value = 2
        response, downstream = self._run(cache)
        self.assertEqual(response.status_code, 429)
        downstream.assert_not_called()
        cache.incr.assert_called_once()

    def test_stale_read_below_limit_does_not_admit(self):
        cache = mock.Mock()
        cache.get.return_value = 0  # both racers saw limit - 1
        cache.add.return_value = False
        cache.incr.return_value = 2  # the other racer incremented first
        response, downstream = self._run(cache)
        self.assertEqual(response.status_code, 429)
        downstream.assert_not_called()

    def test_first_request_admitted_and_counted_via_add(self):
        cache = mock.Mock()
        cache.add.return_value = True
        response, downstream = self._run(cache)
        self.assertEqual(response.status_code, 200)
        downstream.assert_called_once()
        cache.add.assert_called_once()
        cache.incr.assert_not_called()

    def test_expired_between_add_and_incr_reinitializes_and_admits(self):
        cache = mock.Mock()
        cache.add.side_effect = [False, True]
        cache.incr.side_effect = ValueError("expired")
        response, downstream = self._run(cache)
        self.assertEqual(response.status_code, 200)
        downstream.assert_called_once()
        self.assertEqual(cache.add.call_count, 2)

    def test_expiry_race_loser_joins_new_window_and_is_denied(self):
        cache = mock.Mock()
        cache.add.side_effect = [False, False]
        cache.incr.side_effect = [ValueError("expired"), 2]
        response, downstream = self._run(cache)
        self.assertEqual(response.status_code, 429)
        downstream.assert_not_called()

    def test_login_discovery_has_a_separate_counter_under_login_prefix(self):
        cache = mock.Mock()
        cache.add.return_value = True
        downstream = mock.Mock(return_value=HttpResponse("ok"))
        request = RequestFactory().post("/accounts/login/discover/")

        with mock.patch("itambox.ratelimit._get_cache", return_value=cache):
            response = RateLimitMiddleware(downstream)(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            cache.add.call_args.args[0],
            "ratelimit_127.0.0.1_/accounts/login/discover/",
        )
        downstream.assert_called_once()

    def test_budget_is_exact_with_real_cache(self):
        caches["default"].clear()
        with override_settings(RATELIMIT_LIMIT=3):
            statuses = [self._run(caches["default"])[0].status_code for _ in range(5)]
        self.assertEqual(statuses, [200, 200, 200, 429, 429])
