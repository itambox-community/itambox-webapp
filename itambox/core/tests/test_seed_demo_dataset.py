"""The full demo seed runs end to end and produces the lifecycle records it promises."""

import io
import socket
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from assets.models import AssetDisposal, Warranty
from licenses.models import LicenseSeatAssignment


class SeedDemoDatasetTests(TestCase):
    def test_full_seed_creates_lifecycle_and_licensing_data(self):
        # The seed creates webhook endpoints whose hosts are validated by DNS lookup; resolve
        # them to a public address so the run does not depend on network access.
        public = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 0))]
        with patch("core.validators.socket.getaddrinfo", return_value=public):
            call_command("seed_data", force=True, stdout=io.StringIO(), stderr=io.StringIO())

        self.assertTrue(Warranty.objects.exists())
        self.assertTrue(AssetDisposal.objects.exists())
        self.assertTrue(LicenseSeatAssignment.objects.exists())
