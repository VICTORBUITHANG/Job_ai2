# job_ai2_agent/service.py

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from job_ai2_agent.browser_agent import fill_job_application
from job_ai2_agent.config import Settings
from job_ai2_agent.llm_mapper import FieldMapper
from job_ai2_agent.models import AgentRunResult, EducationItem, WorkExperience
from job_ai2_agent.resume_reader import read_resume_profile


class JobApplicationAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(
        self,
        resume_path: Path,
        job_url: str,
        overrides: dict[str, str] | None = None,
        account_profile: dict[str, str] | None = None,
    ) -> AgentRunResult:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        profile = read_resume_profile(resume_path)
        if account_profile:
            for key, value in account_profile.items():
                if value.strip():
                    profile.fields[key] = value.strip()
        if overrides:
            for key, value in overrides.items():
                if value.strip():
                    profile.fields[key] = value.strip()
            _apply_structured_overrides(profile, overrides)
        mapper = FieldMapper(
            api_key=self.settings.openai_api_key,
            model=self.settings.openai_model,
            local_provider=self.settings.local_llm_provider,
            local_base_url=self.settings.local_llm_base_url,
            local_model=self.settings.local_llm_model,
            local_timeout_seconds=self.settings.local_llm_timeout_seconds,
        )
        screenshot_path = self.settings.screenshot_dir / f"run_{timestamp}.png"
        result, decisions = await fill_job_application(
            job_url=job_url,
            resume_path=resume_path,
            profile=profile,
            mapper=mapper,
            headless=self.settings.browser_headless,
            hold_seconds=self.settings.browser_hold_seconds,
            screenshot_path=screenshot_path,
        )
        review_path = self.settings.review_dir / f"run_{timestamp}.json"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        result.review_path = str(review_path)
        review = {
            "file_location_comment": f"File: {review_path}",
            "result": result.to_dict(),
            "resume_profile": profile.fields,
            "work_experiences": [asdict(item) for item in profile.work_experiences],
            "education_items": [asdict(item) for item in profile.education_items],
            "decisions": [asdict(decision) for decision in decisions],
        }
        review_text = json.dumps(review, indent=2)
        review_path.write_text(review_text, encoding="utf-8")
        (self.settings.review_dir / "latest_review.json").write_text(
            review_text,
            encoding="utf-8",
        )
        latest_error = self.settings.review_dir / "latest_error.txt"
        if latest_error.exists():
            latest_error.unlink()
        return result


def _apply_structured_overrides(profile, overrides):
    work_experiences = []
    for index in range(6):
        prefix = f"work_{index}_"
        title = overrides.get(f"{prefix}title", "").strip()
        company = overrides.get(f"{prefix}company", "").strip()
        if not title and not company:
            continue
        work_experiences.append(
            WorkExperience(
                title=title,
                company=company,
                location=overrides.get(f"{prefix}location", "").strip(),
                start_month=overrides.get(f"{prefix}start_month", "").strip(),
                start_year=overrides.get(f"{prefix}start_year", "").strip(),
                end_month=overrides.get(f"{prefix}end_month", "").strip(),
                end_year=overrides.get(f"{prefix}end_year", "").strip(),
                currently_work_here=overrides.get(f"{prefix}currently_work_here", "").strip().lower() == "yes",
                description=overrides.get(f"{prefix}description", "").strip(),
            )
        )
    if work_experiences:
        profile.work_experiences = work_experiences
        current = work_experiences[0]
        profile.fields["current_job_title"] = current.title
        profile.fields["current_company"] = current.company
        profile.fields["current_job_location"] = current.location
        profile.fields["current_job_start_month"] = current.start_month
        profile.fields["current_job_start_year"] = current.start_year

    education_items = []
    for index in range(4):
        prefix = f"education_{index}_"
        school = overrides.get(f"{prefix}school", "").strip()
        degree = overrides.get(f"{prefix}degree", "").strip()
        if not school and not degree:
            continue
        education_items.append(
            EducationItem(
                school=school,
                degree=degree,
                field_of_study=overrides.get(f"{prefix}field", "").strip(),
                end_year=overrides.get(f"{prefix}end_year", "").strip(),
            )
        )
    if education_items:
        profile.education_items = education_items
