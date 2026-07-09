<!-- File: /Users/victorbui/AI/Job_ai2/README.md -->
# Job_ai2

Simple agentic AI job application filler.

When you run the local app, it gives you:

- a resume upload box for PDF or DOCX
- a job application webpage link field
- optional phone/address fields for Workday required fields missing from the resume
- a first-run account profile page for Application Questions, Voluntary Disclosures, and Self Identify answers
- a button to start the agent

The account profile is created automatically from the email found in the first uploaded resume. Job_ai2 saves those account-specific answers locally in `artifacts/accounts/`, reuses them on future applications for the same email, fills the application form with Playwright, and leaves submission to you.

Default Python interpreter:

```bash
/Users/victorbui/venvs/ai312/bin/python
```

## Setup

Do this only when you are ready to run it:

```bash
cd /Users/victorbui/AI/Job_ai2
/Users/victorbui/venvs/ai312/bin/python -m pip install -r requirements.txt
/Users/victorbui/venvs/ai312/bin/python -m playwright install chromium
cp .env.example .env
```

Then add your OpenAI key to `.env`.

For local Ollama/Mistral mapping, keep these values in `.env` and make sure Ollama is running:

```bash
LOCAL_LLM_PROVIDER=ollama
LOCAL_LLM_BASE_URL=http://127.0.0.1:11434
LOCAL_LLM_MODEL=mistral
LOCAL_LLM_TIMEOUT_SECONDS=60
```

If Ollama is unavailable or returns unusable JSON, Job_ai2 falls back to OpenAI when configured, then to built-in heuristics.

## Run

Wait until you want to start it, then run:

```bash
cd /Users/victorbui/AI/Job_ai2
/Users/victorbui/venvs/ai312/bin/python run_app.py
```

Open:

```text
http://127.0.0.1:8022
```

## Safety

- The agent does not click submit.
- The browser opens visibly by default.
- Unknown fields are skipped instead of invented.
- Every run writes a JSON review file in `artifacts/reviews/`.
- Saved account profiles stay local under `artifacts/accounts/`.
- The browser remains open for review for `BROWSER_HOLD_SECONDS`.
