# job_ai2_agent/web_app.py

import html
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from job_ai2_agent.account_store import (
    account_fields,
    ensure_account_profile,
    load_account_profile,
    save_account_fields,
)
from job_ai2_agent.config import PROJECT_ROOT, ensure_artifact_dirs, load_settings
from job_ai2_agent.models import EducationItem, WorkExperience
from job_ai2_agent.resume_reader import read_resume_profile
from job_ai2_agent.service import JobApplicationAgent


settings = load_settings()
ensure_artifact_dirs(settings)

# Fields saved as reusable profile answers for job-specific questions and
# voluntary disclosure pages. These are not parsed reliably from resumes, so the
# user can correct them once and reuse them across applications.
PROFILE_KEYS = {
    "desired_start_date",
    "work_authorization",
    "visa_sponsorship",
    "desired_salary",
    "application_questions_notes",
    "ethnicity",
    "race",
    "gender",
    "veteran_status",
    "disability_status",
    "self_identify_gender",
    "pronouns",
    "sms_consent",
}

# Fields shown on the pre-run review page and sent into the browser agent for
# the current application. Some come from resume parsing and some come from the
# saved account profile; the review page lets the user correct either source.
APPLICATION_KEYS = {
    "legal_name",
    "full_name",
    "first_name",
    "last_name",
    "preferred_name",
    "email",
    "phone",
    "postal_code",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "country",
    "current_job_title",
    "current_company",
    "current_job_location",
    "current_job_start_month",
    "current_job_start_year",
}

# Personal identity and contact fields that should persist after the review
# step. Current job and resume-specific work history stay per-resume, because
# different saved resumes may intentionally describe different experience.
SAVED_APPLICATION_KEYS = {
    "legal_name",
    "full_name",
    "first_name",
    "last_name",
    "preferred_name",
    "email",
    "phone",
    "postal_code",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "country",
}

WORK_EXPERIENCE_LIMIT = 6
EDUCATION_LIMIT = 4

app = FastAPI(title="Job_ai2")
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "static"), name="static")


# Main launch page. It stays intentionally small: choose or upload a resume,
# paste the job application URL, and then move into the review buffer before any
# browser automation starts.
@app.get("/", response_class=HTMLResponse)
async def home():
    # The remembered resume is preselected so repeat applications can start
    # without uploading the same file again.
    last_resume = _remembered_resume()
    saved_resumes = _saved_resumes()
    recent_job_urls = _recent_job_urls()
    resume_input_required = "" if saved_resumes else " required"
    return _page(
        f"""
        <section class="home-shell">
          <nav class="home-auth-actions" aria-label="Account">
            <a class="button-link secondary-button" href="/sign-in">Sign in</a>
            <a class="button-link" href="/register">Register</a>
          </nav>
          <section class="panel launch-panel">
            <form action="/run-agent" method="post" enctype="multipart/form-data">
              <div class="launch-grid">
                <!-- Launch inputs: only the resume source and job URL belong here. -->
                <div>
                  <h1>Job_ai2</h1>
                  <!-- Resume upload is optional when a saved resume is selected. -->
                  <div class="field-group">
                    <label for="resume-upload">Upload new resume</label>
                    <input id="resume-upload" type="file" name="resume" accept=".pdf,.docx"{resume_input_required}>
                  </div>
                  <!-- Job URL is validated server-side before parsing or automation. -->
                  <label>
                    Job application link
                    <input type="url" name="job_url" list="recent-job-links" placeholder="https://company.com/apply" required>
                    {_job_url_history_html(recent_job_urls)}
                  </label>
                  <!-- User reviews parsed/profile data before this starts the agent. -->
                  <div class="panel-actions">
                    <button type="submit">Review application info</button>
                    <a class="button-link secondary-button" href="/profile">Edit saved profile</a>
                  </div>
                </div>
                <!-- Saved resumes stay on the right so users can choose the best resume per job. -->
                <aside class="resume-sidebar">
                  <h2>Saved resumes</h2>
                  {_resume_picker_html(saved_resumes, last_resume)}
                </aside>
              </div>
            </form>
          </section>
        </section>
        """
    )


# Build the saved-resume radio list. The value sent back to the server is only
# the basename, and _uploaded_resume_path resolves it inside artifacts/uploads
# so a crafted form cannot point outside the upload directory.
def _resume_picker_html(resumes, selected_resume):
    if not resumes:
        return '<p class="empty-note">No saved resumes yet.</p>'
    selected_name = selected_resume.name if selected_resume else resumes[0].name
    items = []
    for resume_path in resumes:
        checked = " checked" if resume_path.name == selected_name else ""
        safe_name = html.escape(resume_path.name)
        items.append(
            f"""
            <div class="resume-option">
              <label class="resume-choice">
                <input type="radio" name="saved_resume_name" value="{safe_name}"{checked}>
                <span>
                  <strong>{safe_name}</strong>
                  <small>{html.escape(_resume_meta(resume_path))}</small>
                </span>
              </label>
              <button
                class="resume-delete-button"
                type="submit"
                formaction="/delete-resume"
                formmethod="post"
                formnovalidate
                name="delete_resume_name"
                value="{safe_name}"
                aria-label="Remove {safe_name}"
                title="Remove resume"
                onclick="return confirm('Remove this saved resume?');"
              >x</button>
            </div>
            """
        )
    return "\n".join(items)


