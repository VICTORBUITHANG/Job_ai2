# File: /Users/victorbui/AI/Job_ai2/job_ai2_agent/web_app.py
from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from job_ai2_agent.account_store import (
    account_fields,
    ensure_account_profile,
    save_account_fields,
)
from job_ai2_agent.config import PROJECT_ROOT, ensure_artifact_dirs, load_settings
from job_ai2_agent.resume_reader import read_resume_profile
from job_ai2_agent.service import JobApplicationAgent


settings = load_settings()
ensure_artifact_dirs(settings)

app = FastAPI(title="Job_ai2")
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return _page(
        """
        <section class="panel">
          <h1>Job_ai2</h1>
          <form action="/run-agent" method="post" enctype="multipart/form-data">
            <label>
              Resume PDF or DOCX
              <input type="file" name="resume" accept=".pdf,.docx" required>
            </label>
            <label>
              Job application link
              <input type="url" name="job_url" placeholder="https://company.com/apply" required>
            </label>
            <div class="grid-two">
              <label>
                Phone
                <input type="tel" name="phone" placeholder="5551234567">
              </label>
              <label>
                Postal code
                <input type="text" name="postal_code" placeholder="48104">
              </label>
            </div>
            <label>
              Address line 1
              <input type="text" name="address_line1" placeholder="Street address">
            </label>
            <div class="grid-two">
              <label>
                City
                <input type="text" name="city" placeholder="Ann Arbor">
              </label>
              <label>
                State
                <input type="text" name="state" placeholder="MI">
              </label>
            </div>
            <button type="submit">Start agent</button>
          </form>
        </section>
        """
    )


@app.post("/run-agent", response_class=HTMLResponse)
async def run_agent(
    background_tasks: BackgroundTasks,
    resume: UploadFile = File(...),
    job_url: str = Form(...),
    phone: str = Form(""),
    postal_code: str = Form(""),
    address_line1: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
) -> str:
    if urlparse(job_url).scheme not in {"http", "https"}:
        return _page(
            """
            <section class="panel">
              <h1>Invalid link</h1>
              <p>Please enter a full http or https job application URL.</p>
              <a class="button-link" href="/">Try again</a>
            </section>
            """
        )
    saved_resume = await _save_upload(resume)
    resume_profile = read_resume_profile(saved_resume)
    email = resume_profile.fields.get("email", "").strip().lower()
    if not email:
        return _page(
            """
            <section class="panel">
              <h1>Email not found</h1>
              <p>The resume needs an email address so Job_ai2 can create the account profile.</p>
              <a class="button-link" href="/">Try another resume</a>
            </section>
            """
        )
    account = ensure_account_profile(settings, email)
    overrides = {
        "phone": phone,
        "postal_code": postal_code,
        "address_line1": address_line1,
        "city": city,
        "state": state,
    }
    if not account.get("completed_sections"):
        return _profile_page(
            email=email,
            resume_name=saved_resume.name,
            job_url=job_url,
            overrides=overrides,
            fields=account_fields(account),
        )
    background_tasks.add_task(
        _run_background_agent,
        saved_resume,
        job_url,
        overrides,
        account_fields(account),
    )
    safe_name = html.escape(saved_resume.name)
    safe_url = html.escape(job_url)
    return _page(
        f"""
        <section class="panel">
          <h1>Agent started</h1>
          <p>The resume was uploaded and the agent is opening the job page.</p>
          <dl>
            <dt>Resume</dt>
            <dd>{safe_name}</dd>
            <dt>Job URL</dt>
            <dd>{safe_url}</dd>
          </dl>
          <p>The browser will stay open for review and manual submit.</p>
          <a class="button-link" href="/">Start another</a>
        </section>
        """
    )


@app.post("/save-profile-and-run", response_class=HTMLResponse)
async def save_profile_and_run(
    request: Request,
    background_tasks: BackgroundTasks,
) -> str:
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    resume_name = Path(str(form.get("resume_name", ""))).name
    job_url = str(form.get("job_url", ""))
    resume_path = settings.upload_dir / resume_name
    if not email or not resume_path.exists() or urlparse(job_url).scheme not in {"http", "https"}:
        return _page(
            """
            <section class="panel">
              <h1>Profile could not be saved</h1>
              <p>The saved resume, account email, or job link was missing.</p>
              <a class="button-link" href="/">Start again</a>
            </section>
            """
        )
    profile_keys = {
        "desired_start_date",
        "work_authorization",
        "visa_sponsorship",
        "desired_salary",
        "application_questions_notes",
        "ethnicity",
        "gender",
        "veteran_status",
        "disability_status",
        "self_identify_gender",
        "pronouns",
    }
    profile_data = {
        key: str(form.get(key, ""))
        for key in profile_keys
    }
    saved_account = save_account_fields(settings, email, profile_data)
    overrides = {
        "phone": str(form.get("phone", "")),
        "postal_code": str(form.get("postal_code", "")),
        "address_line1": str(form.get("address_line1", "")),
        "city": str(form.get("city", "")),
        "state": str(form.get("state", "")),
    }
    background_tasks.add_task(
        _run_background_agent,
        resume_path,
        job_url,
        overrides,
        account_fields(saved_account),
    )
    return _page(
        f"""
        <section class="panel">
          <h1>Profile saved</h1>
          <p>Job_ai2 saved the account details for {html.escape(email)} and started the agent.</p>
          <p>The browser will stay open for review and manual submit.</p>
          <a class="button-link" href="/">Start another</a>
        </section>
        """
    )


