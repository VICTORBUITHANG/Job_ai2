# File: /Users/victorbui/AI/Job_ai2/job_ai2_agent/models.py
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class WorkExperience:
    title: str = ""
    company: str = ""
    location: str = ""
    start_month: str = ""
    start_year: str = ""
    end_month: str = ""
    end_year: str = ""
    currently_work_here: bool = False
    description: str = ""


@dataclass(slots=True)
class EducationItem:
    school: str = ""
    degree: str = ""
    field_of_study: str = ""
    end_year: str = ""


@dataclass(slots=True)
class ResumeProfile:
    fields: dict[str, str]
    raw_text: str
    work_experiences: list[WorkExperience] = field(default_factory=list)
    education_items: list[EducationItem] = field(default_factory=list)


@dataclass(slots=True)
class ApplicationField:
    selector: str
    label: str
    tag: str
    input_type: str
    name: str = ""
    placeholder: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FillDecision:
    selector: str
    label: str
    action: str
    value: str
    confidence: float
    reason: str


@dataclass(slots=True)
class AgentRunResult:
    status: str
    job_url: str
    filled_count: int
    skipped_count: int
    review_path: str
    screenshot_path: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
