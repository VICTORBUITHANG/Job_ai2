# File: /Users/victorbui/AI/Job_ai2/job_ai2_agent/browser_agent.py
from __future__ import annotations

import re
from asyncio import sleep
from datetime import date
from pathlib import Path
from time import monotonic

from job_ai2_agent.llm_mapper import FieldMapper
from job_ai2_agent.models import AgentRunResult, ApplicationField, EducationItem, FillDecision, ResumeProfile, WorkExperience


async def fill_job_application(
    job_url: str,
    resume_path: Path,
    profile: ResumeProfile,
    mapper: FieldMapper,
    headless: bool,
    hold_seconds: int,
    screenshot_path: Path,
) -> tuple[AgentRunResult, list[FillDecision]]:
    from playwright.async_api import async_playwright

    filled_count = 0
    skipped_count = 0
    decisions: list[FillDecision] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        page = await browser.new_page()
        page.set_default_timeout(10_000)
        await page.goto(job_url, wait_until="domcontentloaded")
        await _quiet_wait(page)

        unavailable_message = await _workday_unavailable_message(page)
        if unavailable_message:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(screenshot_path), full_page=True)
            await browser.close()
            result = AgentRunResult(
                status="failed",
                job_url=job_url,
                filled_count=0,
                skipped_count=0,
                review_path="",
                screenshot_path=str(screenshot_path),
                message=unavailable_message,
            )
            return result, decisions

        uploaded = await _upload_resume_if_possible(page, resume_path)
        if uploaded:
            await _quiet_wait(page)
            await _wait_and_click_safe_next(page, timeout_ms=90_000)
            await _quiet_wait(page)

        for _step in range(6):
            step_text = await _body_text(page)
            special_decisions = await _fill_workday_known_fields(page, profile, resume_path)
            decisions.extend(special_decisions)
            special_filled = sum(1 for decision in special_decisions if decision.action != "skip")
            filled_count += special_filled
            current_step_name = _current_workday_step_name(step_text)
            if current_step_name == "Review":
                break
            if current_step_name in {"My Information", "My Experience"}:
                if not await _click_safe_next(page):
                    break
                await _quiet_wait(page)
                continue

            fields = [
                field
                for field in await _collect_fields(page)
                if field.required or _looks_required_label(field.label)
            ]
            if not fields:
                if not await _wait_and_click_safe_next(page, timeout_ms=90_000):
                    break
                await _quiet_wait(page)
                continue

            step_decisions = mapper.decisions(fields, profile)
            decisions.extend(step_decisions)
            step_filled = 0

            for decision in step_decisions:
                if not decision.selector or decision.action == "skip" or not decision.value:
                    skipped_count += 1
                    continue
                try:
                    await _apply_decision(page, decision)
                    filled_count += 1
                    step_filled += 1
                except Exception:
                    skipped_count += 1

            if not await _click_safe_next(page):
                break
            await _quiet_wait(page)
            if step_filled + special_filled == 0:
                break

        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(screenshot_path), full_page=True)

        if hold_seconds > 0:
            try:
                await page.wait_for_timeout(hold_seconds * 1000)
            except Exception:
                pass
        await browser.close()

    result = AgentRunResult(
        status="completed",
        job_url=job_url,
        filled_count=filled_count,
        skipped_count=skipped_count,
        review_path="",
        screenshot_path=str(screenshot_path),
        message="Filled available fields from the uploaded resume. Submit was not clicked.",
    )
    return result, decisions


async def _fill_workday_known_fields(
    page,
    profile: ResumeProfile,
    resume_path: Path,
) -> list[FillDecision]:
    decisions: list[FillDecision] = []
    text = await _body_text(page)
    current_step = _current_workday_step_name(text)
    if current_step == "My Information":
        decisions.extend(await _fill_workday_my_information(page, profile))
    elif current_step == "My Experience":
        decisions.extend(await _fill_workday_my_experience(page, profile, resume_path))
    elif current_step == "Application Questions":
        decisions.extend(await _fill_workday_application_questions(page, profile))
    elif current_step in {"Voluntary Disclosures", "Self Identify"}:
        decisions.extend(await _fill_workday_demographic_step(page, profile))
    return decisions


def _current_workday_step_name(text: str) -> str:
    match = re.search(
        r"current step\s+\d+\s+of\s+\d+\s*\n([^\n]+)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    headings = [
        "Autofill with Resume",
        "My Information",
        "My Experience",
        "Application Questions",
        "Voluntary Disclosures",
        "Self Identify",
        "Review",
    ]
    for heading in headings:
        if re.search(rf"\n{re.escape(heading)}\n", f"\n{text}\n"):
            return heading
    return ""


async def _fill_workday_my_information(
    page,
    profile: ResumeProfile,
) -> list[FillDecision]:
    fields = profile.fields
    decisions: list[FillDecision] = []
    decisions.append(
        await _choose_radio_near_text(
            page,
            "Have you previously been employed",
            "No",
            "previous employment",
        )
    )
    decisions.append(
        await _choose_dropdown_by_button_id(
            page,
            "country--country",
            fields.get("country", "United States of America"),
            "Country",
        )
    )
    field_map = {
        "#name--legalName--firstName": ("First Name", fields.get("first_name", "")),
        "#name--legalName--lastName": ("Last Name", fields.get("last_name", "")),
        "#address--addressLine1": ("Address Line 1", fields.get("address_line1", "")),
        "#address--addressLine2": ("Address Line 2", fields.get("address_line2", "")),
        "#address--city": ("City", fields.get("city", "")),
        "#address--postalCode": ("Postal Code", fields.get("postal_code", "")),
        "#emailAddress--emailAddress": ("Email", fields.get("email", "")),
        "#phoneNumber--phoneNumber": ("Phone Number", fields.get("phone", "")),
    }
    for selector, (label, value) in field_map.items():
        decisions.append(await _fill_if_present(page, selector, label, value))
    if fields.get("preferred_name"):
        decisions.append(await _check_if_present(page, "#name--preferredCheck", "I have a preferred name"))
        await page.wait_for_timeout(500)
        decisions.extend(await _fill_preferred_name_fields(page, fields))
    decisions.append(
        await _choose_dropdown_by_button_id(
            page,
            "address--countryRegion",
            _state_name(fields.get("state", "")),
            "State",
        )
    )
    decisions.append(
        await _choose_dropdown_by_button_id(
            page,
            "phoneNumber--phoneType",
            "Mobile",
            "Phone Device Type",
        )
    )
    return decisions


async def _fill_workday_my_experience(
    page,
    profile: ResumeProfile,
    resume_path: Path,
) -> list[FillDecision]:
    decisions: list[FillDecision] = []
    decisions.extend(await _fill_workday_work_experience(page, profile))
    decisions.extend(await _fill_workday_education(page, profile))
    decisions.append(await _upload_required_file_on_current_step(page, resume_path))
    return decisions


async def _fill_workday_work_experience(
    page,
    profile: ResumeProfile,
) -> list[FillDecision]:
    fields = profile.fields
    decisions: list[FillDecision] = []
    experiences = profile.work_experiences or [
        WorkExperience(
            title=fields.get("current_job_title", ""),
            company=fields.get("current_company", ""),
            location=fields.get("current_job_location", ""),
            start_month=fields.get("current_job_start_month", ""),
            start_year=fields.get("current_job_start_year", ""),
            currently_work_here=True,
        )
    ]
    experiences = [experience for experience in experiences if experience.title or experience.company]
    if not experiences:
        return decisions
    decisions.extend(await _ensure_section_entry_count(page, "Work Experience", "[id^='workExperience-'][id$='--jobTitle']", len(experiences)))
    for index, experience in enumerate(experiences):
        decisions.extend(await _fill_work_experience_entry(page, index, experience, fields))
    return decisions


