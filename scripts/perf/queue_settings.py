"""Local settings overlay for the disposable qcluster throughput run.

Successful task rows are retained only in the isolated performance database so
the harness can observe completion. The application defaults remain untouched.
"""

import os

from core.settings.dev import *  # noqa: F403

Q_CLUSTER = {
    **Q_CLUSTER,  # noqa: F405
    "workers": int(os.environ.get("PERF_Q_WORKERS", "2")),
    "save_limit": 20_000,
    "sync": False,
}
