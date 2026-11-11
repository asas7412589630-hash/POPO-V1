from __future__ import annotations
import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "console"


class ConsoleAssetTests(unittest.TestCase):
    def setUp(self):
        self.html = (CONSOLE / "index.html").read_text(encoding="utf-8")

    def test_manifest_is_linked(self):
        self.assertIn('rel="manifest"', self.html)

    def test_manifest_is_valid_and_installable(self):
        path = CONSOLE / "manifest.webmanifest"
        if not path.exists():
            path = CONSOLE / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for field in ("name", "short_name", "start_url", "display", "icons"):
            self.assertIn(field, manifest)
        self.assertEqual(manifest["display"], "standalone")
        self.assertTrue(manifest["icons"])

    def test_i18n_script_loaded_before_app(self):
        i18n_at = self.html.find("i18n.js")
        app_at = self.html.find("app.js")
        self.assertGreaterEqual(i18n_at, 0, "i18n.js is not referenced")
        self.assertLess(i18n_at, app_at, "i18n.js must load before app.js")

    def test_no_external_network_dependency(self):
        # The console must work offline on a phone with no CDN reachable.
        self.assertNotRegex(self.html, r'(src|href)="https?://')

    def test_tab_order_matches_section_order(self):
        """Buttons and panels must be in the same order.

        They were not: the button order was setup/result/armor/tuning while the
        panels were setup/tuning/armor, so clicking 「人設」 lit up 「結果」 and
        displayed 「調校」. Two lists that must stay aligned need a test.
        """
        buttons = re.findall(r'<button data-tab="(\w+)"', self.html)
        sections = re.findall(r'<section id="tab-(\w+)"', self.html)
        self.assertEqual(buttons, sections,
                         "tab buttons and panels are in different orders")
        self.assertEqual(len(sections), len(set(sections)), "duplicate panel id")

    def test_every_tab_button_is_translatable(self):
        buttons = re.findall(r'<button data-tab="\w+"[^>]*>', self.html)
        missing = [b for b in buttons if "data-i18n" not in b]
        self.assertEqual(missing, [], "tab buttons without an i18n key")

    def test_translatable_nodes_declared(self):
        self.assertGreaterEqual(len(re.findall(r'data-i18n', self.html)), 1)

    def test_setup_explains_automatic_model_fingerprint_routing(self):
        app = (CONSOLE / "app.js").read_text(encoding="utf-8")
        # The supplied 2.7.9 console now describes the shared V1 family block.
        # Assert the stable UI anchors instead of an older explanatory sentence.
        self.assertIn('id="modelList"', self.html)
        self.assertIn("V1 Cinder", self.html)
        self.assertIn("select.className = 'armor-select'", app)
        self.assertIn("自動（依模型指紋）", app)

    def test_setup_update_posts_the_selected_profiles(self):
        app = (CONSOLE / "app.js").read_text(encoding="utf-8")
        self.assertRegex(app, r"api\('POST',\s*'/setup/models',\s*\{\s*models:\s*published,\s*profiles\s*\}\)")

    def test_model_block_picker_uses_server_family_choices(self):
        app = (CONSOLE / "app.js").read_text(encoding="utf-8")
        self.assertIn("state.armorFamilies = data.families || data.profiles || []", app)
        self.assertRegex(app, r"profiles\[m\]\s*=\s*chosen")

    def test_public_key_url_never_falls_back_to_console_origin(self):
        app = (CONSOLE / "app.js").read_text(encoding="utf-8")
        self.assertNotRegex(app, r"\|\|\s*state\.base\s*\+\s*['\"]/v1")
        self.assertIn("尚無可用的此範圍網址", app)


if __name__ == "__main__":
    unittest.main()


class RootRedirectTests(unittest.TestCase):
    """Opening a bare hostname must land on the console, not a JSON 404.

    A tunnel publishes a hostname with no path. The first thing anyone does is
    open that URL; a 404 there reads as "the deployment is broken".
    """

    def test_root_redirects_to_console(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        from popo.config import Config
        from popo.server.app import ApiHandler

        ApiHandler.config = Config(bind_host="127.0.0.1", require_client_key=False,
                                   data_dir="/tmp/popo_root_test")
        ApiHandler.reset_shared_state()
        server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/")
            response = conn.getresponse()
            response.read()
            conn.close()
            self.assertEqual(response.status, 302)
            self.assertEqual(response.getheader("Location"), "/app/")
        finally:
            server.shutdown()
            server.server_close()