async def _fill_work_experience_entry(
    page,
    index: int,
    experience: WorkExperience,
    fields: dict[str, str],
) -> list[FillDecision]:
    label_prefix = f"Work Experience {index + 1}"
    entry_prefix = await _work_experience_entry_prefix(page, index)
    selector = _work_experience_selector(entry_prefix)
    decisions = [
        await _force_fill_nth_if_present(
            page,
            selector("--jobTitle", "[id^='workExperience-'][id$='--jobTitle']"),
            index,
            f"{label_prefix} Job Title",
            experience.title,
        ),
        await _force_fill_nth_if_present(
            page,
            selector("--companyName", "[id^='workExperience-'][id$='--companyName']"),
            index,
            f"{label_prefix} Company",
            experience.company,
        ),
    ]
    if experience.currently_work_here:
        decisions.append(
            await _check_nth_if_present(
                page,
                selector("--currentlyWorkHere", "[id^='workExperience-'][id$='--currentlyWorkHere']"),
                index,
                f"{label_prefix} Currently Work Here",
            )
        )
    else:
        decisions.append(
            await _uncheck_nth_if_present(
                page,
                selector("--currentlyWorkHere", "[id^='workExperience-'][id$='--currentlyWorkHere']"),
                index,
                f"{label_prefix} Currently Work Here",
            )
        )
    decisions.extend(
        [
            await _force_fill_date_part_nth_if_present(
                page,
                selector("--startDate-dateSectionMonth-input", "[id^='workExperience-'][id*='--startDate'][id$='Month-input']"),
                index,
                f"{label_prefix} Start Month",
                experience.start_month or "01",
            ),
            await _force_fill_date_part_nth_if_present(
                page,
                selector("--startDate-dateSectionYear-input", "[id^='workExperience-'][id*='--startDate'][id$='Year-input']"),
                index,
                f"{label_prefix} Start Year",
                experience.start_year,
            ),
            await _force_fill_date_part_nth_if_present(
                page,
                selector("--endDate-dateSectionMonth-input", "[id^='workExperience-'][id*='--endDate'][id$='Month-input']"),
                index,
                f"{label_prefix} End Month",
                "" if experience.currently_work_here else (experience.end_month or "01"),
            ),
            await _force_fill_date_part_nth_if_present(
                page,
                selector("--endDate-dateSectionYear-input", "[id^='workExperience-'][id*='--endDate'][id$='Year-input']"),
                index,
                f"{label_prefix} End Year",
                "" if experience.currently_work_here else experience.end_year,
            ),
        ]
    )
    return decisions


async def _work_experience_entry_prefix(page, index: int) -> str:
    locator = page.locator(_visible_selector("[id^='workExperience-'][id$='--jobTitle']")).nth(index)
    try:
        element_id = await locator.get_attribute("id", timeout=1_000)
    except Exception:
        return ""
    if not element_id or "--" not in element_id:
        return ""
    return element_id.split("--", 1)[0]


def _work_experience_selector(entry_prefix: str):
    def _selector(suffix: str, fallback: str) -> str:
        return f"#{entry_prefix}{suffix}" if entry_prefix else fallback

    return _selector


async def _delete_extra_work_experience_entries(page, keep_count: int = 1) -> list[FillDecision]:
    decisions: list[FillDecision] = []
    for _ in range(10):
        selector = await _mark_last_extra_delete_button_between_sections(page, "Work Experience", "Education", keep_count)
        if not selector:
            break
        try:
            await _click_locator_with_mouse(page, page.locator(selector).first)
            await page.wait_for_timeout(600)
            await _confirm_delete_if_prompted(page)
            decisions.append(
                FillDecision(
                    selector,
                    "Extra Work Experience",
                    "click",
                    "Delete",
                    0.9,
                    "Deleted extra Workday work experience block to avoid incomplete required fields.",
                )
            )
        except Exception as exc:
            decisions.append(
                FillDecision(
                    selector,
                    "Extra Work Experience",
                    "skip",
                    "Delete",
                    0.0,
                    f"Could not delete extra work experience block: {exc}",
                )
            )
            break
    return decisions


async def _mark_last_extra_delete_button_between_sections(page, start_section: str, end_section: str, keep_count: int = 1) -> str:
    marker = "data-job-ai2-delete-extra-work"
    found = await page.evaluate(
        """
        ([startSection, endSection, marker, keepCount]) => {
          document.querySelectorAll(`[${marker}]`).forEach(el => el.removeAttribute(marker));
          const visible = (el) => {
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
          };
          const textOf = (el) => (el.innerText || el.textContent || '').trim();
          const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,div,span,button')).filter(visible);
          const startIndex = nodes.findIndex(el => textOf(el) === startSection);
          if (startIndex < 0) return false;
          let endIndex = nodes.findIndex((el, index) => index > startIndex && textOf(el) === endSection);
          if (endIndex < 0) endIndex = nodes.length;
          const deletes = nodes
            .slice(startIndex + 1, endIndex)
            .filter(el => el.tagName === 'BUTTON' && textOf(el).toLowerCase() === 'delete');
          if (deletes.length <= keepCount) return false;
          deletes[deletes.length - 1].setAttribute(marker, 'true');
          return true;
        }
        """,
        [start_section, end_section, marker, keep_count],
    )
    return f"[{marker}='true']" if found else ""


async def _confirm_delete_if_prompted(page) -> None:
    dialog = page.locator("[role='dialog'], [aria-modal='true']").last
    try:
        if not await dialog.is_visible(timeout=1_000):
            return
    except Exception:
        return
    for label in ["Delete", "OK", "Yes"]:
        try:
            locator = dialog.get_by_role("button", name=label, exact=True).last
            if await locator.is_visible(timeout=500) and not await _is_disabled(locator):
                await _click_locator_with_mouse(page, locator)
                await page.wait_for_timeout(600)
                return
        except Exception:
            continue


async def _fill_workday_education(
    page,
    profile: ResumeProfile,
) -> list[FillDecision]:
    fields = profile.fields
    decisions: list[FillDecision] = []
    educations = profile.education_items or [
        EducationItem(
            school=fields.get("education_school", ""),
            degree=fields.get("education_degree", ""),
            field_of_study=fields.get("education_field", ""),
            end_year=fields.get("education_end_year", ""),
        )
    ]
    educations = [
        education
        for education in educations
        if (education.school or education.degree) and education.degree.lower() != "certificate"
    ]
    if not educations:
        return decisions
    decisions.extend(await _ensure_section_entry_count(page, "Education", "[id^='education-'][id$='--schoolName']", len(educations)))
    for index, education in enumerate(educations):
        label_prefix = f"Education {index + 1}"
        decisions.append(
            await _fill_token_input_nth(
                page,
                "[id^='education-'][id$='--schoolName']",
                index,
                f"{label_prefix} School",
                education.school,
            )
        )
        decisions.append(
            await _choose_dropdown_nth_by_selector_options(
                page,
                "[id^='education-'][id$='--degree']",
                index,
                _degree_options(education.degree),
                f"{label_prefix} Degree",
            )
        )
    return decisions


async def _fill_workday_languages(page) -> list[FillDecision]:
    decisions: list[FillDecision] = []
    if await _is_visible_selector(page, "[id^='language-'][id$='--language']"):
        return decisions
    added = await _click_add_for_section(page, "Languages")
    decisions.append(added)
    if added.action == "skip":
        return decisions
    await page.wait_for_timeout(800)
    decisions.append(
        await _choose_dropdown_by_selector_options(
            page,
            "[id^='language-'][id$='--language']",
            ["English"],
            "Language",
        )
    )
    decisions.append(await _check_if_present(page, "[id^='language-'][id$='--native']", "Fluent language"))
    for label in ["Comprehension", "Overall", "Reading", "Speaking", "Writing"]:
        decisions.append(
            await _choose_dropdown_by_aria_options(
                page,
                label,
                ["Fluent", "Native", "Advanced", "Expert"],
                f"Language {label}",
            )
        )
    return decisions


async def _fill_workday_application_questions(
    page,
    profile: ResumeProfile,
) -> list[FillDecision]:
    decisions: list[FillDecision] = []
    fields = profile.fields
    text = await _body_text(page)
    if "desired start date" in text.lower():
        decisions.append(
            await _fill_date_near_text(
                page,
                "desired start date",
                "Desired Start Date",
                fields.get("desired_start_date", "") or _default_start_date(),
            )
        )
    work_authorization = fields.get("work_authorization", "Yes") or "Yes"
    if "legally permitted" in text.lower():
        decisions.append(
            await _choose_dropdown_near_text(
                page,
                "legally permitted to work",
                [work_authorization],
                "Legal Work Permission",
            )
        )
    elif "authorized" in text.lower() or "work" in text.lower():
        decisions.append(await _choose_radio_near_text(page, "authorized", work_authorization, "work authorization"))
    await page.wait_for_timeout(500)
    text = await _body_text(page)
    if "proof of eligibility" in text.lower():
        decisions.append(
            await _choose_dropdown_near_text(
                page,
                "proof of eligibility",
                [work_authorization],
                "Proof of Eligibility",
            )
        )
    sponsorship = fields.get("visa_sponsorship", "No") or "No"
    if "sponsorship" in text.lower() or "visa" in text.lower():
        decisions.append(
            await _choose_dropdown_near_text(
                page,
                "require visa sponsorship",
                [sponsorship],
                "Visa Sponsorship",
            )
        )
        decisions.append(await _choose_radio_near_text(page, "sponsorship", sponsorship, "sponsorship"))
    salary = fields.get("desired_salary", "") or fields.get("salary", "")
    if salary and "desired annual salary" in text.lower():
        decisions.append(
            await _fill_input_near_text(
                page,
                "desired annual salary",
                "Desired Annual Salary",
                salary,
            )
        )
    return decisions


