"""Regression tests for #1045: the SSRF guard rejects every non-global address.

``100.64.0.0/10`` (RFC 6598 shared address space) is neither private, reserved,
loopback nor link-local in Python's ``ipaddress`` model, so an enumeration of
dangerous categories let it through. Both ``validate_external_url`` and the
``request_pinned`` sender (which re-validates at send time) must refuse it.
"""

import socket
from unittest.mock import MagicMock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from core.http import request_pinned
from core.validators import validate_external_url

BLOCKED_HOSTS = (
    "100.64.0.1",
    "100.100.100.200",
    "100.127.255.254",
    "[::ffff:100.100.100.200]",
    "[::ffff:10.0.0.1]",
    "[::ffff:127.0.0.1]",
    "127.0.0.1",
    "169.254.169.254",
    "10.0.0.1",
    "[::1]",
    "[fe80::1]",
    "[fc00::1]",
    "0.0.0.0",
    "224.0.0.1",
)


def _addrinfo(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]


class ValidateExternalUrlNonGlobalTests(SimpleTestCase):
    def test_literal_non_global_hosts_are_rejected(self):
        for host in BLOCKED_HOSTS:
            with self.subTest(host=host), self.assertRaises(ValidationError):
                validate_external_url(f"https://{host}/hook")

    def test_hostname_resolving_to_cgnat_is_rejected(self):
        for address in ("100.64.0.1", "100.100.100.200", "::ffff:100.100.100.200"):
            with self.subTest(address=address), patch("socket.getaddrinfo", return_value=_addrinfo(address)):
                with self.assertRaises(ValidationError):
                    validate_external_url("https://hooks.example.com/x")

    def test_any_resolved_address_being_non_global_rejects(self):
        infos = _addrinfo("93.184.216.34") + _addrinfo("100.100.100.200")
        with patch("socket.getaddrinfo", return_value=infos), self.assertRaises(ValidationError):
            validate_external_url("https://hooks.example.com/x")

    def test_scoped_ipv6_resolution_is_rejected(self):
        with patch("socket.getaddrinfo", return_value=_addrinfo("fe80::1%eth0")), self.assertRaises(ValidationError):
            validate_external_url("https://hooks.example.com/x")

    def test_public_addresses_are_accepted(self):
        for address in ("93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946", "::ffff:8.8.8.8"):
            with self.subTest(address=address), patch("socket.getaddrinfo", return_value=_addrinfo(address)):
                resolved = validate_external_url("https://hooks.example.com/x")
                self.assertEqual(len(resolved), 1)

    def test_allow_private_still_permits_internal_ranges(self):
        for host in ("100.100.100.200", "10.0.0.1", "127.0.0.1", "169.254.169.254", "[::ffff:100.100.100.200]"):
            with self.subTest(host=host):
                self.assertTrue(validate_external_url(f"http://{host}/x", allow_private=True))

    def test_unresolvable_host_still_fails_closed(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror()), self.assertRaises(ValidationError):
            validate_external_url("https://doesnotexist.invalid/x")


class RequestPinnedNonGlobalTests(SimpleTestCase):
    def _response(self):
        resp = MagicMock(status_code=200)
        resp.is_redirect = False
        resp.raw = None
        return resp

    @patch("requests.adapters.HTTPAdapter.send")
    def test_literal_non_global_hosts_are_never_sent(self, mock_send):
        for host in BLOCKED_HOSTS:
            with self.subTest(host=host), self.assertRaises(ValidationError):
                request_pinned("POST", f"https://{host}/hook", json={})
        mock_send.assert_not_called()

    @patch("requests.adapters.HTTPAdapter.send")
    def test_hostname_resolving_to_cgnat_is_never_sent(self, mock_send):
        for address in ("100.64.0.1", "100.100.100.200", "::ffff:100.100.100.200"):
            with self.subTest(address=address), patch("socket.getaddrinfo", return_value=_addrinfo(address)):
                with self.assertRaises(ValidationError):
                    request_pinned("POST", "https://hooks.example.com/x", json={})
        mock_send.assert_not_called()

    @patch("requests.adapters.HTTPAdapter.send")
    def test_public_address_is_still_pinned(self, mock_send):
        mock_send.return_value = self._response()
        with patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            request_pinned("POST", "https://hooks.example.com/x", json={})
        sent = mock_send.call_args[0][0]
        self.assertTrue(sent.url.startswith("https://93.184.216.34"))
        self.assertEqual(sent.headers["Host"], "hooks.example.com")
