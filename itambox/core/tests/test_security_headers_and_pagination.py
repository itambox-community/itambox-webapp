import json
import re
from pathlib import Path

import django_filters
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from model_bakery import baker
from rest_framework.test import APIRequestFactory

from assets.models import Asset, AssetType, Manufacturer
from core.filters import BaseFilterSet
from core.managers import set_current_tenant
from core.paginator import EnhancedPaginator
from itambox.api.pagination import ITAMBoxPagination
from itambox.middleware import CSPMiddleware
from organization.models import Location, Tenant


class MockAssetFilterSet(BaseFilterSet):
    location = django_filters.ModelChoiceFilter(queryset=Location.objects.all())
    manufacturer = django_filters.ModelChoiceFilter(queryset=Manufacturer.objects.all())

    class Meta:
        model = Asset
        fields = []


class EnhancedPaginatorTests(SimpleTestCase):
    def test_enhanced_page_marks_gaps_between_visible_pages(self):
        paginator = EnhancedPaginator(range(100), 10)

        self.assertEqual(paginator.page(5).smart_pages(), [1, False, 3, 4, 5, 6, 7, False, 10])


class MitigationsPhase2Tests(TestCase):
    def test_base_template_disables_htmx_runtime_indicator_styles(self):
        template = Path(__file__).parents[2] / "templates" / "base.html"
        match = re.search(r'<meta name="htmx-config"\s+content=\'([^\']+)\'>', template.read_text())

        self.assertIsNotNone(match)
        config = json.loads(match.group(1))
        self.assertFalse(config["includeIndicatorStyles"])
        self.assertFalse(config["scrollIntoViewOnBoost"])

    def test_pagination_limit_zero_capped(self):
        from rest_framework.request import Request

        factory = APIRequestFactory()
        wsgi_req = factory.get("/api/assets/", {"limit": "0"})
        request = Request(wsgi_req)

        paginator = ITAMBoxPagination()
        limit = paginator.get_limit(request)
        self.assertIsNotNone(limit)
        self.assertEqual(limit, paginator.default_limit)

    def test_csp_middleware_generates_nonce(self):
        def get_response(req):
            return HttpResponse("Hello")

        middleware = CSPMiddleware(get_response)
        req = RequestFactory().get("/")

        response = middleware(req)
        self.assertTrue(hasattr(req, "csp_nonce"))
        nonce = req.csp_nonce
        self.assertTrue(len(nonce) > 10)

        csp_header = response["Content-Security-Policy"]
        self.assertIn(f"'nonce-{nonce}'", csp_header)

        # Extract directives to inspect script-src
        directives = {}
        for directive in csp_header.split("; "):
            parts = directive.strip().split(" ", 1)
            if len(parts) == 2:
                directives[parts[0]] = parts[1]

        self.assertIn("script-src", directives)
        self.assertNotIn("'unsafe-inline'", directives["script-src"])

    def test_csp_middleware_scopes_inline_style_relaxation_to_attributes(self):
        def get_response(req):
            return HttpResponse("Hello")

        middleware = CSPMiddleware(get_response)
        request = RequestFactory().get("/")
        response = middleware(request)

        csp_header = response["Content-Security-Policy"]
        directives = {}
        for directive in csp_header.split("; "):
            parts = directive.strip().split(" ", 1)
            if len(parts) == 2:
                directives[parts[0]] = parts[1]

        self.assertNotIn("'unsafe-inline'", directives["style-src"])
        self.assertIn("'nonce-{}'".format(request.csp_nonce), directives["style-src"])
        self.assertEqual(directives["style-src-attr"], "'unsafe-inline'")
        self.assertNotIn("'unsafe-inline'", directives["style-src-elem"])
        self.assertIn("'nonce-{}'".format(request.csp_nonce), directives["style-src-elem"])

        request_without_nonce = RequestFactory().get("/")
        response_without_nonce = middleware.process_response(request_without_nonce, HttpResponse("Hello"))
        self.assertIn("style-src-attr 'unsafe-inline'", response_without_nonce["Content-Security-Policy"])
        self.assertNotIn("style-src 'self' 'unsafe-inline'", response_without_nonce["Content-Security-Policy"])
        self.assertNotIn("style-src-elem 'self' 'unsafe-inline'", response_without_nonce["Content-Security-Policy"])

    def test_base_filter_set_scopes_choices(self):
        # Create Tenants
        tenant_a = baker.make(Tenant, name="Tenant A", slug="tenant-a")
        tenant_b = baker.make(Tenant, name="Tenant B", slug="tenant-b")

        # Create locations scoped to tenants
        loc_a = baker.make(Location, tenant=tenant_a)
        loc_b = baker.make(Location, tenant=tenant_b)

        # Create manufacturers
        mfg_a = baker.make(Manufacturer)
        mfg_b = baker.make(Manufacturer)

        # Link manufacturer A to tenant A's assets
        type_a = baker.make(AssetType, manufacturer=mfg_a)
        baker.make(Asset, tenant=tenant_a, asset_type=type_a)

        # Link manufacturer B to tenant B's assets
        type_b = baker.make(AssetType, manufacturer=mfg_b)
        baker.make(Asset, tenant=tenant_b, asset_type=type_b)

        # Set active tenant context to Tenant A
        set_current_tenant(tenant_a)
        try:
            fs = MockAssetFilterSet(data={})

            # Verify locations are filtered to Tenant A
            loc_qs = fs.filters["location"].queryset
            self.assertIn(loc_a, loc_qs)
            self.assertNotIn(loc_b, loc_qs)

            # Verify manufacturers are filtered to Tenant A
            mfg_qs = fs.filters["manufacturer"].queryset
            self.assertIn(mfg_a, mfg_qs)
            self.assertNotIn(mfg_b, mfg_qs)
        finally:
            set_current_tenant(None)