async def _save_upload(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "resume").suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise ValueError("Resume must be PDF or DOCX.")
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(upload.filename or "resume").stem)
    target = settings.upload_dir / f"{safe_stem}{suffix}"
    content = await upload.read()
    target.write_bytes(content)
    return target


async def _run_background_agent(
    resume_path: Path,
    job_url: str,
    overrides: dict[str, str],
    account_profile: dict[str, str] | None = None,
) -> None:
    agent = JobApplicationAgent(settings)
    try:
        await agent.run(
            resume_path=resume_path,
            job_url=job_url,
            overrides=overrides,
            account_profile=account_profile,
        )
    except Exception as exc:
        error_path = settings.review_dir / "latest_error.txt"
        error_path.write_text(str(exc), encoding="utf-8")


def _profile_page(
    email: str,
    resume_name: str,
    job_url: str,
    overrides: dict[str, str],
    fields: dict[str, str],
) -> str:
    return _page(
        f"""
        <section class="panel wide-panel">
          <h1>Account details</h1>
          <p class="muted">These answers are saved for {html.escape(email)} and reused for Application Questions, Voluntary Disclosures, and Self Identify.</p>
          <form action="/save-profile-and-run" method="post">
            <input type="hidden" name="email" value="{html.escape(email)}">
            <input type="hidden" name="resume_name" value="{html.escape(resume_name)}">
            <input type="hidden" name="job_url" value="{html.escape(job_url)}">
            {_hidden_inputs(overrides)}
            <fieldset>
              <legend>Application Questions</legend>
              <div class="grid-two">
                {_text_input("desired_start_date", "Desired start date", fields, "MM/DD/YYYY")}
                {_select("work_authorization", "Authorized to work in the U.S.", fields, ["Yes", "No"])}
              </div>
              <div class="grid-two">
                {_select("visa_sponsorship", "Require visa sponsorship", fields, ["No", "Yes"])}
                {_text_input("desired_salary", "Desired annual salary", fields, "$140000")}
              </div>
              <label>
                Additional application question answers
                <textarea name="application_questions_notes" rows="4" placeholder="Example: relocation preferences, availability, security clearance, travel preference">{html.escape(fields.get("application_questions_notes", ""))}</textarea>
              </label>
            </fieldset>
            <fieldset>
              <legend>Voluntary Disclosures</legend>
              <div class="grid-two">
                {_select("ethnicity", "Ethnicity", fields, ["I do not wish to answer", "Not Hispanic or Latino", "Hispanic or Latino"])}
                {_select("gender", "Gender", fields, ["I do not wish to answer", "Male", "Female", "Non-binary"])}
              </div>
              <div class="grid-two">
                {_select("veteran_status", "Veteran status", fields, ["I do not wish to answer", "I am not a protected veteran", "I identify as one or more classifications of protected veteran"])}
                {_select("disability_status", "Disability status", fields, ["I do not want to answer", "No, I do not have a disability and have not had one in the past", "Yes, I have a disability, or have had one in the past"])}
              </div>
            </fieldset>
            <fieldset>
              <legend>Self Identify</legend>
              <div class="grid-two">
                {_select("self_identify_gender", "Self-identified gender", fields, ["I do not wish to answer", "Male", "Female", "Non-binary"])}
                {_text_input("pronouns", "Pronouns", fields, "he/him")}
              </div>
            </fieldset>
            <button type="submit">Save and start agent</button>
          </form>
        </section>
        """
    )


def _hidden_inputs(values: dict[str, str]) -> str:
    return "\n".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">'
        for key, value in values.items()
    )


def _text_input(name: str, label: str, fields: dict[str, str], placeholder: str = "") -> str:
    return f"""
    <label>
      {html.escape(label)}
      <input type="text" name="{html.escape(name)}" value="{html.escape(fields.get(name, ""))}" placeholder="{html.escape(placeholder)}">
    </label>
    """


def _select(name: str, label: str, fields: dict[str, str], options: list[str]) -> str:
    current = fields.get(name, "")
    option_html = "\n".join(
        f'<option value="{html.escape(option)}"{" selected" if option == current else ""}>{html.escape(option)}</option>'
        for option in options
    )
    return f"""
    <label>
      {html.escape(label)}
      <select name="{html.escape(name)}">{option_html}</select>
    </label>
    """


def _page(body: str) -> str:
    return f"""
    <!doctype html>
    <!-- File: generated HTML response from /Users/victorbui/AI/Job_ai2/job_ai2_agent/web_app.py -->
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Job_ai2</title>
        <link rel="stylesheet" href="/static/style.css">
      </head>
      <body>
        <main>{body}</main>
      </body>
    </html>
    """
