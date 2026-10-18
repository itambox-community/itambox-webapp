from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.perf.safety import is_loopback_ip, require_local_database, require_loopback_http_url


class PerformanceSafetyTests(unittest.TestCase):
    def test_loopback_addresses_are_recognized(self):
        self.assertTrue(is_loopback_ip("127.0.0.1"))
        self.assertTrue(is_loopback_ip("::1"))
        self.assertFalse(is_loopback_ip("192.168.1.10"))
        self.assertFalse(is_loopback_ip("not-an-ip"))

    def test_database_guard_rejects_remote_hosts(self):
        for host in ("", "localhost", "127.0.0.1", "::1"):
            with self.subTest(host=host):
                require_local_database({"HOST": host})
        with self.assertRaisesRegex(RuntimeError, "non-local database host"):
            require_local_database({"HOST": "db.example.invalid"})

    def test_http_origin_guard_only_accepts_loopback_origins(self):
        for value in ("http://127.0.0.1:8000", "http://[::1]:8000", "http://localhost:8000/"):
            with self.subTest(value=value):
                require_loopback_http_url(value)
        for value in (
            "https://127.0.0.1:8000",
            "http://example.invalid",
            "http://127.0.0.1:8000/path",
            "http://user:password@127.0.0.1:8000",
            "http://127.0.0.1:not-a-port",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                require_loopback_http_url(value)

    def test_wsgi_wrapper_denies_non_loopback_clients(self):
        repo_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo_root))
        sys.path.insert(0, str(repo_root / "itambox"))
        with patch.dict(os.environ, {"ITAMBOX_ENV": "dev", "DJANGO_SETTINGS_MODULE": "scripts.perf.queue_settings"}):
            from scripts.perf import perf_wsgi

        responses = []
        body = b"".join(
            perf_wsgi.application(
                {"REMOTE_ADDR": "10.66.6.20", "PATH_INFO": "/"},
                lambda status, headers: responses.append(status),
            )
        )
        self.assertEqual(responses, ["403 Forbidden"])
        self.assertIn(b"loopback access required", body)


if __name__ == "__main__":
    unittest.main()
