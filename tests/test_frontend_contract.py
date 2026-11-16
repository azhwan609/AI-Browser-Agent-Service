import unittest
from pathlib import Path


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.html = (root / "manus-obtima.html").read_text(encoding="utf-8")
        cls.backend = (root / "main.py").read_text(encoding="utf-8")

    def test_sse_endpoint_is_same_origin_and_not_hardcoded(self):
        self.assertIn("new URL('/execute/stream',window.location.href)", self.html)
        self.assertNotIn("localhost:8000", self.html)
        self.assertNotIn("127.0.0.1:8000", self.html)

    def test_live_panel_only_opens_for_browser_tasks(self):
        self.assertIn("if(data.browser_task)showPanel();else panel.hidden=true;", self.html)
        self.assertIn('"browser_task": browser_task', self.backend)

    def test_incomplete_sse_stream_is_reported_as_an_error(self):
        self.assertIn("if(!terminalEventReceived)throw new Error", self.html)
        self.assertIn("terminalEventReceived=true", self.html)

    def test_unsupported_attachments_are_rejected_without_clearing_state(self):
        self.assertIn("supportsAttachments:false", self.html)
        self.assertIn("payload.files.length&&adapter&&adapter.supportsAttachments===false", self.html)


if __name__ == "__main__":
    unittest.main()
