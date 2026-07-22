# tests/test_job_url_history.py

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from job_ai2_agent import web_app


class JobUrlHistoryTests(unittest.TestCase):
    def test_keeps_30_unique_urls_with_most_recent_first(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_settings = SimpleNamespace(account_dir=Path(directory))
            with patch.object(web_app, "settings", fake_settings):
                for index in range(32):
                    web_app._remember_job_url(f"https://example.com/jobs/{index}")
                web_app._remember_job_url("https://example.com/jobs/10")

                history = web_app._recent_job_urls()

            self.assertEqual(30, len(history))
            self.assertEqual("https://example.com/jobs/10", history[0])
            self.assertEqual(1, history.count("https://example.com/jobs/10"))
            self.assertNotIn("https://example.com/jobs/0", history)
            self.assertNotIn("https://example.com/jobs/1", history)

    def test_ignores_invalid_history_file(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_settings = SimpleNamespace(account_dir=Path(directory))
            history_path = Path(directory) / "job_url_history.json"
            history_path.write_text("not json", encoding="utf-8")
            with patch.object(web_app, "settings", fake_settings):
                self.assertEqual([], web_app._recent_job_urls())

    def test_imports_existing_drafts_newest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_settings = SimpleNamespace(account_dir=Path(directory))
            draft_dir = Path(directory) / "drafts"
            draft_dir.mkdir()
            older = {"job_url": "https://example.com/jobs/older", "saved_at": "2026-01-01T00:00:00Z"}
            newer = {"job_url": "https://example.com/jobs/newer", "saved_at": "2026-02-01T00:00:00Z"}
            (draft_dir / "older.json").write_text(json.dumps(older), encoding="utf-8")
            (draft_dir / "newer.json").write_text(json.dumps(newer), encoding="utf-8")

            with patch.object(web_app, "settings", fake_settings):
                self.assertEqual(
                    ["https://example.com/jobs/newer", "https://example.com/jobs/older"],
                    web_app._recent_job_urls(),
                )


if __name__ == "__main__":
    unittest.main()
