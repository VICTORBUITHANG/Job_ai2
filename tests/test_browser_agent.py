# tests/test_browser_agent.py

import unittest
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from job_ai2_agent.browser_agent import _human_verification_required, _is_rippling_job_detail_url, _is_rippling_url, _open_rippling_application


class RipplingNavigationTests(unittest.IsolatedAsyncioTestCase):
    def test_recognizes_rippling_job_detail_url(self):
        self.assertTrue(
            _is_rippling_job_detail_url(
                "https://ats.rippling.com/general/jobs/dfb385b0-bdf0-45cc-acd7-c350c52eba4c?src=lnki"
            )
        )
        self.assertFalse(
            _is_rippling_job_detail_url(
                "https://ats.rippling.com/general/jobs/dfb385b0-bdf0-45cc-acd7-c350c52eba4c/apply?src=lnki"
            )
        )
        self.assertFalse(_is_rippling_job_detail_url("https://example.com/general/jobs/123"))
        self.assertTrue(_is_rippling_url("https://ats.rippling.com/general/jobs/123/apply"))

    async def test_clicks_visible_apply_now_button(self):
        apply_button = Mock()
        apply_button.is_visible = AsyncMock(return_value=True)
        apply_button.is_enabled = AsyncMock(return_value=True)
        apply_button.evaluate = AsyncMock(return_value=False)
        locator = Mock()
        locator.count = AsyncMock(return_value=1)
        locator.nth.return_value = apply_button
        page = Mock()
        type(page).url = PropertyMock(side_effect=[
            "https://ats.rippling.com/general/jobs/job-id?src=lnki",
            "https://ats.rippling.com/general/jobs/job-id/apply?src=lnki",
        ])
        page.locator.return_value = locator
        page.wait_for_url = AsyncMock()

        with (
            patch("job_ai2_agent.browser_agent._click_locator_with_mouse", new=AsyncMock()) as click,
            patch("job_ai2_agent.browser_agent._quiet_wait", new=AsyncMock()) as quiet_wait,
        ):
            opened = await _open_rippling_application(page)

        self.assertTrue(opened)
        click.assert_awaited_once_with(page, apply_button)
        quiet_wait.assert_awaited_once_with(page)

    async def test_detects_human_verification_text(self):
        page = Mock()
        with patch(
            "job_ai2_agent.browser_agent._body_text",
            new=AsyncMock(return_value="Verify you are human"),
        ):
            self.assertTrue(await _human_verification_required(page))


if __name__ == "__main__":
    unittest.main()
