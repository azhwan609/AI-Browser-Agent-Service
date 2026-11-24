import asyncio
import base64
import json
import os
import socket
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from fastapi.responses import Response
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
                self.assertEqual(response.text, "V3.2 - Live Manual Login Handoff")
                self.assertTrue(response.headers["content-type"].startswith("text/plain"))

    def test_root_serves_the_frontend_adapter(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertIn("AI_BROWSER_AGENT_BACKEND_ADAPTER", response.text)

    def test_temporary_login_credentials_fill_only_matching_blank_fields(self):
        plan = main.BrowserPlan(goal="Sign in", steps=[
            main.BrowserAction(action="fill", target="Username", value=""),
            main.BrowserAction(action="fill", target="Password", value=""),
            main.BrowserAction(action="fill", target="Search", value="my query"),
        ])
        updated = main._apply_login_credentials(
            plan, "person@example.com", "secret-value", "Log in to example.com"
        )
        self.assertEqual([step.value for step in updated.steps], ["person@example.com", "secret-value", "my query"])
        self.assertEqual([step.value for step in plan.steps], ["", "", "my query"])
        unchanged = main._apply_login_credentials(
            plan, "person@example.com", "secret-value", "Search example.com"
        )
        self.assertEqual(unchanged, plan)

    def test_login_target_semantics_include_common_names_and_persian(self):
        self.assertEqual(main._login_field_kind("Email address"), "username")
        self.assertEqual(main._login_field_kind("Username"), "username")
        self.assertEqual(main._login_field_kind("Password"), "password")
        self.assertEqual(main._login_field_kind("رمز عبور"), "password")
        self.assertTrue(main._is_login_intent("لطفاً وارد حساب شو"))
        self.assertFalse(main._is_login_intent("صفحه را باز کن"))

    def test_manual_login_plan_discards_any_model_suggested_credentials(self):
        plan = main.BrowserPlan(goal="Sign in", steps=[
            main.BrowserAction(action="fill", target="Username", value="model guessed user"),
            main.BrowserAction(action="fill", target="Password", value="model guessed secret"),
            main.BrowserAction(action="fill", target="Search", value="keep this query"),
        ])
        sanitized = main._clear_planned_login_values(plan)
        self.assertEqual([step.value for step in sanitized.steps], ["", "", "keep this query"])
        self.assertEqual(plan.steps[1].value, "model guessed secret")

    def test_manual_login_submission_is_one_use_and_secrets_never_enter_the_token(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            token = "a" * 43
            future = loop.create_future()
            main.MANUAL_LOGIN_WAITERS[token] = main.ManualLoginWaiter(future, loop.time() + 30)
            request = MagicMock()
            request.headers = {}
            response = Response()
            payload = main.ManualLoginSubmission(
                session_token=token, username="person@example.com", password="temporary secret"
            )
            try:
                accepted = await main.submit_manual_login(payload, request, response)
                self.assertEqual(accepted, {"accepted": True})
                self.assertEqual(future.result(), {
                    "username": "person@example.com", "password": "temporary secret"
                })
                self.assertEqual(response.headers["cache-control"], "no-store")
                with self.assertRaises(Exception) as error:
                    await main.submit_manual_login(payload, request, Response())
                self.assertEqual(error.exception.status_code, 409)
            finally:
                main.MANUAL_LOGIN_WAITERS.pop(token, None)

        asyncio.run(scenario())

    def test_manual_login_submission_rejects_expired_token(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            token = "b" * 43
            future = loop.create_future()
            main.MANUAL_LOGIN_WAITERS[token] = main.ManualLoginWaiter(future, loop.time() - 1)
            request = MagicMock()
            request.headers = {}
            try:
                with self.assertRaises(Exception) as error:
                    await main.submit_manual_login(
                        main.ManualLoginSubmission(session_token=token, username="person", password="secret"),
                        request,
                        Response(),
                    )
                self.assertEqual(error.exception.status_code, 410)
                self.assertTrue(future.cancelled())
                self.assertNotIn(token, main.MANUAL_LOGIN_WAITERS)
            finally:
                main.MANUAL_LOGIN_WAITERS.pop(token, None)

        asyncio.run(scenario())

    def test_manual_login_submission_rejects_cross_origin_request(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            token = "c" * 43
            future = loop.create_future()
            main.MANUAL_LOGIN_WAITERS[token] = main.ManualLoginWaiter(future, loop.time() + 30)
            request = MagicMock()
            request.headers = {"origin": "https://attacker.example", "host": "agent.example"}
            try:
                with self.assertRaises(Exception) as error:
                    await main.submit_manual_login(
                        main.ManualLoginSubmission(session_token=token, username="person", password="secret"),
                        request,
                        Response(),
                    )
                self.assertEqual(error.exception.status_code, 403)
                self.assertFalse(future.done())
            finally:
                main.MANUAL_LOGIN_WAITERS.pop(token, None)

        asyncio.run(scenario())

    def test_manual_login_sse_gate_resumes_the_associated_browser_task(self):
        captured = {}
        planned = main.BrowserPlan(goal="Sign in", steps=[
            main.BrowserAction(action="navigate", target="https://example.com/login", value=""),
            main.BrowserAction(action="fill", target="Username", value="unsafe planner value"),
            main.BrowserAction(action="fill", target="Password", value="unsafe planner secret"),
        ])

        async def fake_execute(plan, frame_callback=None, progress_callback=None, manual_login_callback=None, **kwargs):
            captured["plan"] = plan
            captured["credentials"] = await manual_login_callback()
            return "Signed in", len(plan.steps), "Example", "https://example.com/account"

        async def scenario():
            events = main._execute_stream_events(main.ExecuteRequest(
                task="Open https://example.com/login and log in", manual_login=True
            ))
            with patch.object(main, "_generate_plan_sync", return_value=planned), patch.object(
                main, "_execute_plan", side_effect=fake_execute
            ):
                async for event in events:
                    if event.startswith("event: login_required"):
                        data = json.loads(event.split("data: ", 1)[1])
                        request = MagicMock()
                        request.headers = {}
                        await main.submit_manual_login(
                            main.ManualLoginSubmission(
                                session_token=data["session_token"],
                                username="person@example.com",
                                password="temporary secret",
                            ),
                            request,
                            Response(),
                        )
            self.assertEqual([step.value for step in captured["plan"].steps], ["", "", ""])
            self.assertEqual(captured["credentials"], {
                "username": "person@example.com", "password": "temporary secret"
            })
            self.assertEqual(main.MANUAL_LOGIN_WAITERS, {})

        asyncio.run(scenario())

    def test_manual_login_mode_requires_login_intent_and_rejects_profile_persistence(self):
        for body in (
            {"task": "Open https://example.com", "manual_login": True},
            {"task": "Open https://example.com and log in", "manual_login": True, "profile_id": "p1"},
            {"task": "Open https://example.com and log in", "manual_login": True, "login_password": "secret"},
        ):
            with self.subTest(body=body):
                response = self.client.post("/execute/stream", json=body)
                self.assertEqual(response.status_code, 400)

    def test_manual_login_without_recognized_fill_field_returns_safe_error(self):
        plan = main.BrowserPlan(goal="Navigate only", steps=[
            main.BrowserAction(action="navigate", target="https://example.com/login", value=""),
        ])
        with patch.object(main, "_generate_plan_sync", return_value=plan), patch.object(
            main, "_execute_plan", new=AsyncMock()
        ) as browser_execute:
            response = self.client.post("/execute/stream", json={
                "task": "Open https://example.com/login and log in", "manual_login": True,
            })
        self.assertIn("event: error", response.text)
        self.assertIn("couldn’t identify a standard sign-in field", response.text)
        browser_execute.assert_not_awaited()

    def test_manual_login_disconnect_cleans_gate_and_releases_browser_semaphore(self):
        async def fake_execute(plan, frame_callback=None, progress_callback=None, manual_login_callback=None, **kwargs):
            await manual_login_callback()
            return "Done", 1, "Example", "https://example.com/"

        async def scenario():
            semaphore = asyncio.Semaphore(1)
            with patch.object(main, "BROWSER_SEMAPHORE", semaphore), patch.object(
                main, "_generate_plan_sync", return_value=main.BrowserPlan(
                    goal="Login", steps=[
                        main.BrowserAction(action="navigate", target="https://example.com/login", value=""),
                        main.BrowserAction(action="fill", target="Username", value=""),
                    ]
                )
            ), patch.object(main, "_execute_plan", side_effect=fake_execute):
                events = main._execute_stream_events(main.ExecuteRequest(
                    task="Open https://example.com/login and log in", manual_login=True
                ))
                token = None
                async for event in events:
                    if event.startswith("event: login_required"):
                        token = json.loads(event.split("data: ", 1)[1])["session_token"]
                        await events.aclose()
                        break
                await asyncio.sleep(0.05)
                self.assertIsNotNone(token)
                self.assertNotIn(token, main.MANUAL_LOGIN_WAITERS)
                self.assertEqual(semaphore._value, 1)

        asyncio.run(scenario())

    def test_manual_login_disconnect_before_gate_does_not_create_orphan_waiter(self):
        async def fake_execute(plan, frame_callback=None, progress_callback=None, manual_login_callback=None, **kwargs):
            await asyncio.sleep(0.05)
            await manual_login_callback()
            return "Done", 1, "Example", "https://example.com/"

        async def scenario():
            semaphore = asyncio.Semaphore(1)
            with patch.object(main, "BROWSER_SEMAPHORE", semaphore), patch.object(
                main, "_generate_plan_sync", return_value=main.BrowserPlan(
                    goal="Login", steps=[
                        main.BrowserAction(action="navigate", target="https://example.com/login", value=""),
                        main.BrowserAction(action="fill", target="Username", value=""),
                    ]
                )
            ), patch.object(main, "_execute_plan", side_effect=fake_execute):
                events = main._execute_stream_events(main.ExecuteRequest(
                    task="Open https://example.com/login and log in", manual_login=True
                ))
                await anext(events)
                await anext(events)
                await events.aclose()
                await asyncio.sleep(0.02)
                self.assertEqual(main.MANUAL_LOGIN_WAITERS, {})
                self.assertEqual(semaphore._value, 1)

        asyncio.run(scenario())

    def test_sse_event_format(self):
        event = main._sse_event("status", {"message": "Started"})
        self.assertTrue(event.startswith("event: status\ndata: "))
        self.assertTrue(event.endswith("\n\n"))
        self.assertIn('"message": "Started"', event)

    def test_stream_delivers_frame_status_and_complete(self):
        stale_frame = base64.b64encode(b"stale frame").decode("ascii")
        latest_frame = base64.b64encode(b"jpeg").decode("ascii")

        async def fake_execute(plan, frame_callback=None, progress_callback=None):
            await progress_callback({"message": "Working on your request…"})
            await frame_callback(stale_frame)
            await progress_callback({"message": "The page is ready; looking for your answer…"})
            await frame_callback(latest_frame)
            return "Example Domain", 1, "Example Domain", "https://example.com/"

        with patch.object(main, "_generate_plan_sync", return_value=self.plan), patch.object(
            main, "_execute_plan", side_effect=fake_execute
        ):
            response = self.client.post("/execute/stream", json={"task": "Read example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: status", response.text)
        self.assertIn('"message": "The page is ready; looking for your answer…"', response.text)
        self.assertIn("event: frame", response.text)
        self.assertIn('"browser_task": true', response.text)
        self.assertNotIn("Step 1 of 1", response.text)
        self.assertNotIn('"message": "navigate"', response.text)
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

    def test_stream_sends_task_without_secrets_to_planner_and_applies_secrets_afterward(self):
        captured = {}
        login_plan = main.BrowserPlan(goal="Sign in", steps=[
            main.BrowserAction(action="navigate", target="https://example.com/login", value=""),
            main.BrowserAction(action="fill", target="Username", value=""),
            main.BrowserAction(action="fill", target="Password", value=""),
        ])

        async def fake_execute(plan, frame_callback=None, progress_callback=None):
            captured["plan"] = plan
            return "Signed in", len(plan.steps), "Example", "https://example.com/"

        def fake_plan(task, *_):
            captured["task"] = task
            return login_plan

        with patch.object(main, "_generate_plan_sync", side_effect=fake_plan), patch.object(
            main, "_execute_plan", side_effect=fake_execute
        ):
            response = self.client.post("/execute/stream", json={
                "task": "Open https://example.com/login and log in",
                "login_username": "person@example.com",
                "login_password": "never-send-this-to-the-planner",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["task"], "Open https://example.com/login and log in")
        self.assertNotIn("never-send-this-to-the-planner", captured["task"])
        self.assertEqual([step.value for step in captured["plan"].steps], [
            "", "person@example.com", "never-send-this-to-the-planner"
        ])

    def test_stream_hides_playwright_error_details(self):
        async def fail_execute(plan, frame_callback=None, progress_callback=None):
            raise main.PlaywrightError("BrowserType.launch: Executable doesn't exist")

        with patch.object(main, "_generate_plan_sync", return_value=self.plan), patch.object(
            main, "_execute_plan", side_effect=fail_execute
        ):
            response = self.client.post("/execute/stream", json={"task": "Read example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: error", response.text)
        self.assertIn("I couldn’t complete that browser task", response.text)
        self.assertNotIn("BrowserType.launch", response.text)
        self.assertNotIn("Executable doesn't exist", response.text)

    def test_chat_stream_returns_incremental_text_without_starting_browser(self):
        def emit_chunks(task, api_key, model, on_delta):
            on_delta("Hello! ")
            on_delta("I'm doing well, thanks for asking.")
        with patch.object(main, "_generate_chat_stream_sync", side_effect=emit_chunks) as chat_reply, patch.object(
            main, "_generate_plan_sync"
        ) as planner, patch.object(main, "_execute_plan", new=AsyncMock()) as browser_execute:
            response = self.client.post("/execute/stream", json={"task": "Hello, how are you?"})

        self.assertEqual(response.status_code, 200)
        self.assertIn('event: delta\ndata: {"text": "Hello! "}', response.text)
        self.assertIn("event: delta\ndata: {\"text\": \"I'm doing well, thanks for asking.\"}", response.text)
        self.assertIn("event: complete", response.text)
        self.assertNotIn("Starting the live browser", response.text)
        self.assertIn("Writing a reply", response.text)
        chat_reply.assert_called_once()
        planner.assert_not_called()
        browser_execute.assert_not_awaited()

    def test_groq_completion_falls_back_on_http_429_and_unavailable_model(self):
        expected_models = {429: "llama-3.1-8b-instant", 404: "openai/gpt-oss-120b"}
        for status_code, expected_model in expected_models.items():
            with self.subTest(status_code=status_code):
                client = MagicMock()
                error = RuntimeError("provider rejected primary model")
                error.status_code = status_code
                if status_code == 404:
                    error.body = {"error": {"code": "model_not_found"}}
                expected = object()
                client.chat.completions.create.side_effect = [error, expected]

                result = main._create_groq_completion(client, "llama-3.3-70b-versatile", messages=[])

                self.assertIs(result, expected)
                calls = client.chat.completions.create.call_args_list
                self.assertEqual(calls[0].kwargs["model"], "llama-3.3-70b-versatile")
                self.assertEqual(calls[1].kwargs["model"], expected_model)

    def test_groq_missing_8b_fallback_uses_current_fast_replacement(self):
        client = MagicMock()
        primary_limit = RuntimeError("rate limited")
        primary_limit.status_code = 429
        retired = RuntimeError("model missing")
        retired.status_code = 404
        retired.body = {"error": {"code": "model_not_found"}}
        expected = object()
        client.chat.completions.create.side_effect = [primary_limit, retired, expected]

        result = main._create_groq_completion(client, "llama-3.3-70b-versatile", messages=[])

        self.assertIs(result, expected)
        calls = client.chat.completions.create.call_args_list
        self.assertEqual([call.kwargs["model"] for call in calls], [
            "llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-20b"
        ])

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
        self.assertTrue(main._requires_browser("Log in to github.com"))

    def test_persian_browser_intents_are_routed_without_misclassifying_chat(self):
        self.assertTrue(main._requires_browser("برو به example.com و عنوان صفحه را بگو"))
        self.assertTrue(main._requires_browser("در گوگل جستجو کن"))
        self.assertTrue(main._requires_browser("برو به ویکی‌پدیا و عنوان اصلی را بگو"))
        self.assertFalse(main._requires_browser("سلام، حالت چطوره؟"))
        self.assertFalse(main._requires_browser("ویکی‌پدیا چیست؟"))

    def test_persian_chat_stream_skips_browser_planner(self):
        def emit_reply(task, api_key, model, on_delta):
            on_delta("خوبم، ممنون!")
        with patch.object(main, "_generate_chat_stream_sync", side_effect=emit_reply) as chat_reply, patch.object(
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

    def test_browser_plan_allows_twelve_steps_but_rejects_thirteen(self):
        action = main.BrowserAction(action="navigate", target="https://example.com", value="")
        plan = main.BrowserPlan(goal="bounded workflow", steps=[action] * main.MAX_ACTIONS)
        self.assertEqual(len(plan.steps), 12)
        with self.assertRaises(ValidationError):
            main.BrowserPlan(goal="too many steps", steps=[action] * 13)

    def test_plan_planner_retries_json_mode_after_groq_schema_validation_error(self):
        client = MagicMock()
        completion = MagicMock()
        completion.choices[0].message.content = (
            '{"goal":"Read the page","steps":[{"action":"navigate",'
            '"target":"https://example.com/","value":""}]}'
        )
        client.chat.completions.create.side_effect = [
            RuntimeError("Groq json_validate_failed: Failed to validate JSON"),
            completion,
        ]

        with patch.object(main, "Groq", return_value=client):
            plan = main._generate_plan_sync("Open example.com", "test-key", "test-model")

        self.assertEqual(plan.steps[0].action, "navigate")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        calls = client.chat.completions.create.call_args_list
        self.assertEqual(calls[0].kwargs["response_format"]["type"], "json_schema")
        self.assertEqual(calls[1].kwargs["response_format"], {"type": "json_object"})
        client.close.assert_called_once()

    def test_scoped_click_locator_selects_control_inside_named_product(self):
        item = MagicMock()
        item.count = AsyncMock(return_value=1)
        container = MagicMock()
        item.locator.return_value = container
        exact_button = MagicMock()
        exact_button.count = AsyncMock(return_value=0)
        partial_button = MagicMock()
        partial_button.count = AsyncMock(return_value=1)
        container.get_by_role.side_effect = [exact_button, partial_button]
        page = MagicMock()
        page.get_by_text.return_value.first = item

        locator = asyncio.run(
            main._scoped_click_locator(page, "Add", "Sauce Labs Backpack")
        )

        self.assertIs(locator, partial_button.first)
        self.assertEqual(container.get_by_role.call_count, 2)

    def test_cart_plans_keep_only_requested_unique_products(self):
        backpack = main.BrowserAction(
            action="click", target="Add", value="Sauce Labs Backpack"
        )
        bike_light = main.BrowserAction(
            action="click", target="Add", value="Sauce Labs Bike Light"
        )
        task = "Add only the Sauce Labs Backpack to the cart once."

        normalized = main._normalize_requested_cart_items(
            main.BrowserPlan(goal="add products", steps=[backpack, bike_light, backpack]), task
        )
        self.assertEqual(normalized.steps, [backpack])

    def test_cart_plan_infers_scope_for_single_named_product(self):
        task = "Add only the Sauce Labs Backpack to the cart once."
        generic_add = main.BrowserAction(action="click", target="Add", value="")

        self.assertEqual(
            main._infer_requested_cart_item_title(task), "Sauce Labs Backpack"
        )
        normalized = main._normalize_requested_cart_items(
            main.BrowserPlan(goal="add the requested product", steps=[generic_add]), task
        )
        self.assertEqual(normalized.steps[0].value, "Sauce Labs Backpack")

    def test_cart_item_inference_refuses_multiple_requested_products(self):
        task = "Add Sauce Labs Backpack and Sauce Labs Bike Light to the cart."
        self.assertIsNone(main._infer_requested_cart_item_title(task))

    def test_unscoped_click_locator_rejects_ambiguous_partial_button(self):
        exact_button = MagicMock()
        exact_button.count = AsyncMock(return_value=0)
        exact_link = MagicMock()
        exact_link.count = AsyncMock(return_value=0)
        partial_button = MagicMock()
        partial_button.count = AsyncMock(return_value=6)
        page = MagicMock()
        page.get_by_role.side_effect = [exact_button, exact_link, partial_button]
        exact_text = MagicMock()
        exact_text.count = AsyncMock(return_value=0)
        page.get_by_text.return_value.first = exact_text

        with self.assertRaisesRegex(ValueError, "partial click name matches multiple"):
            asyncio.run(main._unscoped_click_locator(page, "Add"))

    def test_cart_target_prefers_actual_cart_link_before_partial_matching(self):
        page = MagicMock()
        cart_link = MagicMock()
        with patch("main._visible_cart_link", new=AsyncMock(return_value=cart_link)):
            with patch("main._unscoped_click_locator", new=AsyncMock()) as partial_lookup:
                locator = asyncio.run(
                    main._click_locator_for_action(page, "Shopping Cart", "")
                )

        self.assertIs(locator, cart_link)
        partial_lookup.assert_not_awaited()

    def test_cart_plan_without_any_requested_product_fails_safely(self):
        bike_light = main.BrowserAction(
            action="click", target="Add to cart", value="Sauce Labs Bike Light"
        )
        with self.assertRaisesRegex(ValueError, "couldn't safely match"):
            main._normalize_requested_cart_items(
                main.BrowserPlan(goal="add product", steps=[bike_light]),
                "Add only the Sauce Labs Backpack to the cart.",
            )

    def test_system_prompt_maps_title_text_and_h1_to_extraction_actions(self):
        self.assertIn(f"no more than {main.MAX_ACTIONS} steps", main.SYSTEM_PROMPT)
        self.assertIn("every intermediate transition explicitly", main.SYSTEM_PROMPT)
        self.assertIn("visible Shopping Cart link -> click Checkout", main.SYSTEM_PROMPT)
        self.assertIn("add only products the user explicitly named, once each", main.SYSTEM_PROMPT)
        self.assertIn("infer that single named title", main.SYSTEM_PROMPT)
        self.assertIn("click.value", main.SYSTEM_PROMPT)
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

    def test_virtual_display_failure_does_not_fall_back_to_headless(self):
        fake_display = unittest.mock.Mock()
        fake_display.start.side_effect = RuntimeError("Xvfb binary missing")
        with patch.object(main, "Display", return_value=fake_display), patch.object(
            main, "_execute_plan_browser", new=AsyncMock()
        ) as browser_run:
            with self.assertRaisesRegex(RuntimeError, "live browser could not start"):
                asyncio.run(main._execute_plan(self.plan))

        fake_display.start.assert_called_once()
        fake_display.stop.assert_not_called()
        browser_run.assert_not_awaited()

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
            result = asyncio.run(main._execute_plan_browser(plan, headless=False))

        self.assertEqual(result[2], "Example Domain")
        proxy = browser.new_context.await_args.kwargs["proxy"]
        self.assertRegex(proxy["server"], r"^http://127\.0\.0\.1:\d+$")
        playwright.chromium.launch.assert_awaited_once_with(headless=False)

    def test_executor_pauses_only_after_validating_login_fields_and_fills_after_resume(self):
        username_field = {"tag": "input", "type": "text", "disabled": False, "readOnly": False, "autocomplete": "username"}
        password_field = {"tag": "input", "type": "password", "disabled": False, "readOnly": False, "autocomplete": "current-password"}
        username = MagicMock()
        username.evaluate = AsyncMock(side_effect=[username_field, username_field, username_field])
        username.fill = AsyncMock()
        password = MagicMock()
        password.evaluate = AsyncMock(side_effect=[password_field, password_field, password_field, password_field])
        password.fill = AsyncMock()
        page = MagicMock()
        page.set_default_timeout = MagicMock()
        page.goto = AsyncMock()
        page.title = AsyncMock(return_value="Sign in")
        page.url = "https://example.com/login"
        context = MagicMock()
        context.route = AsyncMock()
        context.new_page = AsyncMock(return_value=page)
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)

        class PlaywrightManager:
            async def __aenter__(self):
                return playwright

            async def __aexit__(self, exc_type, exc, traceback):
                return None

        plan = main.BrowserPlan(goal="Sign in", steps=[
            main.BrowserAction(action="navigate", target="https://example.com/login", value=""),
            main.BrowserAction(action="fill", target="Username", value=""),
            main.BrowserAction(action="fill", target="Password", value=""),
        ])

        async def scenario():
            calls = []
            credential_future = asyncio.get_running_loop().create_future()

            async def wait_for_credentials():
                calls.append("prompt")
                return await credential_future

            with patch.object(main, "_is_public_web_url", new=AsyncMock(return_value=True)), patch.object(
                main, "_capture_frames", new=AsyncMock()
            ), patch.object(main, "_fill_field_locator", new=AsyncMock(side_effect=[username, password, username, password, password])), patch.object(
                main, "async_playwright", return_value=PlaywrightManager()
            ):
                task = asyncio.create_task(main._execute_plan_browser_with_proxy(
                    plan,
                    proxy=MagicMock(url="http://127.0.0.1:12345"),
                    headless=False,
                    manual_login_callback=wait_for_credentials,
                ))
                await asyncio.sleep(0)
                self.assertEqual(calls, ["prompt"])
                username.fill.assert_not_awaited()
                password.fill.assert_not_awaited()
                credential_future.set_result({"username": "person@example.com", "password": "temporary secret"})
                result = await task
            self.assertEqual(result[2], "Sign in")
            username.fill.assert_awaited_once_with("person@example.com", timeout=10_000)
            password.fill.assert_awaited_once_with("temporary secret", timeout=10_000)

        asyncio.run(scenario())

    def test_executor_rejects_non_password_control_without_prompting(self):
        username_field = {"tag": "input", "type": "text", "disabled": False, "readOnly": False, "autocomplete": "username"}
        invalid_password_field = {"tag": "input", "type": "search", "disabled": False, "readOnly": False, "autocomplete": ""}
        username = MagicMock()
        username.evaluate = AsyncMock(side_effect=[username_field, username_field])
        password = MagicMock()
        password.evaluate = AsyncMock(return_value=invalid_password_field)
        page = MagicMock()
        page.set_default_timeout = MagicMock()
        page.goto = AsyncMock()
        page.title = AsyncMock(return_value="Login")
        page.url = "https://example.com/login"
        context = MagicMock()
        context.route = AsyncMock()
        context.new_page = AsyncMock(return_value=page)
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)

        class PlaywrightManager:
            async def __aenter__(self):
                return playwright

            async def __aexit__(self, exc_type, exc, traceback):
                return None

        plan = main.BrowserPlan(goal="Sign in", steps=[
            main.BrowserAction(action="navigate", target="https://example.com/login", value=""),
            main.BrowserAction(action="fill", target="Username", value=""),
            main.BrowserAction(action="fill", target="Password", value=""),
        ])
        prompt = AsyncMock(return_value={"username": "user", "password": "secret"})

        async def scenario():
            with patch.object(main, "_is_public_web_url", new=AsyncMock(return_value=True)), patch.object(
                main, "_capture_frames", new=AsyncMock()
            ), patch.object(main, "_fill_field_locator", new=AsyncMock(side_effect=[username, password])), patch.object(
                main, "async_playwright", return_value=PlaywrightManager()
            ):
                with self.assertRaisesRegex(ValueError, "only accepts standard username and password fields"):
                    await main._execute_plan_browser_with_proxy(
                        plan, proxy=MagicMock(url="http://127.0.0.1:12345"),
                        headless=False, manual_login_callback=prompt,
                    )
            prompt.assert_not_awaited()

        asyncio.run(scenario())

    def test_live_frame_is_captured_after_navigation(self):
        events = []
        statuses = []
        page = unittest.mock.Mock()
        page.set_default_timeout = unittest.mock.Mock()

        async def navigate(*args, **kwargs):
            events.append("navigate")

        async def screenshot(**kwargs):
            events.append("screenshot")
            return b"jpeg-frame"

        async def record_frame(encoded):
            events.append("frame")

        async def record_status(status):
            statuses.append(status)

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
                    headless=False,
                    frame_callback=record_frame,
                    progress_callback=record_status,
                )
            )

        self.assertEqual(events, ["navigate", "screenshot", "frame"])
        self.assertGreaterEqual(len(statuses), 2)
        self.assertTrue(all("step" not in status for status in statuses))
        self.assertTrue(all("navigate" not in status["message"].lower() for status in statuses))

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
