#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys


def main():
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
    if sys.argv[1:2] == ["plugins"] and "--check" in sys.argv:
        # The pre-flight must work when a broken plugin makes the settings import
        # (and therefore Django itself) abort, so it runs before Django is set up.
        # inline import: cycle: importing the plugin package must not load settings
        from itambox.plugins.preflight import main as preflight_main

        sys.exit(preflight_main(sys.argv[2:]))
    try:
        # inline import: optional-dependency: Django may be missing; the except
        # branch is the whole point of this block.
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
