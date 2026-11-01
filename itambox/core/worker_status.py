"""Best-effort detection of a running django-q cluster.

Webhook and notification deliveries are enqueued to the background worker
(``python manage.py qcluster``). When no worker runs, tasks pile up in the broker
queue and nothing is ever delivered — the single most common reason the
Webhook/Event-Rule features look like a no-op. These helpers let the UI surface
that state instead of failing silently.

Cluster heartbeats live in the cache configured for django-q (``Conf.CACHE``).
With a local-memory cache the web process and the qcluster process hold *separate*
caches, so a running worker is invisible from here. In that case we report
``detectable=False`` rather than a misleading "offline".
"""

import logging

from django.conf import settings
from django.db import DatabaseError
from django_q.conf import Conf
from django_q.models import OrmQ
from django_q.status import Stat

logger = logging.getLogger(__name__)


def get_worker_status():
    """Return a dict describing background-worker availability.

    Keys:
        detectable    — whether live status can be observed from this process
        online        — a cluster heartbeat was seen (only meaningful if detectable)
        cluster_count — number of live clusters seen
        queued_tasks  — tasks waiting in the ORM broker queue (None if unavailable)
        cache_alias   — the cache alias django-q uses for heartbeats
    """

    result = {
        "detectable": True,
        "online": False,
        "cluster_count": 0,
        "queued_tasks": None,
        "cache_alias": "default",
    }

    cache_alias = Conf.CACHE
    result["cache_alias"] = cache_alias

    backend = (settings.CACHES.get(cache_alias, {}) or {}).get("BACKEND", "")
    if not backend or "locmem" in backend.lower() or "dummy" in backend.lower():
        # Heartbeats can't be shared across processes — don't claim "offline".
        result["detectable"] = False

    try:
        clusters = Stat.get_all()
        result["cluster_count"] = len(clusters)
        result["online"] = len(clusters) > 0
    # broad except: boundary-isolation: django-q reads the heartbeat through the cache backend
    # (redis, memcached, ...), whose connection errors are not enumerable; the page degrades to "offline"
    except Exception:
        logger.warning("Could not read django-q cluster heartbeats", exc_info=True)

    # Pending tasks in the ORM broker live in the shared DB, so this count is
    # reliable across processes regardless of the cache backend.
    try:
        result["queued_tasks"] = OrmQ.objects.count()
    except DatabaseError:
        logger.warning("Could not count queued django-q tasks", exc_info=True)

    return result