async def _fill_workday_demographic_step(page, profile: ResumeProfile) -> list[FillDecision]:
    decisions: list[FillDecision] = []
    fields = profile.fields
    text = (await _body_text(page)).lower()
    if "ethnicity" in text:
        decisions.append(
            await _choose_dropdown_near_text(
                page,
                "ethnicity",
                _answer_options(fields.get("ethnicity", ""), ["I do not wish to answer", "Decline to Answer", "Decline to Self Identify"]),
                "Ethnicity",
            )
        )
    if "gender" in text:
        decisions.append(
            await _choose_dropdown_near_text(
                page,
                "gender",
                _answer_options(fields.get("gender", "") or fields.get("self_identify_gender", ""), ["I do not wish to answer", "Decline to Answer"]),
                "Gender",
            )
        )
    if "veteran status" in text:
        decisions.append(
            await _choose_dropdown_near_text(
                page,
                "veteran status",
                _answer_options(fields.get("veteran_status", ""), ["I do not wish to answer", "I don't wish to answer", "Decline to Answer"]),
                "Veteran Status",
            )
        )
    if "disability" in text:
        disability = fields.get("disability_status", "")
        if disability:
            decisions.append(await _click_text_option(page, disability, "Disability Status"))
        else:
            decisions.append(
                await _click_text_option(page, "I do not want to answer", "Disability Status")
            )
    if "pronoun" in text and fields.get("pronouns"):
        decisions.append(
            await _fill_input_near_text(page, "pronoun", "Pronouns", fields.get("pronouns", ""))
        )
    if "voluntary self-identification of disability" in text or "omb control number" in text:
        decisions.append(
            await _fill_input_after_exact_label(
                page,
                "Name",
                "Disability Form Name",
                fields.get("full_name", ""),
            )
        )
        decisions.append(
            await _fill_date_near_text(
                page,
                "Date",
                "Disability Form Date",
                _default_start_date(),
            )
        )
    if "i certify that all information" in text:
        decisions.append(await _check_last_visible_checkbox(page, "Application certification"))
    if "decline" in text and not any(
        fields.get(key) for key in ["ethnicity", "gender", "veteran_status", "disability_status", "self_identify_gender"]
    ):
        for label in ["Decline to Answer", "I do not wish to answer", "I don't wish to answer"]:
            decision = await _click_text_option(page, label, f"demographic choice: {label}")
            if decision.action != "skip":
                decisions.append(decision)
    return decisions


def _answer_options(preferred: str, fallbacks: list[str]) -> list[str]:
    if preferred:
        return [preferred, *[fallback for fallback in fallbacks if fallback != preferred]]
    return fallbacks


def _looks_required_label(label: str) -> bool:
    return "*" in label or "required" in label.lower()


