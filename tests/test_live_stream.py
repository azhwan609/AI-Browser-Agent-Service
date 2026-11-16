import asyncio
import base64
import os
import socket
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

import main


class LiveStreamTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.plan = main.BrowserPlan(
            goal="Read the example page",
            steps=[main.BrowserAction(action="navigate", target="https://example.com", value="")],
        )
        self.key = patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
        self.key.start()
        self.addCleanup(self.key.stop)

    def test_version_routes_return_version_tag(self):
        for path in ("/version", "/execute"):
            with self.subTest(path=path):
                response = self.client.get(path)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, "V2.0 - Chat Routing Active")
                self.assertTrue(response.headers["content-type"].startswith("text/plain"))

    def test_root_serves_the_frontend_adapter(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertIn("AI_BROWSER_AGENT_BACKEND_ADAPTER", response.text)

    def test_sse_event_format(self):
        event = main._sse_event("status", {"message": "Started"})
        self.assertTrue(event.startswith("event: status\ndata: "))
        self.assertTrue(event.endswith("\n\n"))
        self.assertIn('"message": "Started"', event)

    def test_stream_delivers_frame_status_and_complete(self):
        stale_frame = base64.b64encode(b"stale frame").decode("ascii")
        latest_frame = base64.b64encode(b"jpeg").decode("ascii")

        async def fake_execute(plan, frame_callback=None, progress_callback=None):
            await progress_callback({"message": "Step 1 of 1: navigate", "step": 1, "total": 1})
            await frame_callback(stale_frame)
            await progress_callback({"message": "Page is loading", "step": 1, "total": 1})
            await frame_callback(latest_frame)
            return "Example Domain", 1, "Example Domain", "https://example.com/"

        with patch.object(main, "_generate_plan_sync", return_value=self.plan), patch.object(
            main, "_execute_plan", side_effect=fake_execute
        ):
            response = self.client.post("/execute/stream", json={"task": "Read example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: status", response.text)
        self.assertIn('"message": "Page is loading"', response.text)
        self.assertIn("event: frame", response.text)
        self.assertIn('"browser_task": true', response.text)
        self.assertIn(f"data:image/jpeg;base64,{latest_frame}", response.text)
        self.assertNotIn(f"data:image/jpeg;base64,{stale_frame}", response.text)
        self.assertIn("event: complete", response.text)
        self.assertIn('"output": "Example Domain"', response.text)

    def test_stream_preserves_frame_and_status_order(self):
        frame = base64.b64encode(b"ordered frame").decode("ascii")

        async def fake_execute(plan, frame_callback=None, progress_callback=None):
            await frame_callback(frame)
            await progress_callback({"message": "Status after frame"})
            return "Done", 1, "Example Domain", "https://example.com/"

        with patch.object(main, "_generate_plan_sync", return_value=self.plan), patch.object(
            main, "_execute_plan", side_effect=fake_execute
        ):
            response = self.client.post("/execute/stream", json={"task": "Read example.com"})

        self.assertEqual(response.status_code, 200)
        frame_position = response.text.index(f"data:image/jpeg;base64,{frame}")
        status_position = response.text.index('"message": "Status after frame"')
        self.assertLess(frame_position, status_position)

    def test_stream_exposes_playwright_error_message(self):
        async def fail_execute(plan, frame_callback=None, progress_callback=None):
            raise main.PlaywrightError("BrowserType.launch: Executable doesn't exist")

        with patch.object(main, "_generate_plan_sync", return_value=self.plan), patch.object(
            main, "_execute_plan", side_effect=fail_execute
        ):
            response = self.client.post("/execute/stream", json={"task": "Read example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: error", response.text)
        self.assertIn("Browser automation failed: BrowserType.launch: Executable doesn't exist", response.text)

    def test_chat_stream_returns_text_without_starting_browser(self):
        with patch.object(
            main, "_generate_chat_sync", return_value="Hello! I'm doing well, thanks for asking."
        ) as chat_reply, patch.object(main, "_generate_plan_sync") as planner, patch.object(
            main, "_execute_plan", new=AsyncMock()
        ) as browser_execute:
            response = self.client.post("/execute/stream", json={"task": "Hello, how are you?"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: complete", response.text)
        self.assertIn("Hello! I'm doing well, thanks for asking.", response.text)
        self.assertNotIn("Starting the live browser", response.text)
        self.assertIn("Writing a reply", response.text)
        chat_reply.assert_called_once()
        planner.assert_not_called()
        browser_execute.assert_not_awaited()

    def test_chat_json_route_returns_text_without_starting_browser(self):
        with patch.object(
            main, "_generate_chat_sync", return_value="Hello! I'm doing well, thanks for asking."
        ), patch.object(main, "_generate_plan_sync") as planner, patch.object(
            main, "_execute_plan", new=AsyncMock()
        ) as browser_execute:
            response = self.client.post("/execute", json={"task": "Hello, how are you?"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output"], "Hello! I'm doing well, thanks for asking.")
        self.assertEqual(response.json()["steps_completed"], 0)
        planner.assert_not_called()
        browser_execute.assert_not_awaited()

    def test_operational_gate_only_routes_web_tasks_to_browser_planner(self):
        self.assertFalse(main._requires_browser("Hello, how are you?"))
        self.assertFalse(main._requires_browser("What is the capital of France?"))
        self.assertFalse(main._requires_browser("Hello, who are you?"))
        self.assertTrue(
            main._requires_browser("Go to wikipedia.org and give me the main heading (h1) of the page.")
        )
        self.assertTrue(main._requires_browser("Go to google.com and tell me the title"))
        self.assertTrue(main._requires_browser("Get the page title from https://example.org"))

    def test_persian_browser_intents_are_routed_without_misclassifying_chat(self):
        self.assertTrue(main._requires_browser("برو به example.com و عنوان صفحه را بگو"))
        self.assertTrue(main._requires_browser("در گوگل جستجو کن"))
        self.assertTrue(main._requires_browser("برو به ویکی‌پدیا و عنوان اصلی را بگو"))
        self.assertFalse(main._requires_browser("سلام، حالت چطوره؟"))
        self.assertFalse(main._requires_browser("ویکی‌پدیا چیست؟"))

    def test_persian_chat_stream_skips_browser_planner(self):
        with patch.object(main, "_generate_chat_sync", return_value="خوبم، ممنون!") as chat_reply, patch.object(
            main, "_generate_plan_sync"
        ) as planner, patch.object(main, "_execute_plan", new=AsyncMock()) as browser_execute:
            response = self.client.post("/execute/stream", json={"task": "سلام، حالت چطوره؟"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("خوبم، ممنون!", response.text)
        self.assertIn('"browser_task": false', response.text)
        chat_reply.assert_called_once()
        planner.assert_not_called()
        browser_execute.assert_not_awaited()

    def test_public_destination_validation_rechecks_dns_each_time(self):
        def record(address):
            return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))

        public_answer = [record("8.8.8.8")]
        private_answer = [record("10.0.0.8")]
        with patch("egress_proxy.socket.getaddrinfo", side_effect=[public_answer, private_answer]):
            self.assertTrue(asyncio.run(main._is_public_web_url("https://changing.example")))
            self.assertFalse(asyncio.run(main._is_public_web_url("https://changing.example")))
        self.assertFalse(asyncio.run(main._is_public_web_url("http://127.0.0.1/")))

    def test_page_title_and_h1_extraction_call_the_requested_dom_target(self):
        page = unittest.mock.Mock()
        page.title = AsyncMock(return_value="Artificial Intelligence")
        heading = unittest.mock.Mock()
        heading.evaluate_all = AsyncMock(return_value="Artificial intelligence")
        page.locator.return_value = heading

        title = asyncio.run(main._extract_page_content(page, "page title"))
        h1 = asyncio.run(main._extract_page_content(page, "css:h1"))

        self.assertEqual(title, "Artificial Intelligence")
        self.assertEqual(h1, "Artificial intelligence")
        page.locator.assert_called_with("h1")

    def test_first_paragraph_extraction_reads_only_the_first_paragraph(self):
        page = unittest.mock.Mock()
        first_paragraph = unittest.mock.Mock()
        first_paragraph.inner_text = AsyncMock(return_value="First paragraph only.")
        page.locator.return_value.first = first_paragraph

        result = asyncio.run(main._extract_page_content(page, "first paragraph"))

        self.assertEqual(result, "First paragraph only.")
        page.locator.assert_called_once_with("p")
        first_paragraph.inner_text.assert_awaited_once_with(timeout=10_000)

    def test_title_request_replaces_a_planner_url_extraction(self):
        plan = main.BrowserPlan(
            goal="Open Google and report its title",
            steps=[
                main.BrowserAction(action="navigate", target="https://www.google.com/", value=""),
                main.BrowserAction(action="extract", target="url", value=""),
            ],
        )

        normalized = main._enforce_requested_extraction(
            plan, "Go to google.com and tell me the title"
        )

        self.assertEqual([step.action for step in normalized.steps], ["navigate", "extract"])
        self.assertEqual(normalized.steps[-1].target, "page title")
        self.assertEqual(plan.steps[-1].target, "url")

    def test_explicit_extraction_requests_map_to_stable_targets(self):
        cases = (
            ("Go to Wikipedia and get the main heading (h1)", "css:h1"),
            ("Go to Wikipedia and extract the first paragraph", "first paragraph"),
            ("Go to example.com and get text", "page text"),
            ("برو به example.com و عنوان صفحه را بگو", "page title"),
            ("برو به ویکی‌پدیا و عنوان اصلی را بگو", "css:h1"),
        )
        for task, expected_target in cases:
            with self.subTest(task=task):
                self.assertEqual(main._requested_extraction_target(task), expected_target)

    def test_json_endpoint_enforces_requested_title_extraction(self):
        captured = {}

        async def fake_execute(plan, frame_callback=None, progress_callback=None):
            captured["plan"] = plan
            return "Google", len(plan.steps), "Google", "https://www.google.com/"

        navigate_only = main.BrowserPlan(
            goal="Open Google", steps=[
                main.BrowserAction(action="navigate", target="https://www.google.com/", value="")
            ]
        )
        with patch.object(main, "_generate_plan_sync", return_value=navigate_only), patch.object(
            main, "_execute_plan", side_effect=fake_execute
        ):
            response = self.client.post(
                "/execute", json={"task": "Go to google.com and tell me the title"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output"], "Google")
        self.assertEqual(captured["plan"].steps[-1].target, "page title")

    def test_stream_endpoint_enforces_requested_title_extraction(self):
        captured = {}

        async def fake_execute(plan, frame_callback=None, progress_callback=None):
            captured["plan"] = plan
            return "Google", len(plan.steps), "Google", "https://www.google.com/"

        navigate_only = main.BrowserPlan(
            goal="Open Google", steps=[
                main.BrowserAction(action="navigate", target="https://www.google.com/", value="")
            ]
        )
        with patch.object(main, "_generate_plan_sync", return_value=navigate_only), patch.object(
            main, "_execute_plan", side_effect=fake_execute
        ):
            response = self.client.post(
                "/execute/stream", json={"task": "Go to google.com and tell me the title"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('"output": "Google"', response.text)
        self.assertEqual(captured["plan"].steps[-1].target, "page title")

    def test_browser_plan_rejects_more_than_six_steps(self):
        action = main.BrowserAction(action="navigate", target="https://example.com", value="")
        with self.assertRaises(ValidationError):
            main.BrowserPlan(goal="too many steps", steps=[action] * 7)

    def test_system_prompt_maps_title_text_and_h1_to_extraction_actions(self):
        self.assertIn("target 'page title' (read page.title()", main.SYSTEM_PROMPT)
        self.assertIn("target 'css:h1'", main.SYSTEM_PROMPT)
        self.assertIn("'page text'", main.SYSTEM_PROMPT)
        self.assertIn("do not return a navigation URL as a substitute", main.SYSTEM_PROMPT)

    def test_stream_returns_safe_error_without_key(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post("/execute/stream", json={"task": "Read example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: error", response.text)
        self.assertIn("not configured", response.text)
        self.assertNotIn("Traceback", response.text)

    def test_existing_json_endpoint_still_works(self):
        async def fake_execute(plan, frame_callback=None, progress_callback=None):
            return "Example Domain", 1, "Example Domain", "https://example.com/"

        with patch.object(main, "_generate_plan_sync", return_value=self.plan), patch.object(
            main, "_execute_plan", side_effect=fake_execute
        ):
            response = self.client.post("/execute", json={"task": "Read example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output"], "Example Domain")
        self.assertEqual(response.json()["steps_completed"], 1)

    def test_virtual_display_is_stopped_after_browser_execution(self):
        fake_display = unittest.mock.Mock()
        with patch.object(main, "Display", return_value=fake_display), patch.object(
            main, "_execute_plan_browser", new=AsyncMock(return_value=("", 1, "Title", "https://example.com"))
        ) as browser_run:
            result = asyncio.run(main._execute_plan(self.plan, frame_callback=AsyncMock()))
        self.assertEqual(result[2], "Title")
        fake_display.start.assert_called_once()
        fake_display.stop.assert_called_once()
        self.assertFalse(browser_run.await_args.kwargs["headless"])

    def test_browser_context_uses_connection_filtering_proxy(self):
        page = unittest.mock.Mock()
        page.set_default_timeout = unittest.mock.Mock()
        page.title = AsyncMock(return_value="Example Domain")
        page.url = "https://example.com/"
        context = unittest.mock.Mock()
        context.route = AsyncMock()
        context.new_page = AsyncMock(return_value=page)
        browser = unittest.mock.Mock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        playwright = unittest.mock.Mock()
        playwright.chromium.launch = AsyncMock(return_value=browser)

        class PlaywrightManager:
            async def __aenter__(self):
                return playwright

            async def __aexit__(self, exc_type, exc, traceback):
                return None

        plan = main.BrowserPlan(goal="No-op proxy check", steps=[])
        with patch.object(main, "async_playwright", return_value=PlaywrightManager()):
            result = asyncio.run(main._execute_plan_browser(plan, headless=True))

        self.assertEqual(result[2], "Example Domain")
        proxy = browser.new_context.await_args.kwargs["proxy"]
        self.assertRegex(proxy["server"], r"^http://127\.0\.0\.1:\d+$")

    def test_live_frame_is_captured_after_navigation(self):
        events = []
        page = unittest.mock.Mock()
        page.set_default_timeout = unittest.mock.Mock()

        async def navigate(*args, **kwargs):
            events.append("navigate")

        async def screenshot(**kwargs):
            events.append("screenshot")
            return b"jpeg-frame"

        async def record_frame(encoded):
            events.append("frame")

        page.goto = AsyncMock(side_effect=navigate)
        page.screenshot = AsyncMock(side_effect=screenshot)
        page.title = AsyncMock(return_value="Example Domain")
        page.url = "https://example.com/"
        context = unittest.mock.Mock()
        context.route = AsyncMock()
        context.new_page = AsyncMock(return_value=page)
        browser = unittest.mock.Mock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        playwright = unittest.mock.Mock()
        playwright.chromium.launch = AsyncMock(return_value=browser)

        class PlaywrightManager:
            async def __aenter__(self):
                return playwright

            async def __aexit__(self, exc_type, exc, traceback):
                return None

        plan = main.BrowserPlan(
            goal="Open Example Domain",
            steps=[main.BrowserAction(action="navigate", target="https://example.com/", value="")],
        )
        with patch.object(main, "_is_public_web_url", new=AsyncMock(return_value=True)), patch.object(
            main, "_capture_frames", new=AsyncMock()
        ), patch.object(main, "async_playwright", return_value=PlaywrightManager()):
            asyncio.run(
                main._execute_plan_browser_with_proxy(
                    plan,
                    proxy=unittest.mock.Mock(url="http://127.0.0.1:12345"),
                    headless=True,
                    frame_callback=record_frame,
                )
            )

        self.assertEqual(events, ["navigate", "screenshot", "frame"])

    def test_disconnect_does_not_strand_browser_semaphore(self):
        async def fake_execute(plan, frame_callback=None, progress_callback=None):
            await asyncio.sleep(0.05)
            return "Example Domain", 1, "Example Domain", "https://example.com/"

        async def scenario():
            semaphore = asyncio.Semaphore(1)
            with patch.object(main, "BROWSER_SEMAPHORE", semaphore), patch.object(
                main, "_generate_plan_sync", return_value=self.plan
            ), patch.object(main, "_execute_plan", side_effect=fake_execute):
                events = main._execute_stream_events(main.ExecuteRequest(task="Read example.com"))
                await anext(events)  # planning status
                await anext(events)  # browser launch status; worker is now detached
                await events.aclose()  # simulate the client disconnecting
                await asyncio.sleep(0.1)
                self.assertEqual(semaphore._value, 1)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
