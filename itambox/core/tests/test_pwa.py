import json
import re

from django.contrib.auth.models import AnonymousUser
from django.contrib.staticfiles import finders
from django.template import engines
from django.templatetags.static import static
from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve, reverse

from itambox.release import VERSION


class PWAInstallabilityTests(SimpleTestCase):
    def test_public_login_page_exposes_pwa_capabilities(self):
        response = self.client.get(reverse("login"))

        manifest_link = '<link rel="manifest" href="{}">'.format(reverse("manifest.json"))
        self.assertContains(response, manifest_link, html=True)
        self.assertContains(response, "navigator.serviceWorker.register")
        self.assertContains(response, reverse("service-worker.js"))

    def test_manifest_has_stable_identity_and_root_scope(self):
        response = self.client.get(reverse("manifest.json"))
        manifest = json.loads(response.content)

        self.assertEqual(manifest["id"], "/")
        self.assertEqual(manifest["scope"], "/")

    def test_manifest_declares_existing_installable_png_icons(self):
        response = self.client.get(reverse("manifest.json"))
        manifest = json.loads(response.content)

        icons_by_size = {icon["sizes"]: icon for icon in manifest["icons"] if icon["type"] == "image/png"}
        for size in ("192x192", "512x512"):
            with self.subTest(size=size):
                icon = icons_by_size[size]
                self.assertIn("any", icon.get("purpose", "any").split())
                static_path = icon["src"].removeprefix("/static/")
                self.assertIsNotNone(finders.find(static_path), static_path)

    def test_pwa_bootstrap_responses_are_not_cached(self):
        for url_name in ("manifest.json", "service-worker.js"):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertIn("no-cache", response.headers["Cache-Control"])

    def test_service_worker_precaches_manifest_png_icons(self):
        manifest = json.loads(self.client.get(reverse("manifest.json")).content)
        worker_source = self.client.get(reverse("service-worker.js")).content.decode()

        for icon in manifest["icons"]:
            if icon["type"] == "image/png":
                with self.subTest(icon=icon["src"]):
                    self.assertRegex(worker_source, rf"['\"]{re.escape(icon['src'])}['\"]")

    def test_shell_asset_urls_are_precached_with_the_release_version(self):
        css_url = f"{static('dist/itambox.css')}?v={VERSION}"
        js_url = f"{static('dist/itambox.js')}?v={VERSION}"

        public_page = self.client.get(reverse("login"))
        request = RequestFactory().get(reverse("dashboard"))
        request.user = AnonymousUser()
        request.resolver_match = resolve(reverse("dashboard"))
        request.csp_nonce = "test-nonce"
        authenticated_shell = engines["django"].get_template("base.html").render({}, request=request)
        worker_source = self.client.get(reverse("service-worker.js")).content.decode()
        precache_block = worker_source.split("const PRECACHE_ASSETS = [", 1)[1].split("];", 1)[0]
        precache_urls = re.findall(r"['\"]([^'\"]+)['\"]", precache_block)

        for asset_url in (css_url, js_url):
            with self.subTest(asset_url=asset_url):
                self.assertContains(public_page, asset_url)
                self.assertIn(asset_url, authenticated_shell)
                self.assertIn(asset_url, precache_urls)

        self.assertIn(f"const CACHE_NAME = 'itambox-pwa-cache-v{VERSION}';", worker_source)
