import re
import unittest
from pathlib import Path


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.html = (root / "manus-obtima.html").read_text(encoding="utf-8")
        cls.backend = (root / "main.py").read_text(encoding="utf-8")
        cls.dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        cls.requirements = (root / "requirements.txt").read_text(encoding="utf-8")
        cls.render_config = (root / "render.yaml").read_text(encoding="utf-8")

    def test_sse_endpoint_is_same_origin_and_not_hardcoded(self):
        self.assertIn("new URL('/execute/stream',window.location.href)", self.html)
        self.assertNotIn("localhost:8000", self.html)
        self.assertNotIn("127.0.0.1:8000", self.html)

    def test_live_panel_only_opens_for_browser_tasks(self):
        self.assertIn("if(data.browser_task)showPanel();else panel.hidden=true;", self.html)
        self.assertIn('"browser_task": browser_task', self.backend)

    def test_live_panel_is_positioned_above_the_composer(self):
        self.assertIn("function positionPanel()", self.html)
        self.assertIn("composer.getBoundingClientRect()", self.html)

    def test_account_profiles_are_a_clear_ui_only_preview(self):
        self.assertIn('@app.get("/profiles"', self.backend)
        self.assertIn('id="metal-appbar"', self.html)
        self.assertIn('href="/profiles" data-app-route="profiles"', self.html)
        self.assertIn('id="profile-page"', self.html)
        self.assertIn('data-profile-preview="Google"', self.html)
        self.assertIn('data-profile-preview="GitHub"', self.html)
        self.assertIn("No passwords are requested or stored.", self.html)
        self.assertIn("Account linking is not active in this preview.", self.html)
        profile_markup = self.html.split('id="profile-page"', 1)[1].split("</section>", 1)[0]
        self.assertNotIn('type="password"', profile_markup)
        self.assertNotIn("/auth/oauth", self.backend)

    def test_browser_execution_requires_headed_chromium_and_hides_steps(self):
        self.assertIn("headless=False", self.backend)
        self.assertIn("Display(visible=False", self.backend)
        self.assertNotIn("using headless Chromium for live screenshots", self.backend)
        self.assertNotIn("Step {index} of {len(plan.steps)}", self.backend)

    def test_incomplete_sse_stream_is_reported_as_an_error(self):
        self.assertIn("if(!terminalEventReceived)throw new Error", self.html)
        self.assertIn("terminalEventReceived=true", self.html)

    def test_unsupported_attachments_are_rejected_without_clearing_state(self):
        self.assertIn("supportsAttachments:false", self.html)
        self.assertIn("payload.files.length&&adapter&&adapter.supportsAttachments===false", self.html)

    def test_playwright_image_and_python_package_versions_match(self):
        image = re.search(r"(?m)^FROM mcr\.microsoft\.com/playwright/python:v([\d.]+)-noble$", self.dockerfile)
        package = re.search(r"(?m)^playwright==([\d.]+)$", self.requirements)

        self.assertIsNotNone(image)
        self.assertIsNotNone(package)
        self.assertEqual(image.group(1), package.group(1))
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH=/ms-playwright", self.dockerfile)
        self.assertIn("xvfb xauth", self.dockerfile)
        self.assertIn("value: /ms-playwright", self.render_config)
        self.assertNotIn("install --with-deps", self.dockerfile)
        self.assertIn("uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}", self.dockerfile)


if __name__ == "__main__":
    unittest.main()
