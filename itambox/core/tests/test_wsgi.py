"""The WSGI entry point loads the URLconf at import time, not on first request."""

import importlib
import sys
from unittest import mock

from django.urls import get_resolver

import core.wsgi


def test_importing_wsgi_reads_url_patterns():
    resolver = mock.MagicMock()
    with mock.patch("django.urls.get_resolver", return_value=resolver) as patched:
        reloaded = importlib.reload(core.wsgi)
    try:
        patched.assert_called_once_with()
        assert resolver.url_patterns is not None
        assert hasattr(reloaded, "application")
    finally:
        importlib.reload(core.wsgi)


def test_wsgi_module_populates_real_resolver():
    importlib.reload(core.wsgi)
    assert get_resolver().url_patterns
    assert "core.urls" in sys.modules