async def _quiet_wait(page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        await page.wait_for_timeout(1_500)
    await _wait_for_workday_step_ready(page)


async def _wait_for_workday_step_ready(page, timeout_ms: int = 90_000) -> None:
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
                  const uploadControl = document.querySelector('input[type="file"]')
                    || /select file|upload a file|drop file here/i.test(text);
                  const uploadDone = /Successfully Uploaded|successfully uploaded/.test(text);
                  const inApplicationStep = /current step\\s+[2-7]\\s+of\\s+7/i.test(text);
                  if (inApplicationStep) return editable || uploadControl;
                  return !hasLoadingOnly || editable || uploadDone;
                }
                """
            )
            if ready:
                return
        except Exception:
            return
        await page.wait_for_timeout(1_000)


async def _upload_resume_if_possible(page, resume_path: Path) -> bool:
    try:
        await page.wait_for_selector("input[type='file'], button:has-text('Select file')", timeout=45_000)
    except Exception:
        pass

    file_inputs = page.locator("input[type='file']")
    count = await file_inputs.count()
    if count:
        for index in range(count):
            try:
                await file_inputs.nth(index).set_input_files(str(resume_path))
                return True
            except Exception:
                continue

    upload_triggers = [
        "button:has-text('Upload')",
        "button:has-text('Resume')",
        "button:has-text('Autofill')",
        "text=/upload resume/i",
        "text=/select file/i",
        "text=/select files/i",
        "text=/choose file/i",
    ]
    for selector in upload_triggers:
        trigger = page.locator(selector).first
        try:
            if not await trigger.is_visible(timeout=1_500):
                continue
            async with page.expect_file_chooser(timeout=3_000) as chooser_info:
                await trigger.click()
            chooser = await chooser_info.value
            await chooser.set_files(str(resume_path))
            return True
        except Exception:
            continue
    return False


async def _upload_required_file_on_current_step(page, resume_path: Path) -> FillDecision:
    selector = "input[type='file']"
    text_before = await _body_text(page)
    if _resume_uploaded_on_page(text_before, resume_path):
        return FillDecision(selector, "Required resume upload", "skip", str(resume_path), 1.0, "Resume already uploaded on this step.")

    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(500)
    except Exception:
        pass

    file_inputs = page.locator(selector)
    try:
        count = await file_inputs.count()
    except Exception:
        count = 0

    for index in range(count):
        try:
            await file_inputs.nth(index).set_input_files(str(resume_path))
            if await _wait_for_uploaded_file(page, resume_path, timeout_ms=30_000):
                return FillDecision(selector, "Required resume upload", "upload", str(resume_path), 1.0, "Uploaded resume into Workday file field.")
            return FillDecision(selector, "Required resume upload", "upload", str(resume_path), 0.7, "Set file input; no success text detected.")
        except Exception:
            continue

    trigger_selectors = [
        "button:has-text('Select file')",
        "button:has-text('Select File')",
        "button:has-text('Upload')",
        "text=/select file/i",
        "text=/upload a file/i",
        "text=/drop file here/i",
    ]
    for trigger_selector in trigger_selectors:
        trigger = page.locator(trigger_selector).first
        try:
            if not await trigger.is_visible(timeout=1_000):
                continue
            async with page.expect_file_chooser(timeout=3_000) as chooser_info:
                await trigger.click()
            chooser = await chooser_info.value
            await chooser.set_files(str(resume_path))
            if await _wait_for_uploaded_file(page, resume_path, timeout_ms=30_000):
                return FillDecision(trigger_selector, "Required resume upload", "upload", str(resume_path), 1.0, "Uploaded resume using Workday file chooser.")
            return FillDecision(trigger_selector, "Required resume upload", "upload", str(resume_path), 0.7, "Set file chooser; no success text detected.")
        except Exception:
            continue

    return FillDecision(selector, "Required resume upload", "skip", str(resume_path), 0.0, "No file upload control found on this step.")


async def _wait_for_uploaded_file(page, resume_path: Path, timeout_ms: int) -> bool:
    deadline = monotonic() + (timeout_ms / 1000)
    while monotonic() < deadline:
        if _resume_uploaded_on_page(await _body_text(page), resume_path):
            return True
        await page.wait_for_timeout(1_000)
    return False


def _resume_uploaded_on_page(text: str, resume_path: Path) -> bool:
    name = resume_path.name
    return name in text and (
        "Successfully Uploaded" in text
        or "successfully uploaded" in text
        or "uploaded" in text.lower()
    )


async def _body_text(page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=5_000)
    except Exception:
        return ""


async def _workday_unavailable_message(page) -> str:
    text = await _body_text(page)
    if re.search(r"Workday is currently unavailable|service interruption", text, re.IGNORECASE):
        return "Workday is currently unavailable. Try the same resume and job URL again later."
    return ""


async def _fill_if_present(page, selector: str, label: str, value: str) -> FillDecision:
    if not value:
        return FillDecision(selector, label, "skip", "", 0.0, "No resume value available.")
    locator = page.locator(selector).first
    try:
        if not await locator.is_visible(timeout=1_000):
            return FillDecision(selector, label, "skip", value, 0.0, "Field not visible.")
        current = await locator.input_value(timeout=1_000)
        if current.strip():
            return FillDecision(selector, label, "skip", current, 1.0, "Already filled by Workday.")
        await _fill_text(locator, value)
        return FillDecision(selector, label, "fill", value, 1.0, "Filled Workday known field.")
    except Exception as exc:
        return FillDecision(selector, label, "skip", value, 0.0, f"Could not fill: {exc}")


async def _check_if_present(page, selector: str, label: str) -> FillDecision:
    locator = page.locator(selector).first
    try:
        if not await locator.is_visible(timeout=1_000):
            return FillDecision(selector, label, "skip", "", 0.0, "Checkbox not visible.")
        checked = await locator.is_checked(timeout=1_000)
        if not checked:
            await locator.check()
        return FillDecision(selector, label, "check", "true", 1.0, "Checked Workday checkbox.")
    except Exception as exc:
        return FillDecision(selector, label, "skip", "", 0.0, f"Could not check: {exc}")


async def _check_last_visible_checkbox(page, label: str) -> FillDecision:
    selector = "input[type='checkbox']"
    try:
        checkboxes = page.locator(selector)
        count = await checkboxes.count()
        for index in range(count - 1, -1, -1):
            locator = checkboxes.nth(index)
            if not await locator.is_visible(timeout=500):
                continue
            if not await locator.is_checked(timeout=500):
                await locator.check()
            return FillDecision(selector, label, "check", "true", 1.0, "Checked last visible Workday checkbox.")
        return FillDecision(selector, label, "skip", "true", 0.0, "No visible checkbox found.")
    except Exception as exc:
        return FillDecision(selector, label, "skip", "true", 0.0, f"Could not check checkbox: {exc}")


async def _fill_preferred_name_fields(page, fields: dict[str, str]) -> list[FillDecision]:
    preferred_name = fields.get("preferred_name", "")
    decisions: list[FillDecision] = []
    first_selectors = [
        "#name--preferredName--firstName",
        "input[name='preferredName--firstName']",
        "input[id*='preferred'][id*='first' i]",
        "input[name*='preferred'][name*='first' i]",
    ]
    last_selectors = [
        "#name--preferredName--lastName",
        "input[name='preferredName--lastName']",
        "input[id*='preferred'][id*='last' i]",
        "input[name*='preferred'][name*='last' i]",
    ]
    filled_first = False
    for selector in first_selectors:
        decision = await _force_fill_if_present(page, selector, "Preferred First Name", preferred_name)
        if decision.action == "fill" or "Already filled" in decision.reason:
            decisions.append(decision)
            filled_first = True
            break
    if not filled_first:
        decisions.append(
            FillDecision(
                "preferred first name",
                "Preferred First Name",
                "skip",
                preferred_name,
                0.0,
                "Preferred first name field not visible.",
            )
        )
    for selector in last_selectors:
        decision = await _force_fill_if_present(
            page,
            selector,
            "Preferred Last Name",
            fields.get("last_name", ""),
        )
        if decision.action != "skip":
            decisions.append(decision)
            break
    return decisions


async def _force_fill_if_present(page, selector: str, label: str, value: str) -> FillDecision:
    if not value:
        return FillDecision(selector, label, "skip", "", 0.0, "No resume value available.")
    locator = page.locator(selector).first
    try:
        if not await locator.is_visible(timeout=1_000):
            return FillDecision(selector, label, "skip", value, 0.0, "Field not visible.")
        current = await locator.input_value(timeout=1_000)
        if current.strip() == value:
            return FillDecision(selector, label, "skip", current, 1.0, "Already filled.")
        await _fill_text(locator, value)
        return FillDecision(selector, label, "fill", value, 1.0, "Force-filled Workday known field.")
    except Exception as exc:
        return FillDecision(selector, label, "skip", value, 0.0, f"Could not fill: {exc}")


async def _force_fill_nth_if_present(page, selector: str, index: int, label: str, value: str) -> FillDecision:
    if not value:
        return FillDecision(f"{selector} nth={index}", label, "skip", "", 0.0, "No resume value available.")
    locator = page.locator(_visible_selector(selector)).nth(_selector_match_index(selector, index))
    try:
        if not await locator.is_visible(timeout=1_000):
            return FillDecision(f"{selector} nth={index}", label, "skip", value, 0.0, "Field not visible.")
        current = await locator.input_value(timeout=1_000)
        if current.strip() == value:
            return FillDecision(f"{selector} nth={index}", label, "skip", current, 1.0, "Already filled.")
        await _fill_text(locator, value)
        return FillDecision(f"{selector} nth={index}", label, "fill", value, 1.0, "Force-filled indexed Workday field.")
    except Exception as exc:
        return FillDecision(f"{selector} nth={index}", label, "skip", value, 0.0, f"Could not fill: {exc}")


async def _force_fill_date_part_nth_if_present(page, selector: str, index: int, label: str, value: str) -> FillDecision:
    if not value:
        return FillDecision(f"{selector} nth={index}", label, "skip", "", 0.0, "No resume value available.")
    locator = page.locator(_visible_selector(selector)).nth(_selector_match_index(selector, index))
    try:
        if not await locator.is_visible(timeout=1_000):
            return FillDecision(f"{selector} nth={index}", label, "skip", value, 0.0, "Date field not visible.")
        await _set_workday_spinbutton_value(locator, value)
        await page.wait_for_timeout(150)
        return FillDecision(f"{selector} nth={index}", label, "fill", value, 1.0, "Set and committed indexed Workday date field.")
    except Exception as exc:
        return FillDecision(f"{selector} nth={index}", label, "skip", value, 0.0, f"Could not fill date: {exc}")


async def _set_workday_spinbutton_value(locator, value: str) -> None:
    await locator.evaluate(
        """
        (el, value) => {
          const normalized = String(value).trim();
          const numericText = normalized.replace(/^0+(?=\\d)/, '') || normalized;
          el.focus();
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(el, normalized);
          else el.value = normalized;
          el.setAttribute('aria-valuetext', numericText);
          if (/^\\d+$/.test(numericText)) el.setAttribute('aria-valuenow', numericText);
          el.dispatchEvent(new InputEvent('beforeinput', {bubbles: true, inputType: 'insertText', data: normalized}));
          el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: normalized}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
          el.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: 'Tab', code: 'Tab'}));
          el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'Tab', code: 'Tab'}));
          el.blur();
          el.dispatchEvent(new FocusEvent('blur', {bubbles: true}));
        }
        """,
        value,
    )


async def _check_nth_if_present(page, selector: str, index: int, label: str) -> FillDecision:
    locator = page.locator(_visible_selector(selector)).nth(_selector_match_index(selector, index))
    try:
        if not await locator.is_visible(timeout=1_000):
            return FillDecision(f"{selector} nth={index}", label, "skip", "", 0.0, "Checkbox not visible.")
        if not await locator.is_checked(timeout=1_000):
            await locator.check()
        return FillDecision(f"{selector} nth={index}", label, "check", "true", 1.0, "Checked indexed Workday checkbox.")
    except Exception as exc:
        return FillDecision(f"{selector} nth={index}", label, "skip", "", 0.0, f"Could not check: {exc}")


async def _uncheck_nth_if_present(page, selector: str, index: int, label: str) -> FillDecision:
    locator = page.locator(_visible_selector(selector)).nth(_selector_match_index(selector, index))
    try:
        if not await locator.is_visible(timeout=1_000):
            return FillDecision(f"{selector} nth={index}", label, "skip", "", 0.0, "Checkbox not visible.")
        if await locator.is_checked(timeout=1_000):
            await locator.uncheck()
        return FillDecision(f"{selector} nth={index}", label, "uncheck", "false", 1.0, "Unchecked indexed Workday checkbox.")
    except Exception as exc:
        return FillDecision(f"{selector} nth={index}", label, "skip", "", 0.0, f"Could not uncheck: {exc}")


async def _choose_dropdown_by_button_id(
    page,
    button_id: str,
    option_text: str,
    label: str,
) -> FillDecision:
    selector = f"#{button_id}"
    if not option_text:
        return FillDecision(selector, label, "skip", "", 0.0, "No option value available.")
    button = page.locator(selector).first
    try:
        if not await button.is_visible(timeout=1_000):
            return FillDecision(selector, label, "skip", option_text, 0.0, "Dropdown not visible.")
        current = (await button.inner_text(timeout=1_000)).strip()
        if option_text.lower() in current.lower() and "select one" not in current.lower():
            return FillDecision(selector, label, "skip", current, 1.0, "Already selected.")
        await button.click()
        await page.wait_for_timeout(500)
        if await _click_option_text(page, option_text) or await _click_option_text_fuzzy(page, option_text):
            if await _dropdown_has_value(button):
                return FillDecision(selector, label, "select", option_text, 1.0, "Selected Workday dropdown.")
        if await _type_dropdown_option(page, button, option_text):
            return FillDecision(selector, label, "select", option_text, 0.9, "Selected Workday dropdown by keyboard search.")
        return FillDecision(selector, label, "skip", option_text, 0.0, "Option not found.")
    except Exception as exc:
        return FillDecision(selector, label, "skip", option_text, 0.0, f"Could not select: {exc}")


async def _choose_dropdown_near_text(
    page,
    question_text: str,
    options: list[str],
    label: str,
) -> FillDecision:
    selector = await _mark_visible_control_after_text(
        page,
        question_text,
        "button",
    )
    if not selector:
        return FillDecision(f"dropdown near {question_text}", label, "skip", ", ".join(options), 0.0, "Dropdown not visible.")
    button = page.locator(selector).first
    try:
        current = (await button.inner_text(timeout=1_000)).strip()
        if current and current.lower() != "select one":
            return FillDecision(selector, label, "skip", current, 1.0, "Already selected.")
        await _click_locator_with_mouse(page, button)
        await page.wait_for_timeout(500)
        for option in options:
            if await _click_option_text(page, option) or await _click_option_text_fuzzy(page, option):
                if await _dropdown_has_value(button):
                    return FillDecision(selector, label, "select", option, 1.0, "Selected Workday dropdown near question.")
            if await _type_dropdown_option(page, button, option):
                return FillDecision(selector, label, "select", option, 0.9, "Selected Workday dropdown near question by keyboard search.")
        return FillDecision(selector, label, "skip", ", ".join(options), 0.0, "No dropdown option matched.")
    except Exception as exc:
        return FillDecision(selector, label, "skip", ", ".join(options), 0.0, f"Could not select: {exc}")


async def _fill_input_near_text(
    page,
    question_text: str,
    label: str,
    value: str,
    input_selector: str = "input, textarea, [role='textbox']",
) -> FillDecision:
    if not value:
        return FillDecision(f"input near {question_text}", label, "skip", "", 0.0, "No value available.")
    selector = await _mark_visible_control_after_text(page, question_text, input_selector)
    if not selector:
        return FillDecision(f"input near {question_text}", label, "skip", value, 0.0, "Input not visible.")
    locator = page.locator(selector).first
    try:
        await _fill_text(locator, value)
        return FillDecision(selector, label, "fill", value, 1.0, "Filled Workday input near question.")
    except Exception as exc:
        return FillDecision(selector, label, "skip", value, 0.0, f"Could not fill: {exc}")


async def _fill_input_after_exact_label(
    page,
    label_text: str,
    label: str,
    value: str,
) -> FillDecision:
    if not value:
        return FillDecision(f"input after {label_text}", label, "skip", "", 0.0, "No value available.")
    marker = "data-job-ai2-exact-label-input"
    found = await page.evaluate(
        """
        ([labelText, marker]) => {
          document.querySelectorAll(`[${marker}]`).forEach(el => el.removeAttribute(marker));
          const wanted = labelText.trim().toLowerCase();
          const visible = (el) => {
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
          };
          const clean = (text) => (text || '').replace(/\\*/g, '').trim().toLowerCase();
          const textOf = (el) => (el.innerText || el.textContent || '').trim();
          const nodes = Array.from(document.querySelectorAll('label,legend,p,div,span,input'))
            .filter(visible);
          const labelIndex = nodes.findIndex(el => {
            if (el.tagName === 'INPUT') return false;
            return clean(textOf(el)) === wanted;
          });
          if (labelIndex < 0) return false;
          for (let index = labelIndex + 1; index < nodes.length; index += 1) {
            const el = nodes[index];
            if (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA') continue;
            const type = (el.getAttribute('type') || '').toLowerCase();
            if (['hidden', 'file', 'button', 'submit', 'reset', 'image', 'checkbox', 'radio'].includes(type)) continue;
            if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
            el.setAttribute(marker, 'true');
            return true;
          }
          return false;
        }
        """,
        [label_text, marker],
    )
    selector = f"[{marker}='true']"
    if not found:
        return FillDecision(f"input after {label_text}", label, "skip", value, 0.0, "Input not visible after exact label.")
    try:
        await _fill_text(page.locator(selector).first, value)
        return FillDecision(selector, label, "fill", value, 1.0, "Filled Workday input after exact label.")
    except Exception as exc:
        return FillDecision(selector, label, "skip", value, 0.0, f"Could not fill: {exc}")


async def _fill_date_near_text(
    page,
    question_text: str,
    label: str,
    value: str,
) -> FillDecision:
    match = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*", value)
    if not match:
        return await _fill_input_near_text(page, question_text, label, value)
    month, day, year = (part.zfill(2) if index < 2 else part for index, part in enumerate(match.groups()))
    found = await page.evaluate(
        """
        ([questionText]) => {
          document.querySelectorAll('[data-job-ai2-date-part]').forEach(el => el.removeAttribute('data-job-ai2-date-part'));
          const needle = questionText.trim().toLowerCase();
          const visible = (el) => {
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
          };
          const textOf = (el) => (el.innerText || el.textContent || '').trim();
          const nodes = Array.from(document.querySelectorAll('label,legend,h1,h2,h3,h4,p,div,span,input'))
            .filter(visible);
          const matchesQuestion = (el, exactOnly) => {
            const text = textOf(el).toLowerCase();
            const clean = text.replace(/\\*/g, '').trim();
            if (text.includes('error -') || text.includes('errors found')) return false;
            if (clean === needle) return true;
            return !exactOnly && text.includes(needle) && text.length < 700;
          };
          let questionIndex = nodes.findIndex(el => matchesQuestion(el, true));
          if (questionIndex < 0) questionIndex = nodes.findIndex(el => matchesQuestion(el, false));
          if (questionIndex < 0) return false;
          const inputs = [];
          for (let index = questionIndex + 1; index < nodes.length; index += 1) {
            const el = nodes[index];
            if (el.tagName !== 'INPUT') continue;
            const type = (el.getAttribute('type') || '').toLowerCase();
            if (['hidden', 'file', 'button', 'submit', 'reset', 'image'].includes(type)) continue;
            if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
            inputs.push(el);
            if (inputs.length === 3) break;
          }
          if (inputs.length < 3) return false;
          inputs.forEach((el, index) => el.setAttribute('data-job-ai2-date-part', String(index)));
          return true;
        }
        """,
        [question_text],
    )
    if not found:
        return await _fill_input_near_text(page, question_text, label, value)
    try:
        for index, part in enumerate([month, day, year]):
            await _set_workday_spinbutton_value(
                page.locator(f"[data-job-ai2-date-part='{index}']").first,
                part,
            )
            await page.wait_for_timeout(100)
        return FillDecision("[data-job-ai2-date-part]", label, "fill", value, 1.0, "Filled Workday split date fields.")
    except Exception as exc:
        return FillDecision("[data-job-ai2-date-part]", label, "skip", value, 0.0, f"Could not fill split date: {exc}")


async def _mark_visible_control_after_text(
    page,
    question_text: str,
    control_selector: str,
) -> str:
    marker = "data-job-ai2-near-control"
    found = await page.evaluate(
        """
        ([questionText, controlSelector, marker]) => {
          document.querySelectorAll(`[${marker}]`).forEach(el => el.removeAttribute(marker));
          const needle = questionText.trim().toLowerCase();
          const visible = (el) => {
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
          };
          const textOf = (el) => (el.innerText || el.textContent || '').trim();
          const nodes = Array.from(document.querySelectorAll('label,legend,h1,h2,h3,h4,p,div,span,input,textarea,button,[role="textbox"],[role="combobox"]'))
            .filter(visible);
          let questionIndex = nodes.findIndex(el => {
            const text = textOf(el).toLowerCase();
            return text.includes(needle)
              && text.length < 300
              && !text.includes('back to job posting')
              && !text.includes('errors found');
          });
          if (questionIndex < 0) return false;
          for (let index = questionIndex + 1; index < nodes.length; index += 1) {
            const el = nodes[index];
            if (!el.matches(controlSelector)) continue;
            const controlText = textOf(el).toLowerCase();
            if (['back', 'next', 'submit', 'save and continue', 'continue'].includes(controlText)) continue;
            if (controlText.length > 120) continue;
            const type = (el.getAttribute('type') || '').toLowerCase();
            const blockedTypes = el.tagName === 'BUTTON'
              ? ['hidden', 'file', 'submit', 'reset', 'image']
              : ['hidden', 'file', 'button', 'submit', 'reset', 'image'];
            if (blockedTypes.includes(type)) continue;
            if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
            el.setAttribute(marker, 'true');
            return true;
          }
          return false;
        }
        """,
        [question_text, control_selector, marker],
    )
    return f"[{marker}='true']" if found else ""


async def _click_option_text(page, option_text: str) -> bool:
    candidates = [
        page.get_by_role("option", name=option_text, exact=True),
        page.get_by_role("menuitem", name=option_text, exact=True),
        page.get_by_text(option_text, exact=True),
    ]
    for locator in candidates:
        try:
            if await locator.first.is_visible(timeout=1_000):
                await locator.first.click()
                await page.wait_for_timeout(500)
                return True
        except Exception:
            continue
    return False


async def _click_option_text_fuzzy(page, option_text: str) -> bool:
    try:
        clicked = await page.evaluate(
            """
            (optionText) => {
              const needle = optionText.trim().toLowerCase();
              const candidates = Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], li, div, button'))
                .filter(el => {
                  const rect = el.getBoundingClientRect();
                  const text = (el.innerText || el.textContent || '').trim();
                  return rect.width > 0 && rect.height > 0 && text;
                });
              const match = candidates.find(el => {
                const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                return text === needle || text.includes(needle) || needle.includes(text);
              });
              if (!match) return false;
              match.scrollIntoView({block: 'center'});
              match.click();
              return true;
            }
            """,
            option_text,
        )
        if clicked:
            await page.wait_for_timeout(500)
        return bool(clicked)
    except Exception:
        return False


async def _dropdown_has_value(button) -> bool:
    try:
        text = (await button.inner_text(timeout=1_000)).strip().lower()
        return bool(text and text != "select one")
    except Exception:
        return False


async def _type_dropdown_option(page, button, option_text: str) -> bool:
    try:
        await button.click()
        await page.wait_for_timeout(300)
        await page.keyboard.type(option_text, delay=20)
        await page.wait_for_timeout(700)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(700)
        if await _dropdown_has_value(button):
            return True
        await page.keyboard.press("Escape")
        return False
    except Exception:
        return False


async def _choose_radio_near_text(
    page,
    question_text: str,
    answer_text: str,
    label: str,
) -> FillDecision:
    selector = f"radio near {question_text}"
    try:
        question = page.get_by_text(question_text, exact=False).first
        if not await question.is_visible(timeout=1_000):
            return FillDecision(selector, label, "skip", answer_text, 0.0, "Question not visible.")
        try:
            radio = page.get_by_label(answer_text, exact=True).first
            if await radio.is_visible(timeout=1_000):
                await radio.check()
                return FillDecision(selector, label, "check", answer_text, 1.0, "Selected Workday radio by label.")
        except Exception:
            pass
        clicked = await page.evaluate(
            """
            ([questionText, answerText]) => {
              const lowerQuestion = questionText.toLowerCase();
              const lowerAnswer = answerText.toLowerCase();
              const candidates = Array.from(document.querySelectorAll('input[type="radio"]'));
              for (const input of candidates) {
                const label = input.closest('label');
                const localText = (label?.innerText || input.parentElement?.innerText || '').trim().toLowerCase();
                const containerText = (input.closest('fieldset, section, div')?.innerText || '').trim().toLowerCase();
                if (localText === lowerAnswer && containerText.includes(lowerQuestion)) {
                  input.click();
                  return true;
                }
              }
              for (const input of candidates) {
                const text = (input.parentElement?.innerText || '').trim().toLowerCase();
                if (text.includes(lowerAnswer)) {
                  input.click();
                  return true;
                }
              }
              return false;
            }
            """,
            [question_text, answer_text],
        )
        if clicked:
            return FillDecision(selector, label, "check", answer_text, 1.0, "Selected Workday radio.")
        return FillDecision(selector, label, "skip", answer_text, 0.0, "Radio option not found.")
    except Exception as exc:
        return FillDecision(selector, label, "skip", answer_text, 0.0, f"Could not select: {exc}")


async def _click_text_option(page, text: str, label: str) -> FillDecision:
    try:
        if await _click_option_text(page, text):
            return FillDecision(f"text={text}", label, "check", text, 0.8, "Clicked visible option.")
        return FillDecision(f"text={text}", label, "skip", text, 0.0, "Option not visible.")
    except Exception as exc:
        return FillDecision(f"text={text}", label, "skip", text, 0.0, f"Could not click: {exc}")


async def _fill_first_visible_textarea_or_textbox(
    page,
    label: str,
    value: str,
) -> FillDecision:
    if not value:
        return FillDecision("textarea", label, "skip", "", 0.0, "No resume value available.")
    locator = page.locator("textarea, [role='textbox'], [contenteditable='true']").first
    try:
        if not await locator.is_visible(timeout=1_000):
            return FillDecision("textarea", label, "skip", value, 0.0, "No text area visible.")
        await _fill_text(locator, value)
        return FillDecision("textarea", label, "fill", value, 0.8, "Filled first visible text area.")
    except Exception as exc:
        return FillDecision("textarea", label, "skip", value, 0.0, f"Could not fill: {exc}")


async def _is_visible_selector(page, selector: str) -> bool:
    try:
        return await page.locator(selector).first.is_visible(timeout=1_000)
    except Exception:
        return False


async def _visible_locator_count(page, selector: str) -> int:
    locator = page.locator(selector)
    try:
        count = await locator.count()
    except Exception:
        return 0
    visible_count = 0
    for index in range(count):
        try:
            if await locator.nth(index).is_visible(timeout=300):
                visible_count += 1
        except Exception:
            continue
    return visible_count


def _visible_selector(selector: str) -> str:
    return f"{selector}:visible"


def _selector_match_index(selector: str, index: int) -> int:
    return 0 if selector.startswith("#") else index


async def _ensure_section_entry_count(
    page,
    section: str,
    entry_selector: str,
    desired_count: int,
) -> list[FillDecision]:
    decisions: list[FillDecision] = []
    if desired_count <= 0:
        return decisions
    for _ in range(desired_count + 4):
        count = await _visible_locator_count(page, entry_selector)
        if count >= desired_count:
            break
        decisions.append(await _click_add_for_section(page, section))
        await _wait_for_selector_count(page, entry_selector, count + 1, timeout_ms=10_000)
    if section == "Work Experience":
        decisions.extend(await _delete_extra_work_experience_entries(page, keep_count=desired_count))
    elif section == "Education":
        decisions.extend(await _delete_extra_education_entries(page, keep_count=desired_count))
    return decisions


async def _wait_for_selector_count(page, selector: str, target_count: int, timeout_ms: int) -> bool:
    deadline = monotonic() + (timeout_ms / 1000)
    while monotonic() < deadline:
        if await _visible_locator_count(page, selector) >= target_count:
            return True
        await page.wait_for_timeout(500)
    return False


async def _click_add_for_section(page, section: str) -> FillDecision:
    marker = "data-job-ai2-add-button"
    try:
        found = await page.evaluate(
            """
            (section) => {
              document.querySelectorAll('[data-job-ai2-add-button]').forEach(el => el.removeAttribute('data-job-ai2-add-button'));
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              };
              const elements = Array.from(document.querySelectorAll('h1,h2,h3,h4,div,span,button')).filter(visible);
              const headingIndex = elements.findIndex(el => (el.innerText || '').trim() === section);
              if (headingIndex < 0) return false;
              const stopSections = new Set(['Work Experience', 'Education', 'Languages', 'Skills', 'Resume/CV', 'Websites', 'Social Network URLs']);
              for (let index = headingIndex + 1; index < elements.length; index += 1) {
                const el = elements[index];
                const text = (el.innerText || '').trim();
                if (stopSections.has(text) && text !== section) return false;
                if (el.tagName === 'BUTTON' && /^Add(?: Another)?$/i.test(text)) {
                  el.setAttribute('data-job-ai2-add-button', 'true');
                  return true;
                }
              }
              return false;
            }
            """,
            section,
        )
        if found:
            locator = page.locator(f"[{marker}='true']").first
            await _click_locator_with_mouse(page, locator)
            return FillDecision(f"{section} Add", section, "click", "Add", 1.0, "Clicked Workday section Add button with mouse.")
        return FillDecision(f"{section} Add", section, "skip", "Add", 0.0, "Add button not found.")
    except Exception as exc:
        return FillDecision(f"{section} Add", section, "skip", "Add", 0.0, f"Could not click Add: {exc}")


async def _delete_extra_education_entries(page, keep_count: int) -> list[FillDecision]:
    decisions: list[FillDecision] = []
    for _ in range(10):
        selector = await _mark_last_extra_delete_button_between_sections(page, "Education", "Languages", keep_count)
        if not selector:
            selector = await _mark_last_extra_delete_button_between_sections(page, "Education", "Skills", keep_count)
        if not selector:
            break
        try:
            await _click_locator_with_mouse(page, page.locator(selector).first)
            await page.wait_for_timeout(600)
            await _confirm_delete_if_prompted(page)
            decisions.append(
                FillDecision(
                    selector,
                    "Extra Education",
                    "click",
                    "Delete",
                    0.9,
                    "Deleted extra Workday education block to match resume count.",
                )
            )
        except Exception as exc:
            decisions.append(
                FillDecision(
                    selector,
                    "Extra Education",
                    "skip",
                    "Delete",
                    0.0,
                    f"Could not delete extra education block: {exc}",
                )
            )
            break
    return decisions


async def _click_locator_with_mouse(page, locator) -> None:
    try:
        await locator.scroll_into_view_if_needed(timeout=2_000)
        await page.wait_for_timeout(250)
        box = await locator.bounding_box(timeout=2_000)
        if box:
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            await page.mouse.move(x, y, steps=8)
            await page.wait_for_timeout(80)
            await page.mouse.down()
            await page.wait_for_timeout(80)
            await page.mouse.up()
            await page.wait_for_timeout(500)
            return
    except Exception:
        pass
    await locator.click()
    await page.wait_for_timeout(500)


async def _wait_for_selector_visible(page, selector: str, timeout_ms: int) -> bool:
    deadline = monotonic() + (timeout_ms / 1000)
    while monotonic() < deadline:
        if await _is_visible_selector(page, selector):
            return True
        await page.wait_for_timeout(500)
    return False


async def _choose_dropdown_by_selector_options(
    page,
    selector: str,
    options: list[str],
    label: str,
) -> FillDecision:
    button = page.locator(selector).first
    try:
        if not await button.is_visible(timeout=1_000):
            return FillDecision(selector, label, "skip", "", 0.0, "Dropdown not visible.")
        current = (await button.inner_text(timeout=1_000)).strip()
        if current and current.lower() != "select one":
            return FillDecision(selector, label, "skip", current, 1.0, "Already selected.")
        await button.click()
        await page.wait_for_timeout(500)
        for option in options:
            if await _click_option_text(page, option) or await _click_option_text_fuzzy(page, option):
                if await _dropdown_has_value(button):
                    return FillDecision(selector, label, "select", option, 1.0, "Selected Workday dropdown.")
            if await _type_dropdown_option(page, button, option):
                return FillDecision(selector, label, "select", option, 0.9, "Selected Workday dropdown by keyboard search.")
        return FillDecision(selector, label, "skip", ", ".join(options), 0.0, "No dropdown option matched.")
    except Exception as exc:
        return FillDecision(selector, label, "skip", ", ".join(options), 0.0, f"Could not select: {exc}")


async def _choose_dropdown_nth_by_selector_options(
    page,
    selector: str,
    index: int,
    options: list[str],
    label: str,
) -> FillDecision:
    button = page.locator(_visible_selector(selector)).nth(index)
    decision_selector = f"{selector} nth={index}"
    try:
        if not await button.is_visible(timeout=1_000):
            return FillDecision(decision_selector, label, "skip", "", 0.0, "Dropdown not visible.")
        current = (await button.inner_text(timeout=1_000)).strip()
        if current and current.lower() != "select one":
            return FillDecision(decision_selector, label, "skip", current, 1.0, "Already selected.")
        await page.keyboard.press("Escape")
        await _click_locator_with_mouse(page, button)
        await page.wait_for_timeout(500)
        for option in options:
            if await _click_option_text(page, option) or await _click_option_text_fuzzy(page, option):
                if await _dropdown_has_value(button):
                    return FillDecision(decision_selector, label, "select", option, 1.0, "Selected indexed Workday dropdown.")
            if await _type_dropdown_option(page, button, option):
                return FillDecision(decision_selector, label, "select", option, 0.9, "Selected indexed Workday dropdown by keyboard search.")
        return FillDecision(decision_selector, label, "skip", ", ".join(options), 0.0, "No dropdown option matched.")
    except Exception as exc:
        return FillDecision(decision_selector, label, "skip", ", ".join(options), 0.0, f"Could not select: {exc}")


async def _choose_dropdown_by_aria_options(
    page,
    aria_label_prefix: str,
    options: list[str],
    label: str,
) -> FillDecision:
    selector = f"button[aria-label^='{aria_label_prefix} ']"
    decision = await _choose_dropdown_by_selector_options(page, selector, options, label)
    if decision.action == "skip":
        selector = f"button[aria-label*='{aria_label_prefix}']"
        decision = await _choose_dropdown_by_selector_options(page, selector, options, label)
    return decision


async def _fill_token_input(
    page,
    selector: str,
    label: str,
    value: str,
) -> FillDecision:
    if not value:
        return FillDecision(selector, label, "skip", "", 0.0, "No value available.")
    locator = page.locator(selector).first
    try:
        if not await locator.is_visible(timeout=1_000):
            return FillDecision(selector, label, "skip", value, 0.0, "Token input not visible.")
        await page.keyboard.press("Escape")
        await locator.click()
        await locator.fill(value)
        await page.wait_for_timeout(600)
        if await _click_option_text(page, value) or await _click_option_text_fuzzy(page, value):
            return FillDecision(selector, label, "fill", value, 0.9, "Selected Workday token option.")
        await locator.press("Enter")
        return FillDecision(selector, label, "fill", value, 0.8, "Filled Workday token input.")
    except Exception as exc:
        return FillDecision(selector, label, "skip", value, 0.0, f"Could not fill token input: {exc}")


async def _fill_token_input_nth(
    page,
    selector: str,
    index: int,
    label: str,
    value: str,
) -> FillDecision:
    decision_selector = f"{selector} nth={index}"
    if not value:
        return FillDecision(decision_selector, label, "skip", "", 0.0, "No value available.")
    locator = page.locator(_visible_selector(selector)).nth(index)
    try:
        if not await locator.is_visible(timeout=1_000):
            return FillDecision(decision_selector, label, "skip", value, 0.0, "Token input not visible.")
        await page.keyboard.press("Escape")
        await locator.click()
        await locator.fill(value)
        await page.wait_for_timeout(600)
        if await _click_option_text(page, value) or await _click_option_text_fuzzy(page, value):
            return FillDecision(decision_selector, label, "fill", value, 0.9, "Selected indexed Workday token option.")
        await locator.press("Enter")
        return FillDecision(decision_selector, label, "fill", value, 0.8, "Filled indexed Workday token input.")
    except Exception as exc:
        return FillDecision(decision_selector, label, "skip", value, 0.0, f"Could not fill token input: {exc}")


def _degree_options(value: str) -> list[str]:
    lowered = value.lower()
    if "doctor" in lowered or "phd" in lowered or "ph.d" in lowered:
        return [value, "Doctorate", "Doctorate Degree", "PhD", "Ph.D.", "Doctor of Philosophy"]
    if "master" in lowered:
        return [value, "Master's Degree", "Master of Science", "MS", "M.S."]
    if "bachelor" in lowered:
        return [value, "Bachelor's Degree", "Bachelor of Science", "BS", "B.S."]
    if "certificate" in lowered:
        return [value, "Certificate"]
    return [value] if value else []


def _state_name(value: str) -> str:
    states = {
        "AL": "Alabama",
        "AK": "Alaska",
        "AZ": "Arizona",
        "AR": "Arkansas",
        "CA": "California",
        "CO": "Colorado",
        "CT": "Connecticut",
        "DE": "Delaware",
        "FL": "Florida",
        "GA": "Georgia",
        "HI": "Hawaii",
        "IA": "Iowa",
        "ID": "Idaho",
        "IL": "Illinois",
        "IN": "Indiana",
        "KS": "Kansas",
        "KY": "Kentucky",
        "LA": "Louisiana",
        "MA": "Massachusetts",
        "MD": "Maryland",
        "ME": "Maine",
        "MI": "Michigan",
        "MN": "Minnesota",
        "MO": "Missouri",
        "MS": "Mississippi",
        "MT": "Montana",
        "NC": "North Carolina",
        "ND": "North Dakota",
        "NE": "Nebraska",
        "NH": "New Hampshire",
        "NJ": "New Jersey",
        "NM": "New Mexico",
        "NV": "Nevada",
        "NY": "New York",
        "OH": "Ohio",
        "OK": "Oklahoma",
        "OR": "Oregon",
        "PA": "Pennsylvania",
        "RI": "Rhode Island",
        "SC": "South Carolina",
        "SD": "South Dakota",
        "TN": "Tennessee",
        "TX": "Texas",
        "UT": "Utah",
        "VA": "Virginia",
        "VT": "Vermont",
        "WA": "Washington",
        "WI": "Wisconsin",
        "WV": "West Virginia",
        "WY": "Wyoming",
    }
    stripped = value.strip()
    return states.get(stripped.upper(), stripped)


def _default_start_date() -> str:
    return date.today().strftime("%m/%d/%Y")


async def _collect_fields(page) -> list[ApplicationField]:
    raw_fields = await page.evaluate(
        """
        () => {
          const labelMap = new Map(
            Array.from(document.querySelectorAll('label[for]')).map(
              label => [label.getAttribute('for'), label.innerText.trim()]
            )
          );
          function esc(value) {
            if (window.CSS && window.CSS.escape) return window.CSS.escape(value);
            return String(value).replace(/"/g, '\\"');
          }
          function selectorFor(el, index) {
            const marker = `job-ai2-field-${index}`;
            el.setAttribute('data-job-ai2-selector', marker);
            return `[data-job-ai2-selector="${marker}"]`;
          }
          function labelFor(el) {
            if (el.labels && el.labels.length) {
              return Array.from(el.labels).map(label => label.innerText.trim()).join(' ');
            }
            if (el.id && labelMap.has(el.id)) return labelMap.get(el.id);
            const labelledBy = el.getAttribute('aria-labelledby');
            if (labelledBy) {
              return labelledBy.split(/\\s+/)
                .map(id => document.getElementById(id))
                .filter(Boolean)
                .map(node => node.innerText || node.textContent || '')
                .join(' ')
                .trim();
            }
            return el.getAttribute('aria-label')
              || el.getAttribute('placeholder')
              || el.name
              || el.id
              || '';
          }
          return Array.from(document.querySelectorAll(
              'input, textarea, select, [contenteditable="true"], [role="textbox"], [role="combobox"], [role="checkbox"], [role="radio"]'
            ))
            .filter(el => {
              const type = (el.getAttribute('type') || '').toLowerCase();
              const rect = el.getBoundingClientRect();
              return !['hidden', 'button', 'submit', 'reset', 'image', 'file'].includes(type)
                && rect.width > 0
                && rect.height > 0
                && !el.disabled
                && el.getAttribute('aria-disabled') !== 'true';
            })
            .map((el, index) => ({
              selector: selectorFor(el, index),
              label: labelFor(el),
              tag: el.tagName.toLowerCase(),
              input_type: (el.getAttribute('type') || el.tagName).toLowerCase(),
              name: el.getAttribute('name') || '',
              placeholder: el.getAttribute('placeholder') || '',
              required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
              options: el.tagName.toLowerCase() === 'select'
                ? Array.from(el.options).map(option => option.innerText.trim()).filter(Boolean)
                : []
            }));
        }
        """
    )
    return [ApplicationField(**item) for item in raw_fields]


async def _apply_decision(page, decision: FillDecision) -> None:
    locator = page.locator(decision.selector).first
    if decision.action == "select":
        await _select_best_option(locator, decision.value)
    elif decision.action == "check":
        try:
            await locator.check()
        except Exception:
            await locator.click()
    else:
        await _fill_text(locator, decision.value)


async def _fill_text(locator, value: str) -> None:
    last_error: Exception | None = None
    try:
        await locator.fill(value)
        await _commit_text_value(locator)
        if await _locator_has_text_value(locator, value):
            return
    except Exception as exc:
        last_error = exc

    await locator.click()
    for shortcut in ("Meta+A", "Control+A"):
        try:
            await locator.press(shortcut)
            break
        except Exception:
            continue
    try:
        await locator.press("Backspace")
    except Exception:
        pass
    await locator.type(value, delay=8)
    await _commit_text_value(locator)
    if await _locator_has_text_value(locator, value):
        return

    try:
        await locator.evaluate(
            """
            (el, value) => {
              if ('value' in el) {
                el.value = value;
              } else if (el.isContentEditable) {
                el.textContent = value;
              }
              el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              el.dispatchEvent(new Event('blur', { bubbles: true }));
            }
            """,
            value,
        )
        await _commit_text_value(locator)
        if await _locator_has_text_value(locator, value):
            return
    except Exception as exc:
        last_error = exc

    if last_error:
        raise last_error
    raise ValueError("Text field did not retain filled value.")


async def _commit_text_value(locator) -> None:
    try:
        await locator.evaluate(
            """
            (el) => {
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              el.dispatchEvent(new Event('blur', { bubbles: true }));
            }
            """
        )
    except Exception:
        pass
    try:
        await locator.press("Tab")
    except Exception:
        pass
    await sleep(0.15)


async def _locator_has_text_value(locator, expected: str) -> bool:
    try:
        actual = await locator.input_value(timeout=1_000)
    except Exception:
        try:
            actual = await locator.evaluate(
                "(el) => ('value' in el ? el.value : (el.innerText || el.textContent || ''))"
            )
        except Exception:
            return False
    return actual.strip() == expected.strip()


async def _select_best_option(locator, value: str) -> None:
    try:
        await locator.select_option(label=value)
    except Exception:
        try:
            await locator.select_option(value=value)
        except Exception:
            await locator.click()
            await locator.type(value, delay=8)
            await locator.press("Enter")


async def _click_safe_next(page) -> bool:
    next_selectors = [
        "button:has-text('Save and Continue')",
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "button:has-text('OK')",
        "[role='button']:has-text('Save and Continue')",
        "[role='button']:has-text('Continue')",
        "[role='button']:has-text('Next')",
    ]
    blocked_words = ("submit", "apply", "send application", "finish")
    for selector in next_selectors:
        locator = page.locator(selector).first
        try:
            if not await locator.is_visible(timeout=1_500):
                continue
            text = (await locator.inner_text(timeout=1_000)).strip().lower()
            if any(word in text for word in blocked_words):
                continue
            if await _is_disabled(locator):
                continue
            before = await _current_step_text(page)
            await _click_locator_with_mouse(page, locator)
            await page.wait_for_timeout(1_500)
            after = await _current_step_text(page)
            if after != before:
                return True
            if "Loading" in await _body_text(page):
                return True
        except Exception:
            continue
    return False


async def _wait_and_click_safe_next(page, timeout_ms: int) -> bool:
    deadline = monotonic() + (timeout_ms / 1000)
    while monotonic() < deadline:
        before = await _current_step_text(page)
        clicked = await _click_safe_next(page)
        if clicked:
            await page.wait_for_timeout(1_500)
            after = await _current_step_text(page)
            if after != before:
                return True
            if "Loading" in await _body_text(page):
                return True
        await page.wait_for_timeout(1_000)
    return False


async def _wait_for_safe_next_enabled(page, timeout_ms: int) -> bool:
    deadline = monotonic() + (timeout_ms / 1000)
    while monotonic() < deadline:
        if await _has_enabled_safe_next(page):
            return True
        await page.wait_for_timeout(1_000)
    return False


async def _has_enabled_safe_next(page) -> bool:
    next_selectors = [
        "button:has-text('Save and Continue')",
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "[role='button']:has-text('Save and Continue')",
        "[role='button']:has-text('Continue')",
        "[role='button']:has-text('Next')",
    ]
    for selector in next_selectors:
        locator = page.locator(selector).first
        try:
            if await locator.is_visible(timeout=500) and not await _is_disabled(locator):
                return True
        except Exception:
            continue
    return False


async def _is_disabled(locator) -> bool:
    try:
        if not await locator.is_enabled(timeout=500):
            return True
    except Exception:
        pass
    try:
        return await locator.evaluate(
            """
            (el) => {
              const style = window.getComputedStyle(el);
              return Boolean(
                el.disabled
                || el.getAttribute('aria-disabled') === 'true'
                || el.getAttribute('data-disabled') === 'true'
                || /disabled/i.test(el.className || '')
                || style.pointerEvents === 'none'
                || Number(style.opacity || '1') < 0.55
              );
            }
            """
        )
    except Exception:
        return False


async def _current_step_text(page) -> str:
    try:
        return await page.evaluate(
            """
            () => {
              const text = document.body.innerText || '';
              const current = text.match(/current step\\s+\\d+\\s+of\\s+\\d+\\n[^\\n]+/i);
              return current ? current[0] : text.slice(0, 500);
            }
            """
        )
    except Exception:
        return ""
