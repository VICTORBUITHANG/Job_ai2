# job_ai2_agent/llm_mapper.py

import json
from dataclasses import asdict
from difflib import SequenceMatcher

from job_ai2_agent.local_llm import LocalLLMError, OllamaClient
from job_ai2_agent.models import ApplicationField, FillDecision, ResumeProfile


class FieldMapper:
    def __init__(
        self,
        api_key: str,
        model: str,
        local_provider: str = "",
        local_base_url: str = "",
        local_model: str = "",
        local_timeout_seconds: int = 60,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.local_provider = local_provider
        self.local_base_url = local_base_url
        self.local_model = local_model
        self.local_timeout_seconds = local_timeout_seconds

    def decisions(
        self,
        fields: list[ApplicationField],
        profile: ResumeProfile,
    ) -> list[FillDecision]:
        if self.local_provider == "ollama":
            try:
                return self._decisions_with_ollama(fields, profile)
            except Exception as exc:
                fallback = self._decisions_with_next_available_model(fields, profile)
                fallback.append(
                    FillDecision(
                        selector="",
                        label="Mapper warning",
                        action="skip",
                        value="",
                        confidence=0.0,
                        reason=f"Ollama mapping failed, used fallback: {exc}",
                    )
                )
                return fallback
        return self._decisions_with_next_available_model(fields, profile)

    def _decisions_with_next_available_model(
        self,
        fields: list[ApplicationField],
        profile: ResumeProfile,
    ) -> list[FillDecision]:
        if self.api_key:
            try:
                return self._decisions_with_openai(fields, profile)
            except Exception as exc:
                fallback = self._decisions_with_heuristics(fields, profile)
                fallback.append(
                    FillDecision(
                        selector="",
                        label="Mapper warning",
                        action="skip",
                        value="",
                        confidence=0.0,
                        reason=f"OpenAI mapping failed, used heuristic fallback: {exc}",
                    )
                )
                return fallback
        return self._decisions_with_heuristics(fields, profile)

    def _decisions_with_ollama(
        self,
        fields: list[ApplicationField],
        profile: ResumeProfile,
    ) -> list[FillDecision]:
        client = OllamaClient(
            base_url=self.local_base_url,
            model=self.local_model,
            timeout_seconds=self.local_timeout_seconds,
        )
        prompt = _mapping_prompt(fields, profile)
        payload = client.generate_json(prompt)
        decisions = _validated_decisions(payload, fields)
        if not decisions:
            raise LocalLLMError("Ollama returned no usable decisions.")
        return decisions

    def _decisions_with_openai(
        self,
        fields: list[ApplicationField],
        profile: ResumeProfile,
    ) -> list[FillDecision]:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)
        prompt = _mapping_prompt(fields, profile)
        response = client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "system",
                    "content": "You are a careful job application form filling agent.",
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=True)},
            ],
            text={"format": {"type": "json_object"}},
        )
        payload = json.loads(response.output_text)
        return _validated_decisions(payload, fields)

    def _decisions_with_heuristics(
        self,
        fields: list[ApplicationField],
        profile: ResumeProfile,
    ) -> list[FillDecision]:
        decisions: list[FillDecision] = []
        for field in fields:
            key, value, confidence = _best_match(field, profile.fields)
            action = "select" if field.tag == "select" else "fill"
            if field.input_type in {"checkbox", "radio"}:
                action = "check" if value.lower() in {"yes", "true", "1", "checked"} else "skip"
            if not value:
                action = "skip"
            decisions.append(
                FillDecision(
                    selector=field.selector,
                    label=field.label,
                    action=action,
                    value=value,
                    confidence=confidence,
                    reason=f"Matched resume field '{key}'." if value else "No safe resume match.",
                )
            )
        return decisions


