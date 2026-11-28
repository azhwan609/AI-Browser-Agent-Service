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

    def test_mobile_layout_removes_top_gap_and_anchors_composer_at_bottom(self):
        css = re.findall(r"<style\b[^>]*>(.*?)</style>", self.html, re.S)[-1]
        self.assertIn("@media(max-width:600px)", css)
        self.assertIn("body{min-height:100vh;min-height:100dvh}", css)
        self.assertIn("#metal-appbar{margin-top:0}", css)
        self.assertIn(".composer-wrap{margin-top:auto;padding-bottom:env(safe-area-inset-bottom,0px)}", css)
        self.assertIn(".composer{padding-bottom:0}", css)

    def test_sse_endpoint_is_same_origin_and_not_hardcoded(self):
        self.assertIn("new URL('/execute/stream',window.location.href)", self.html)
        self.assertNotIn("localhost:8000", self.html)
        self.assertNotIn("127.0.0.1:8000", self.html)

    def test_login_credentials_use_live_handoff_or_masked_encrypted_prefill(self):
        self.assertIn('id="login-credentials-dialog"', self.html)
        self.assertIn('type="password" autocomplete="current-password"', self.html)
        self.assertIn('id="login-mode-live"', self.html)
        self.assertIn("Enter at the live login prompt", self.html)
        self.assertIn("Remember encrypted on this device for this site", self.html)
        self.assertIn("never saved on Render", self.html)
        self.assertIn("if(credentials){requestBody.login_username=credentials.username;requestBody.login_password=credentials.password;}", self.html)
        self.assertIn("manual_login:manualLogin", self.html)
        self.assertIn("finally{streamTextCallback=null;clearLiveLoginPrompt();if(credentials)", self.html)
        self.assertIn("crypto.subtle.generateKey({name:'AES-GCM',length:256},false", self.html)
        self.assertIn("indexedDB.open('shiwa-login-vault-v1',1)", self.html)
        self.assertIn("return /^https?:$/.test(url.protocol)?url.origin:null", self.html)
        self.assertNotIn("localStorage.setItem", self.html)

    def test_live_login_prompt_uses_one_time_post_body_and_masked_password(self):
        self.assertIn("else if(name==='login_required'){showLiveLoginPrompt(data);}", self.html)
        self.assertIn("new URL('/execute/stream/login',window.location.href)", self.html)
        self.assertIn("session_token:submittedToken,username:submittedUsername,password:submittedPassword", self.html)
        self.assertIn("type=\"password\" autocomplete=\"current-password\" required maxlength=\"1000\"", self.html)
        self.assertIn("activeLoginToken=null", self.html)
        self.assertIn('@app.post("/execute/stream/login"', self.backend)
        self.assertIn("MANUAL_LOGIN_WAIT_SECONDS = 90", self.backend)
        self.assertIn("if payload.manual_login:", self.backend)
        self.assertIn('executor_kwargs["manual_login_callback"] = manual_login_callback', self.backend)

    def test_live_panel_only_opens_for_browser_tasks(self):
        self.assertIn("if(data.browser_task)showPanel();else panel.hidden=true;", self.html)
        self.assertIn('"browser_task": browser_task', self.backend)

    def test_live_panel_is_positioned_above_the_composer(self):
        self.assertIn("function positionPanel()", self.html)
        self.assertIn("composer.getBoundingClientRect()", self.html)

    def test_live_browser_has_silver_frame_and_secure_session_toolbar(self):
        self.assertIn(".live-browser-panel{border:3px solid #f8f9fa", self.html)
        self.assertIn('class="live-browser-toolbar"', self.html)
        self.assertIn('class="live-browser-engine">CHROMIUM</span>', self.html)

    def test_success_response_links_to_the_visited_page_safely(self):
        self.assertIn("function safePageLink(raw)", self.html)
        self.assertIn("result.success===true?safePageLink(result.current_url):''", self.html)
        self.assertIn("className='live-browser-page-link'", self.html)
        self.assertIn("className='assistant-page-link'", self.html)
        self.assertIn("pageLink.rel='noopener noreferrer'", self.html)
        self.assertIn("private login session is not shared", self.html)

    def test_account_profiles_are_a_clear_ui_only_preview(self):
        self.assertIn('@app.get("/profiles"', self.backend)
        self.assertIn('id="metal-appbar"', self.html)
        self.assertIn('href="/profiles" data-app-route="profiles"', self.html)
        self.assertIn('id="profile-page"', self.html)
        self.assertIn('data-profile-preview="Google"', self.html)
        self.assertIn('data-profile-preview="GitHub"', self.html)
        self.assertIn("This server profile vault does not store passwords.", self.html)
        self.assertIn("Account linking is not active in this preview.", self.html)
        profile_markup = self.html.split('id="profile-page"', 1)[1].split("</section>", 1)[0]
        self.assertNotIn('type="password"', profile_markup)
        self.assertNotIn("/auth/oauth", self.backend)

    def test_mobile_top_controls_are_shifted_up_and_compact(self):
        self.assertIn("min-height:44px;margin:2px auto 2px", self.html)
        self.assertIn("header{height:58px", self.html)
        self.assertIn(".toprow{padding:7px 10px}", self.html)

    def test_browser_execution_requires_headed_chromium_and_hides_steps(self):
        self.assertIn("headless=False", self.backend)
        self.assertIn("Display(visible=False", self.backend)
        self.assertNotIn("using headless Chromium for live screenshots", self.backend)
        self.assertNotIn("Step {index} of {len(plan.steps)}", self.backend)

    def test_incomplete_sse_stream_is_reported_as_an_error(self):
        self.assertIn("if(!terminalEventReceived)throw new Error", self.html)
        self.assertIn("terminalEventReceived=true", self.html)

    def test_chat_sse_deltas_are_rendered_as_they_arrive(self):
        self.assertIn("else if(name==='delta')", self.html)
        self.assertIn("streamTextCallback(data.text)", self.html)
        self.assertIn("_generate_chat_stream_sync", self.backend)
        self.assertIn('yield _sse_event("delta", {"text": delta})', self.backend)

    def test_render_config_sets_model_and_declares_api_key_as_secret(self):
        self.assertIn("value: llama-3.3-70b-versatile", self.render_config)
        self.assertIn("- key: GROQ_API_KEY\n        sync: false", self.render_config)

    def test_attachments_are_forwarded_as_bounded_context(self):
        self.assertIn("supportsAttachments:true", self.html)
        self.assertIn("Attachment:", self.html)
        self.assertIn("slice(0,12000)", self.html)

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