# Short metadata displayed under each saved resume. It helps distinguish PDF
# and DOCX variants without showing long filesystem paths.
def _resume_meta(resume_path):
    size_kb = max(1, round(resume_path.stat().st_size / 1024))
    return f"{resume_path.suffix.upper().lstrip('.')} | {size_kb} KB"


# Pre-automation review buffer. This page merges parsed resume data, saved
# account profile answers, and any current form values so the user can correct
# bad parsing before the browser fills Workday.
def _application_buffer_page(email, resume_path, job_url, fields, work_experiences, education_items):
    safe_resume_name = html.escape(resume_path.name)
    safe_job_url = html.escape(job_url)
    return _page(
        f"""
        <section class="panel review-panel">
          <h1>Review application info</h1>
          <p class="muted">Correct parsed values and saved profile answers before Job_ai2 opens the application. Saved account: {html.escape(email)}.</p>
          <dl>
            <dt>Resume</dt>
            <dd>{safe_resume_name}</dd>
            <dt>Job URL</dt>
            <dd>{safe_job_url}</dd>
          </dl>
          <form id="application-review-form" action="/save-profile-and-run" method="post">
            <!-- Hidden routing data tells the save/run route which resume and job to use. -->
            <input type="hidden" name="resume_name" value="{safe_resume_name}">
            <input type="hidden" name="job_url" value="{safe_job_url}">
            <div class="review-grid">
              <!-- Left column: identity/contact/profile answers reused across applications. -->
              <div class="profile-review-column">
                {_identity_contact_fields_html(fields)}
                {_profile_fields_html(fields)}
              </div>
              <!-- Right column: resume-derived sections that may differ by selected resume. -->
              <div class="resume-review-column">
                {_work_experience_fields_html(work_experiences)}
                {_education_fields_html(education_items)}
              </div>
            </div>
            <div class="panel-actions">
              <button type="submit">Save profile and start agent</button>
              <a class="button-link secondary-button" href="/">Back</a>
            </div>
            <p id="autosave-status" class="autosave-status" aria-live="polite">Autosave ready.</p>
          </form>
        </section>
        <script>
          (() => {{
            const form = document.getElementById("application-review-form");
            const status = document.getElementById("autosave-status");
            if (!form || !status) return;
            let dirty = false;
            let saving = false;

            const markDirty = () => {{
              dirty = true;
              status.textContent = "Unsaved changes.";
            }};

            const autosave = async () => {{
              if (!dirty || saving) return;
              saving = true;
              status.textContent = "Autosaving...";
              try {{
                const response = await fetch("/autosave-application-profile", {{
                  method: "POST",
                  body: new FormData(form),
                  headers: {{
                    "Accept": "application/json",
                  }},
                }});
                if (!response.ok) throw new Error("Autosave failed");
                dirty = false;
                const savedAt = new Date().toLocaleTimeString([], {{
                  hour: "numeric",
                  minute: "2-digit",
                }});
                status.textContent = `Autosaved at ${{savedAt}}.`;
              }} catch (error) {{
                status.textContent = "Autosave failed. Changes are still on this page.";
              }} finally {{
                saving = false;
              }}
            }};

            form.addEventListener("input", markDirty);
            form.addEventListener("change", markDirty);
            form.addEventListener("submit", () => {{
              dirty = false;
            }});
            window.setInterval(autosave, 30000);
          }})();
        </script>
        """
    )


@app.get("/sign-in", response_class=HTMLResponse)
async def sign_in():
    return _account_entry_page(
        title="Sign in",
        action="/sign-in",
        submit_label="Sign in",
        intro="Open an existing local account profile by email.",
        alternate_href="/register",
        alternate_label="Register instead",
    )


@app.post("/sign-in", response_class=HTMLResponse)
async def submit_sign_in(request: Request):
    form = await request.form()
    email = _account_form_email(form)
    if not _valid_email(email):
        return _account_entry_page(
            title="Sign in",
            action="/sign-in",
            submit_label="Sign in",
            intro="Open an existing local account profile by email.",
            alternate_href="/register",
            alternate_label="Register instead",
            error="Enter a valid email address.",
            email=email,
        )
    account = load_account_profile(settings, email)
    if not account:
        return _account_entry_page(
            title="Sign in",
            action="/sign-in",
            submit_label="Sign in",
            intro="Open an existing local account profile by email.",
            alternate_href="/register",
            alternate_label="Register instead",
            error="No saved account was found for that email. Register to create one.",
            email=email,
        )
    return _account_profile_editor(
        email=email,
        fields=account_fields(account),
        intro="You are signed in to this local profile.",
    )


