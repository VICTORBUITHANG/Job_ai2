# File: /Users/victorbui/AI/Job_ai2/tools/probe_experience_add.py
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from job_ai2_agent.browser_agent import (
    _click_safe_next,
    _fill_workday_my_information,
    _quiet_wait,
    _upload_required_file_on_current_step,
    _upload_resume_if_possible,
    _wait_and_click_safe_next,
)
from job_ai2_agent.resume_reader import read_resume_profile


async def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("Usage: python tools/probe_experience_add.py JOB_URL RESUME_PATH SECTION")
    url = sys.argv[1]
    resume_path = Path(sys.argv[2]).resolve()
    section = sys.argv[3]
    profile = read_resume_profile(resume_path)
    profile.fields.update(
        {
            "address_line1": "127 Devonshire ST",
            "city": "Ypsilanti",
            "state": "MI",
            "postal_code": "48198",
            "phone": "8622945599",
        }
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        page.set_default_timeout(15_000)
        await page.goto(url, wait_until="domcontentloaded")
        await _quiet_wait(page)
        await _upload_resume_if_possible(page, resume_path)
        await _quiet_wait(page)
        await _wait_and_click_safe_next(page, timeout_ms=90_000)
        await _quiet_wait(page)
        await _fill_workday_my_information(page, profile)
        await _click_safe_next(page)
        await _quiet_wait(page)
        await _upload_required_file_on_current_step(page, resume_path)

        await click_add_for_section(page, section)
        await page.wait_for_timeout(2_000)
        data = await snapshot(page)
        print(json.dumps(data, indent=2))
        await browser.close()


async def click_add_for_section(page, section: str) -> None:
    clicked = await page.evaluate(
        """
        (section) => {
          const elements = Array.from(document.querySelectorAll('h1,h2,h3,h4,div,span,button'));
          const headingIndex = elements.findIndex(el => (el.innerText || '').trim() === section);
          if (headingIndex < 0) return false;
          const stopSections = new Set(['Work Experience', 'Education', 'Languages', 'Skills', 'Resume/CV', 'Websites', 'Social Network URLs']);
          for (let index = headingIndex + 1; index < elements.length; index += 1) {
            const el = elements[index];
            const text = (el.innerText || '').trim();
            if (stopSections.has(text) && text !== section) return false;
            if (el.tagName === 'BUTTON' && text === 'Add') {
              el.scrollIntoView({block: 'center'});
              el.click();
              return true;
            }
          }
          return false;
        }
        """,
        section,
    )
    if not clicked:
        await page.get_by_role("button", name="Add").first.click()


async def snapshot(page) -> dict:
    return await page.evaluate(
        """
        () => ({
          bodyText: document.body.innerText.slice(0, 5000),
          controls: Array.from(document.querySelectorAll('input, textarea, select, a, button, [role="button"], [role="textbox"], [role="combobox"], [role="checkbox"], [contenteditable="true"]'))
            .map((el, i) => {
              const rect = el.getBoundingClientRect();
              return {
                i,
                tag: el.tagName,
                type: el.getAttribute('type'),
                text: (el.innerText || el.textContent || '').trim(),
                aria: el.getAttribute('aria-label'),
                role: el.getAttribute('role'),
                id: el.id,
                name: el.getAttribute('name'),
                className: String(el.className || ''),
                visible: rect.width > 0 && rect.height > 0,
                width: rect.width,
                height: rect.height,
                disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true')
              };
            })
        })
        """
    )


if __name__ == "__main__":
    asyncio.run(main())
