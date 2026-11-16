import asyncio
import base64
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

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

    def test_sse_event_format(self):
        event = main._sse_event("status", {"message": "Started"})
        self.assertTrue(event.startswith("event: status\ndata: "))
        self.assertTrue(event.endswith("\n\n"))
        self.assertIn('"message": "Started"', event)

    def test_stream_delivers_frame_status_and_complete(self):
        async def fake_execute(plan, frame_callback=None, progress_callback=None):
            await progress_callback({"message": "Step 1 of 1: navigate", "step": 1, "total": 1})
            await frame_callback(base64.b64encode(b"jpeg").decode("ascii"))
            return "Example Domain", 1, "Example Domain", "https://example.com/"

        with patch.object(main, "_generate_plan_sync", return_value=self.plan), patch.object(
            main, "_execute_plan", side_effect=fake_execute
        ):
            response = self.client.post("/execute/stream", json={"task": "Read example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: status", response.text)
        self.assertIn("event: frame", response.text)
        self.assertIn("data:image/jpeg;base64,anBlZw==", response.text)
        self.assertIn("event: complete", response.text)
        self.assertIn('"output": "Example Domain"', response.text)

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
        chat_plan = main.BrowserPlan(
            intent="chat", goal="", reply="Hello! I'm doing well, thanks for asking.", steps=[]
        )
        with patch.object(main, "_generate_plan_sync", return_value=chat_plan), patch.object(
            main, "_execute_plan", new=AsyncMock()
        ) as browser_execute:
            response = self.client.post("/execute/stream", json={"task": "Hello, how are you?"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: complete", response.text)
        self.assertIn("Hello! I'm doing well, thanks for asking.", response.text)
        self.assertNotIn("Starting the live browser", response.text)
        browser_execute.assert_not_awaited()

    def test_chat_json_route_returns_text_without_starting_browser(self):
        chat_plan = main.BrowserPlan(
            intent="chat", goal="", reply="Hello! I'm doing well, thanks for asking.", steps=[]
        )
        with patch.object(main, "_generate_plan_sync", return_value=chat_plan), patch.object(
            main, "_execute_plan", new=AsyncMock()
        ) as browser_execute:
            response = self.client.post("/execute", json={"task": "Hello, how are you?"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output"], "Hello! I'm doing well, thanks for asking.")
        self.assertEqual(response.json()["steps_completed"], 0)
        browser_execute.assert_not_awaited()

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