@app.get("/register", response_class=HTMLResponse)
async def register():
    return _account_entry_page(
        title="Register",
        action="/register",
        submit_label="Create account",
        intro="Create a local saved profile for job applications.",
        alternate_href="/sign-in",
        alternate_label="Sign in instead",
        include_name=True,
    )


@app.post("/register", response_class=HTMLResponse)
async def submit_register(request: Request):
    form = await request.form()
    email = _account_form_email(form)
    full_name = str(form.get("full_name", "")).strip()
    if not _valid_email(email):
        return _account_entry_page(
            title="Register",
            action="/register",
            submit_label="Create account",
            intro="Create a local saved profile for job applications.",
            alternate_href="/sign-in",
            alternate_label="Sign in instead",
            include_name=True,
            error="Enter a valid email address.",
            email=email,
            full_name=full_name,
        )
    existing_account = load_account_profile(settings, email)
    account = ensure_account_profile(settings, email)
    fields = account_fields(account)
    if full_name and not existing_account:
        fields = dict(fields)
        fields.update(
            {
                "email": email,
                "full_name": full_name,
                "legal_name": full_name,
            }
        )
        account = save_account_fields(settings, email, fields)
        fields = account_fields(account)
    intro = "That account already exists, so Job_ai2 opened it for editing."
    if not existing_account:
        intro = "Your local account was created. Add any reusable profile details below."
    return _account_profile_editor(
        email=email,
        fields=fields,
        intro=intro,
    )


# Standalone editor for saved account profile fields. It uses the remembered
# resume only to identify the account email, then loads editable saved answers
# from artifacts/accounts/.
@app.get("/profile", response_class=HTMLResponse)
async def edit_saved_profile():
    last_resume = _remembered_resume()
    if not last_resume:
        return _page(
            """
            <section class="panel">
              <h1>No saved resume</h1>
              <p>Upload a resume once before editing saved profile answers.</p>
              <a class="button-link" href="/">Back</a>
            </section>
            """
        )
    resume_profile = read_resume_profile(last_resume)
    email = resume_profile.fields.get("email", "").strip().lower()
    if not email:
        return _page(
            """
            <section class="panel">
              <h1>Email not found</h1>
              <p>The remembered resume needs an email address before Job_ai2 can edit its saved profile.</p>
              <a class="button-link" href="/">Back</a>
            </section>
            """
        )
    account = ensure_account_profile(settings, email)
    saved_fields = account_fields(account)
    profile_fields = _merge_application_fields(
        resume_profile.fields,
        saved_fields,
        {},
    )
    return _profile_page(
        email=email,
        fields=profile_fields,
        work_experiences=_draft_work_experiences(saved_fields, resume_profile.work_experiences),
        education_items=_draft_education_items(saved_fields, resume_profile.education_items),
        action="/save-profile",
        submit_label="Save profile",
        hidden_html=f'<input type="hidden" name="email" value="{html.escape(email)}">',
        intro="Update saved answers reused for job applications, including personal data, work history, education, application questions, and disclosures.",
        back_link=True,
    )


# First POST from the launch page. This does not run the browser agent yet; it
# saves a newly uploaded resume if present, parses the selected resume, creates
# the account profile if needed, then renders the correction buffer.
@app.post("/run-agent", response_class=HTMLResponse)
async def run_agent(
    resume: UploadFile = File(None),
    saved_resume_name: str = Form(""),
    job_url: str = Form(...),
    phone: str = Form(""),
    postal_code: str = Form(""),
    address_line1: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
):
    # These optional contact fields are kept for backward compatibility with
    # older forms. The current main page collects contact information only in
    # the review/profile screens.
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
    _remember_job_url(job_url)
    try:
        saved_resume = await _resolve_resume(resume, saved_resume_name)
    except ValueError as exc:
        return _page(
            f"""
            <section class="panel">
              <h1>Resume needed</h1>
              <p>{html.escape(str(exc))}</p>
              <a class="button-link" href="/">Try again</a>
            </section>
            """
        )
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
    saved_fields = account_fields(account)
    overrides = {
        "phone": phone,
        "postal_code": postal_code,
        "address_line1": address_line1,
        "city": city,
        "state": state,
    }
    buffer_fields = _merge_application_fields(
        resume_profile.fields,
        saved_fields,
        overrides,
    )
    draft = _load_application_draft(email, saved_resume.name, job_url)
    if draft:
        draft_fields = _draft_fields(draft)
        buffer_fields = _merge_application_fields(buffer_fields, draft_fields, {})
        work_experiences = _draft_work_experiences(draft_fields, resume_profile.work_experiences)
        education_items = _draft_education_items(draft_fields, resume_profile.education_items)
        buffer_fields = _merge_application_fields(buffer_fields, saved_fields, overrides)
    else:
        work_experiences = resume_profile.work_experiences
        education_items = _draft_education_items(saved_fields, resume_profile.education_items)
    work_experiences = _saved_profile_work_experiences(saved_fields, work_experiences)
    education_items = _draft_education_items(saved_fields, education_items)
    return _application_buffer_page(
        email=email,
        resume_path=saved_resume,
        job_url=job_url,
        fields=buffer_fields,
        work_experiences=work_experiences,
        education_items=education_items,
    )