def _best_match(field: ApplicationField, profile: dict[str, str]) -> tuple[str, str, float]:
    label = " ".join(
        part for part in [field.label, field.name, field.placeholder, field.input_type] if part
    ).lower()
    aliases = {
        "legal_name": ("legal name", "legal full name"),
        "full_name": ("full name", "legal name"),
        "first_name": ("first name", "given name"),
        "last_name": ("last name", "family name", "surname"),
        "preferred_name": ("preferred name", "nickname", "chosen name"),
        "email": ("email", "e-mail"),
        "phone": ("phone", "mobile", "telephone"),
        "location": ("location", "city", "state", "address"),
        "linkedin": ("linkedin", "linked in"),
        "github": ("github", "git hub"),
        "website": ("website", "portfolio", "site"),
        "skills": ("skills", "technologies", "tools"),
        "education": ("education", "school", "university", "degree"),
        "experience": ("experience", "employment", "work history"),
        "summary": ("summary", "about", "cover letter", "why are you interested"),
        "desired_start_date": ("desired start date", "start date", "available to start", "availability"),
        "work_authorization": ("authorized", "legally permitted", "eligible to work", "work authorization"),
        "visa_sponsorship": ("sponsorship", "visa", "immigration sponsorship"),
        "desired_salary": ("salary", "compensation", "desired annual salary", "pay expectation"),
        "application_questions_notes": ("additional question", "application question", "notes", "other information"),
        "ethnicity": ("ethnicity", "hispanic", "latino"),
        "race": ("race", "racial identity"),
        "gender": ("gender", "sex"),
        "veteran_status": ("veteran", "protected veteran"),
        "disability_status": ("disability", "disabled"),
        "self_identify_gender": ("self identify gender", "gender identity", "self-identify"),
        "pronouns": ("pronoun", "pronouns"),
    }
    best_key = ""
    best_score = 0.0
    for key, value in profile.items():
        candidates = aliases.get(key, (key,))
        score = max(_score(label, candidate) for candidate in candidates)
        if value and score > best_score:
            best_key = key
            best_score = score
    if best_score < 0.55:
        return "", "", best_score
    return best_key, profile[best_key], best_score


def _mapping_prompt(fields: list[ApplicationField], profile: ResumeProfile) -> dict:
    return {
        "task": "Use resume data to fill job application fields.",
        "rules": [
            "Return only JSON.",
            "Use resume information and saved account-profile answers.",
            "Do not invent missing facts.",
            "For select fields, choose one of the available options when possible.",
            "Use action skip for fields that cannot be safely answered.",
            "Never submit the form.",
            "Use only selectors copied exactly from the provided fields list.",
        ],
        "schema": {
            "decisions": [
                {
                    "selector": "CSS selector from fields",
                    "label": "field label",
                    "action": "fill|select|check|skip",
                    "value": "field value",
                    "confidence": 0.0,
                    "reason": "short reason",
                }
            ]
        },
        "resume_profile": profile.fields,
        "work_experiences": [asdict(item) for item in profile.work_experiences],
        "education_items": [asdict(item) for item in profile.education_items],
        "resume_text": profile.raw_text[:12_000],
        "fields": [asdict(field) for field in fields],
    }


def _validated_decisions(payload: dict, fields: list[ApplicationField]) -> list[FillDecision]:
    allowed_selectors = {field.selector for field in fields}
    allowed_actions = {"fill", "select", "check", "skip"}
    decisions: list[FillDecision] = []
    raw_decisions = payload.get("decisions", [])
    if not isinstance(raw_decisions, list):
        return decisions
    for item in raw_decisions:
        if not isinstance(item, dict):
            continue
        selector = str(item.get("selector", ""))
        action = str(item.get("action", "skip"))
        if selector not in allowed_selectors or action not in allowed_actions:
            continue
        decisions.append(
            FillDecision(
                selector=selector,
                label=str(item.get("label", "")),
                action=action,
                value=str(item.get("value", "")),
                confidence=_float_or_zero(item.get("confidence", 0.0)),
                reason=str(item.get("reason", "")),
            )
        )
    return decisions


def _float_or_zero(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _score(left: str, right: str) -> float:
    if right in left:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()
