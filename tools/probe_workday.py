# File: /Users/victorbui/AI/Job_ai2/tools/probe_workday.py
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from time import monotonic

from playwright.async_api import async_playwright


async def main() -> None:
    if len(sys.argv) not in {2, 3, 4, 5}:
        raise SystemExit("Usage: python tools/probe_workday.py JOB_URL [RESUME_PATH] [NEXT_CLICKS] [ACTION]")
    url = sys.argv[1]
    resume_path = Path(sys.argv[2]).resolve() if len(sys.argv) == 3 else None
    if len(sys.argv) == 4:
        resume_path = Path(sys.argv[2]).resolve()
    next_clicks = int(sys.argv[3]) if len(sys.argv) == 4 else 0
    if len(sys.argv) == 5:
        next_clicks = int(sys.argv[3])
    action = sys.argv[4] if len(sys.argv) == 5 else ""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        page.set_default_timeout(15_000)
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            await page.wait_for_timeout(3_000)
        if resume_path is not None:
            await page.wait_for_selector(
                "input[type='file'], button:has-text('Select file')",
                timeout=45_000,
            )
            await page.locator("input[type='file']").first.set_input_files(str(resume_path))
            await wait_for_next_enabled(page, timeout_ms=90_000)
        for _ in range(next_clicks):
            await wait_for_next_enabled(page, timeout_ms=90_000)
            await page.get_by_role("button", name="Next").click()
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                await page.wait_for_timeout(3_000)
            await wait_for_workday_step_ready(page, timeout_ms=90_000)
        if action == "open-country":
            await page.locator("button#country--country").click()
            await page.wait_for_timeout(2_000)
        if resume_path is not None:
            await page.screenshot(
                path=str(
                    resume_path.parent.parent
                    / "screenshots"
                    / f"probe_after_{next_clicks}_next.png"
                ),
                full_page=True,
            )
        data = await page.evaluate(
            """
            () => ({
              bodyText: document.body.innerText.slice(0, 3000),
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
        print(json.dumps(data, indent=2))
        await browser.close()

async def wait_for_next_enabled(page, timeout_ms: int) -> bool:
    deadline = monotonic() + (timeout_ms / 1000)
    while monotonic() < deadline:
        button = page.get_by_role("button", name="Next").first
        try:
            if await button.is_visible(timeout=500):
                disabled = await button.evaluate(
                    "(el) => Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true')"
                )
                if not disabled:
                    return True
        except Exception:
            pass
        await page.wait_for_timeout(1_000)
    return False


async def wait_for_workday_step_ready(page, timeout_ms: int) -> None:
    deadline = monotonic() + (timeout_ms / 1000)
    while monotonic() < deadline:
        try:
            ready = await page.evaluate(
                """
                () => {
                  const text = document.body.innerText || '';
                  const hasLoadingOnly = /\\nLoading\\n/.test(`\\n${text}\\n`);
                  const editable = Array.from(document.querySelectorAll(
                    'input:not([type="hidden"]):not([type="file"]), textarea, select, [role="textbox"], [role="combobox"], [role="checkbox"], [role="radio"], [contenteditable="true"]'
                  )).some(el => {
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0 && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                  });
                  return !hasLoadingOnly || editable;
                }
                """
            )
            if ready:
                return
        except Exception:
            return
        await page.wait_for_timeout(1_000)


if __name__ == "__main__":
    asyncio.run(main())