# Delete route for the saved-resume X button. It only removes files that resolve
# inside artifacts/uploads through _uploaded_resume_path, then returns to the
# launch page with the resume picker refreshed.
@app.post("/delete-resume")
async def delete_resume(delete_resume_name: str = Form(...)):
    resume_path = _uploaded_resume_path(delete_resume_name)
    if resume_path:
        resume_path.unlink()
        _forget_resume(resume_path.name)
    return RedirectResponse("/", status_code=303)


# Autosave route used by the review buffer. The browser calls this every 30
# seconds only after the user edits a field. It saves stable profile fields to
# the account profile and stores the full indexed form as a draft for this
# email/resume/job combination.
@app.post("/autosave-application-profile")
async def autosave_application_profile(request: Request):
    form = await request.form()
    email = _form_email(form)
    resume_name = Path(str(form.get("resume_name", ""))).name
    job_url = str(form.get("job_url", ""))
    if not email or not resume_name or urlparse(job_url).scheme not in {"http", "https"}:
        return JSONResponse(
            {"ok": False, "message": "Missing account email, resume, or job URL."},
            status_code=400,
        )
    profile_data = _profile_form_fields(form)
    save_account_fields(settings, email, profile_data)
    _save_application_draft(email, resume_name, job_url, form)
    return JSONResponse({"ok": True, "saved_at": datetime.now(UTC).isoformat()})


