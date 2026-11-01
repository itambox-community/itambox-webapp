"""
WSGI config for core project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application
from django.urls import get_resolver

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

application = get_wsgi_application()

# Import the URLconf now, while the worker boots, instead of inside the first
# request: LocaleMiddleware resolves it lazily, and that import drags in
# strawberry, graphql, saml2 and drf-spectacular (several seconds). Reading
# ``url_patterns`` only imports modules and builds the pattern tree; it opens no
# database connection. ``get_resolver`` is cached, so repeating this is a no-op.
get_resolver().url_patterns