# Final POST from the review buffer. It persists reusable profile values, keeps
# resume-specific work/education corrections in overrides, and launches the
# browser automation in the background.
@app.post("/save-profile-and-run", response_class=HTMLResponse)
async def save_profile_and_run(
    request: Request,
    background_tasks: BackgroundTasks,
):
    form = await request.form()
    email = _form_email(form)
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
    profile_data = _profile_form_fields(form)
    saved_account = save_account_fields(settings, email, profile_data)
    _save_application_draft(email, resume_name, job_url, form)
    # APPLICATION_KEYS are per-run overrides used by the browser agent. The
    # structured work/education fields are added below because each row has an
    # index in the HTML field names.
    overrides = {
        key: str(form.get(key, ""))
        for key in APPLICATION_KEYS
    }
    overrides.update(_structured_override_fields(form))
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
          <p>The browser will remain open for any human-verification step and your final review. Job_ai2 will not click Submit.</p>
          <a class="button-link" href="/">Start another</a>
        </section>
        """
    )


# Save-only route used by the Edit saved profile page. Unlike the review route,
# this does not launch browser automation.
@app.post("/save-profile", response_class=HTMLResponse)
async def save_profile(request: Request):
    form = await request.form()
    email_values = [
        str(value).strip().lower()
        for value in form.getlist("email")
        if str(value).strip()
    ]
    email = email_values[-1] if email_values else ""
    if not email:
        return _page(
            """
            <section class="panel">
              <h1>Profile could not be saved</h1>
              <p>The account email was missing.</p>
              <a class="button-link" href="/profile">Try again</a>
            </section>
            """
        )
    profile_data = _profile_form_fields(form)
    save_account_fields(settings, email, profile_data)
    return _page(
        f"""
        <section class="panel">
          <h1>Profile saved</h1>
          <p>Job_ai2 updated saved profile answers for {html.escape(email)}.</p>
          <div class="panel-actions">
            <a class="button-link" href="/">Back to agent</a>
            <a class="button-link secondary-button" href="/profile">Edit again</a>
          </div>
        </section>
        """
    )


# Resolve the resume source. A fresh upload wins over a previously saved
# selection, which lets the user replace the resume for a specific application.
async def _resolve_resume(upload, saved_resume_name):
    if upload and upload.filename:
        return await _save_upload(upload)
    saved_resume_path = _uploaded_resume_path(saved_resume_name)
    if saved_resume_path:
        return saved_resume_path
    raise ValueError("Upload a PDF or DOCX resume before starting the agent.")


# Store uploaded resumes in artifacts/uploads using a conservative filename.
# This keeps the original extension for parser routing while stripping path
# separators and unusual characters from the browser-provided filename.
async def _save_upload(upload):
    suffix = Path(upload.filename or "resume").suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise ValueError("Resume must be PDF or DOCX.")
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(upload.filename or "resume").stem)
    target = settings.upload_dir / f"{safe_stem}{suffix}"
    content = await upload.read()
    target.write_bytes(content)
    _remember_resume(target)
    return target


# Background task wrapper for the browser agent. latest_error.txt is cleared at
# the start so old failures do not make a successful new run look broken.
async def _run_background_agent(
    resume_path,
    job_url,
    overrides,
    account_profile=None,
):
    agent = JobApplicationAgent(settings)
    latest_error = settings.review_dir / "latest_error.txt"
    if latest_error.exists():
        latest_error.unlink()
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


# Generic saved-profile page wrapper. Individual field sections are composed by
# helpers below so the review page and edit page stay consistent.
def _profile_page(
    email,
    fields,
    work_experiences,
    education_items,
    action,
    submit_label,
    hidden_html,
    intro,
    back_link=False,
):
    back_link_html = '<a class="button-link secondary-button" href="/">Back</a>' if back_link else ""
    return _page(
        f"""
        <section class="panel wide-panel">
          <h1>Account details</h1>
          <p class="muted">{html.escape(intro)} Saved account: {html.escape(email)}.</p>
          <form action="{html.escape(action)}" method="post">
            {hidden_html}
            {_identity_contact_fields_html(fields)}
            {_profile_fields_html(fields)}
            <section class="profile-section">
              <h2>Work Experience</h2>
              {_work_experience_fields_html(work_experiences)}
            </section>
            <section class="profile-section">
              <h2>Education</h2>
              {_education_fields_html(education_items)}
            </section>
            <div class="panel-actions">
              <button type="submit">{html.escape(submit_label)}</button>
              {back_link_html}
            </div>
          </form>
        </section>
        """
    )


def _account_entry_page(
    title,
    action,
    submit_label,
    intro,
    alternate_href,
    alternate_label,
    include_name=False,
    error="",
    email="",
    full_name="",
):
    error_html = ""
    if error:
        error_html = f'<p class="status-message">{html.escape(error)}</p>'
    name_html = ""
    if include_name:
        name_html = f"""
        <label>
          Full name
          <input type="text" name="full_name" value="{html.escape(full_name)}" placeholder="Jane Applicant">
        </label>
        """
    return _page(
        f"""
        <section class="panel auth-panel">
          <h1>{html.escape(title)}</h1>
          <p class="muted">{html.escape(intro)}</p>
          {error_html}
          <form action="{html.escape(action)}" method="post">
            {name_html}
            <label>
              Email
              <input type="email" name="email" value="{html.escape(email)}" placeholder="you@example.com" required>
            </label>
            <div class="panel-actions">
              <button type="submit">{html.escape(submit_label)}</button>
              <a class="button-link secondary-button" href="{html.escape(alternate_href)}">{html.escape(alternate_label)}</a>
              <a class="button-link secondary-button" href="/">Back</a>
            </div>
          </form>
        </section>
        """
    )


def _account_profile_editor(email, fields, intro):
    profile_fields = dict(fields)
    profile_fields.setdefault("email", email)
    return _profile_page(
        email=email,
        fields=profile_fields,
        work_experiences=_draft_work_experiences(profile_fields, []),
        education_items=_draft_education_items(profile_fields, []),
        action="/save-profile",
        submit_label="Save profile",
        hidden_html=f'<input type="hidden" name="email" value="{html.escape(email)}">',
        intro=intro,
        back_link=True,
    )


def _account_form_email(form):
    return str(form.get("email", "")).strip().lower()


def _valid_email(email):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email))


# Identity and contact fields map to Workday's My Information page. These are
# saved after review because they are stable user profile data, not job-specific
# resume history.
def _identity_contact_fields_html(fields):
    return f"""
    <fieldset>
      <legend>My Information</legend>
      <div class="grid-two">
        {_text_input("legal_name", "Legal name", fields)}
        {_text_input("full_name", "Full name", fields)}
      </div>
      <div class="grid-two">
        {_text_input("first_name", "First name", fields)}
        {_text_input("last_name", "Last name", fields)}
      </div>
      <div class="grid-two">
        {_text_input("preferred_name", "Preferred name", fields)}
        {_text_input("email", "Email", fields)}
      </div>
    </fieldset>
    <fieldset>
      <legend>Contact</legend>
      <div class="grid-two">
        {_text_input("phone", "Phone", fields, "5551234567")}
        {_text_input("postal_code", "Postal code", fields, "48104")}
      </div>
      {_text_input("address_line1", "Address line 1", fields, "Street address")}
      {_text_input("address_line2", "Address line 2", fields)}
      <div class="grid-two">
        {_text_input("city", "City", fields, "Ann Arbor")}
        {_text_input("state", "State", fields, "MI")}
      </div>
      {_text_input("country", "Country", fields)}
    </fieldset>
    """


# Profile fields cover Workday Application Questions plus voluntary disclosure
# and self-identification sections. Defaults are intentionally conservative and
# editable before each automated run.
def _profile_fields_html(fields):
    return f"""
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
        {_select("race", "Race", fields, ["I do not wish to answer", "Asian", "Black or African American", "White", "American Indian or Alaska Native", "Native Hawaiian or Other Pacific Islander", "Two or more races"])}
        {_select("ethnicity", "Hispanic/Latino status", fields, ["I do not wish to answer", "Not Hispanic or Latino", "Hispanic or Latino"])}
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
      {_select("sms_consent", "Consent to recruiting text messages", fields, ["No", "Yes"])}
    </fieldset>
    """


# Render editable work-history rows parsed from the selected resume. Empty rows
# are included so the user can add missing jobs before automation starts.
def _work_experience_fields_html(experiences):
    rows = []
    padded = list(experiences[:WORK_EXPERIENCE_LIMIT])
    while len(padded) < WORK_EXPERIENCE_LIMIT:
        padded.append(None)
    for index, experience in enumerate(padded):
        values = {
            f"work_{index}_title": experience.title if experience else "",
            f"work_{index}_company": experience.company if experience else "",
            f"work_{index}_location": experience.location if experience else "",
            f"work_{index}_start_month": experience.start_month if experience else "",
            f"work_{index}_start_year": experience.start_year if experience else "",
            f"work_{index}_end_month": experience.end_month if experience else "",
            f"work_{index}_end_year": experience.end_year if experience else "",
            f"work_{index}_description": experience.description if experience else "",
            f"work_{index}_currently_work_here": "Yes" if experience and experience.currently_work_here else "No",
        }
        rows.append(
            f"""
            <fieldset class="nested-fieldset">
              <legend>Work Experience {index + 1}</legend>
              {_text_input(f"work_{index}_title", "Job title", values)}
              {_text_input(f"work_{index}_company", "Company", values)}
              {_text_input(f"work_{index}_location", "Location", values)}
              <div class="grid-two">
                {_text_input(f"work_{index}_start_month", "Start month", values)}
                {_text_input(f"work_{index}_start_year", "Start year", values)}
              </div>
              <div class="grid-two">
                {_text_input(f"work_{index}_end_month", "End month", values)}
                {_text_input(f"work_{index}_end_year", "End year", values)}
              </div>
              {_select(f"work_{index}_currently_work_here", "Currently work here", values, ["No", "Yes"])}
              <label>
                Role description
                <textarea name="work_{index}_description" rows="3">{html.escape(values.get(f"work_{index}_description", ""))}</textarea>
              </label>
            </fieldset>
            """
        )
    return "\n".join(rows)


# Render editable education rows parsed from the selected resume. Workday only
# needs a few rows in practice, so the UI limits the number to keep review usable.
def _education_fields_html(education_items):
    rows = []
    padded = list(education_items[:EDUCATION_LIMIT])
    while len(padded) < EDUCATION_LIMIT:
        padded.append(None)
    for index, education in enumerate(padded):
        values = {
            f"education_{index}_school": education.school if education else "",
            f"education_{index}_degree": education.degree if education else "",
            f"education_{index}_field": education.field_of_study if education else "",
            f"education_{index}_end_year": education.end_year if education else "",
        }
        rows.append(
            f"""
            <fieldset class="nested-fieldset">
              <legend>Education {index + 1}</legend>
              {_text_input(f"education_{index}_school", "School", values)}
              <div class="grid-two">
                {_text_input(f"education_{index}_degree", "Degree", values)}
                {_text_input(f"education_{index}_field", "Field of study", values)}
              </div>
              {_text_input(f"education_{index}_end_year", "End year", values)}
            </fieldset>
            """
        )
    return "\n".join(rows)


def _hidden_inputs(values):
    return "\n".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">'
        for key, value in values.items()
    )


# Prefer the last submitted email value. Some forms include both hidden routing
# data and an editable visible email field, so the visible correction should win.
def _form_email(form):
    email_values = [
        str(value).strip().lower()
        for value in form.getlist("email")
        if str(value).strip()
    ]
    return email_values[-1] if email_values else ""


# Account profile extraction used by save-profile, autosave, and save/run. It
# stores stable personal/application fields plus work and education rows, making
# /profile a reusable master profile rather than only a disclosure editor.
def _profile_form_fields(form):
    fields = {
        key: str(form.get(key, ""))
        for key in PROFILE_KEYS | SAVED_APPLICATION_KEYS
    }
    fields.update(_structured_override_fields(form))
    return fields


# Merge values for the review buffer in priority order:
# 1. resume parser output gives the first draft,
# 2. saved account fields correct stable personal/profile data,
# 3. current form overrides win when an older client still submits them.
def _merge_application_fields(resume_fields, saved_fields, overrides):
    merged = dict(resume_fields)
    for key, value in saved_fields.items():
        if value.strip():
            merged[key] = value.strip()
    for key, value in overrides.items():
        if value.strip():
            merged[key] = value.strip()
    if merged.get("legal_name") and not merged.get("full_name"):
        merged["full_name"] = merged["legal_name"]
    if merged.get("full_name") and not merged.get("legal_name"):
        merged["legal_name"] = merged["full_name"]
    return merged


# Convert repeated Work Experience and Education form rows back into a flat
# override dictionary. service.py rebuilds structured dataclasses from these
# indexed keys before the browser agent starts.
def _structured_override_fields(form):
    values = {}
    for index in range(WORK_EXPERIENCE_LIMIT):
        for field in [
            "title",
            "company",
            "location",
            "start_month",
            "start_year",
            "end_month",
            "end_year",
            "description",
            "currently_work_here",
        ]:
            key = f"work_{index}_{field}"
            values[key] = str(form.get(key, ""))
    for index in range(EDUCATION_LIMIT):
        for field in ["school", "degree", "field", "end_year"]:
            key = f"education_{index}_{field}"
            values[key] = str(form.get(key, ""))
    return values


# Extract saved draft fields from disk data. Draft files intentionally store the
# raw flattened form shape so future UI sections can be restored without a
# migration step.
def _draft_fields(draft):
    fields = draft.get("fields", {})
    if not isinstance(fields, dict):
        return {}
    return {str(key): str(value) for key, value in fields.items()}


# Rebuild work-history rows from autosaved indexed fields. If a draft has no
# work rows, the parser output remains the source of truth.
def _draft_work_experiences(fields, fallback):
    experiences = []
    for index in range(WORK_EXPERIENCE_LIMIT):
        prefix = f"work_{index}_"
        title = fields.get(f"{prefix}title", "").strip()
        company = fields.get(f"{prefix}company", "").strip()
        if not title and not company:
            continue
        experiences.append(
            WorkExperience(
                title=title,
                company=company,
                location=fields.get(f"{prefix}location", "").strip(),
                start_month=fields.get(f"{prefix}start_month", "").strip(),
                start_year=fields.get(f"{prefix}start_year", "").strip(),
                end_month=fields.get(f"{prefix}end_month", "").strip(),
                end_year=fields.get(f"{prefix}end_year", "").strip(),
                currently_work_here=fields.get(f"{prefix}currently_work_here", "").strip().lower() == "yes",
                description=fields.get(f"{prefix}description", "").strip(),
            )
        )
    return experiences or fallback


# Apply the saved master profile to work-history rows while preserving role
# descriptions from the current review source. Role descriptions are resume- or
# job-specific, so the master profile should not overwrite them.
def _saved_profile_work_experiences(saved_fields, current_experiences):
    saved_experiences = _draft_work_experiences(saved_fields, [])
    if not saved_experiences:
        return current_experiences
    for index, experience in enumerate(saved_experiences):
        if index < len(current_experiences):
            experience.description = current_experiences[index].description
        else:
            experience.description = ""
    return saved_experiences


# Rebuild education rows from autosaved indexed fields. Empty draft rows are
# ignored so blank UI space does not erase parser output.
def _draft_education_items(fields, fallback):
    education_items = []
    for index in range(EDUCATION_LIMIT):
        prefix = f"education_{index}_"
        school = fields.get(f"{prefix}school", "").strip()
        degree = fields.get(f"{prefix}degree", "").strip()
        if not school and not degree:
            continue
        education_items.append(
            EducationItem(
                school=school,
                degree=degree,
                field_of_study=fields.get(f"{prefix}field", "").strip(),
                end_year=fields.get(f"{prefix}end_year", "").strip(),
            )
        )
    return education_items or fallback


# Save the entire application review form. The account profile stores stable
# personal data; this draft stores everything on the page, including indexed
# Work Experience and Education rows for the selected resume/job pair.
def _save_application_draft(email, resume_name, job_url, form):
    draft_path = _application_draft_path(email, resume_name, job_url)
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    fields = {
        str(key): str(form.get(key, ""))
        for key in form.keys()
    }
    draft = {
        "email": email,
        "resume_name": resume_name,
        "job_url": job_url,
        "saved_at": datetime.now(UTC).isoformat(),
        "fields": fields,
    }
    draft_path.write_text(json.dumps(draft, indent=2), encoding="utf-8")


# Load the autosaved draft for this exact email/resume/job combination. Drafts
# are optional; missing or corrupt drafts are ignored so the review page can
# still render from the resume parser and saved account profile.
def _load_application_draft(email, resume_name, job_url):
    draft_path = _application_draft_path(email, resume_name, job_url)
    if not draft_path.exists():
        return {}
    try:
        return json.loads(draft_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


# A hashed draft filename keeps URLs and emails out of filenames while making
# the draft deterministic for the same account, resume, and job URL.
def _application_draft_path(email, resume_name, job_url):
    draft_key = "\n".join([email.strip().lower(), Path(resume_name).name, job_url.strip()])
    digest = hashlib.sha256(draft_key.encode("utf-8")).hexdigest()[:20]
    return settings.account_dir / "drafts" / f"{digest}.json"


def _job_url_history_path():
    return settings.account_dir / "job_url_history.json"


def _recent_job_urls():
    history_path = _job_url_history_path()
    history = []
    if history_path.exists():
        try:
            loaded = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                history = [url for url in loaded if isinstance(url, str) and url.strip()]
        except (json.JSONDecodeError, OSError):
            pass
    for job_url in _job_urls_from_drafts():
        if job_url not in history:
            history.append(job_url)
    return history[:30]


def _job_urls_from_drafts():
    draft_dir = settings.account_dir / "drafts"
    saved_urls = []
    for draft_path in draft_dir.glob("*.json") if draft_dir.exists() else []:
        try:
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        job_url = draft.get("job_url", "")
        saved_at = draft.get("saved_at", "")
        if isinstance(job_url, str) and job_url.strip():
            saved_urls.append((str(saved_at), job_url.strip()))
    saved_urls.sort(reverse=True)
    return list(dict.fromkeys(job_url for _saved_at, job_url in saved_urls))


def _remember_job_url(job_url):
    normalized = job_url.strip()
    history = [url for url in _recent_job_urls() if url != normalized]
    history.insert(0, normalized)
    history_path = _job_url_history_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(json.dumps(history[:30], indent=2), encoding="utf-8")


def _job_url_history_html(job_urls):
    options = "\n".join(
        f'<option value="{html.escape(job_url)}"></option>'
        for job_url in job_urls
    )
    return f'<datalist id="recent-job-links">{options}</datalist>'


# Shared text input renderer. All values are escaped because the resume parser
# and user-edited profile data can contain arbitrary text.
def _text_input(name, label, fields, placeholder=""):
    return f"""
    <label>
      {html.escape(label)}
      <input type="text" name="{html.escape(name)}" value="{html.escape(fields.get(name, ""))}" placeholder="{html.escape(placeholder)}">
    </label>
    """


# Shared select renderer. Options are kept explicit in the caller so each
# Workday-like section can control its own wording and defaults.
def _select(name, label, fields, options):
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


# Resolve the last-used resume. If metadata is missing or stale, fall back to
# the newest uploaded PDF/DOCX and rewrite the metadata so the next page load is
# deterministic.
def _remembered_resume():
    metadata_path = _last_resume_metadata_path()
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            metadata = {}
        remembered_path = _uploaded_resume_path(str(metadata.get("filename", "")))
        if remembered_path:
            return remembered_path
    uploaded_resumes = [
        path
        for path in settings.upload_dir.glob("*")
        if path.is_file() and path.suffix.lower() in {".pdf", ".docx"}
    ]
    if not uploaded_resumes:
        return None
    latest_resume = max(uploaded_resumes, key=lambda path: path.stat().st_mtime)
    _remember_resume(latest_resume)
    return latest_resume


# List all uploaded resumes newest-first for the right-side picker.
def _saved_resumes():
    return sorted(
        [
            path
            for path in settings.upload_dir.glob("*")
            if path.is_file() and path.suffix.lower() in {".pdf", ".docx"}
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


# Persist only the uploaded filename, not an absolute path. The actual path is
# rebuilt from settings.upload_dir when the resume is used.
def _remember_resume(resume_path):
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "filename": resume_path.name,
    }
    _last_resume_metadata_path().write_text(json.dumps(metadata, indent=2), encoding="utf-8")


# Update last_resume.json after a file is removed. If the deleted resume was the
# remembered one, promote the newest remaining resume; otherwise keep metadata
# unchanged.
def _forget_resume(filename):
    metadata_path = _last_resume_metadata_path()
    remembered_name = ""
    if metadata_path.exists():
        try:
            remembered_name = str(json.loads(metadata_path.read_text(encoding="utf-8")).get("filename", ""))
        except json.JSONDecodeError:
            remembered_name = ""
    if remembered_name and Path(remembered_name).name != filename:
        return
    remaining_resumes = _saved_resumes()
    if remaining_resumes:
        _remember_resume(remaining_resumes[0])
    elif metadata_path.exists():
        metadata_path.unlink()


# Safely resolve a saved-resume radio value. Path(filename).name strips any
# submitted directory components before checking artifacts/uploads.
def _uploaded_resume_path(filename):
    safe_name = Path(filename or "").name
    if not safe_name:
        return None
    resume_path = settings.upload_dir / safe_name
    if resume_path.exists() and resume_path.suffix.lower() in {".pdf", ".docx"}:
        return resume_path
    return None


# Metadata location for the remembered resume pointer.
def _last_resume_metadata_path():
    return settings.upload_dir / "last_resume.json"


# Shared page shell for all small server-rendered pages in this local app.
# Keeping the stylesheet version here makes cache busting explicit after UI
# changes while avoiding a templating dependency for this small project.
def _page(body):
    return f"""
    <!doctype html>
    <!-- File: generated HTML response from /Users/victorbui/AI/Job_ai2/job_ai2_agent/web_app.py -->
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Job_ai2</title>
        <link rel="stylesheet" href="/static/style.css?v=20260721a">
      </head>
      <body>
        <main>{body}</main>
      </body>
    </html>
    """
