"""Bounded, untrusted model output. No executable fields or tools."""
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1000)]
ShortText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=300)]


class PlanStep(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    number: int = Field(ge=1, le=12)
    action: ShortText
    purpose: Text
    expected_output: Text
    dependencies: list[Annotated[int, Field(ge=1, le=12)]] = Field(max_length=11)


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    goal: Text
    assumptions: list[ShortText] = Field(max_length=10)
    constraints: list[ShortText] = Field(max_length=10)
    steps: list[PlanStep] = Field(min_length=1, max_length=12)
    clarification_needed: bool
    clarification_questions: list[ShortText] = Field(max_length=10)

    @model_validator(mode='after')
    def coherent_plan(self):
        if self.clarification_needed != bool(self.clarification_questions):
            raise ValueError('Clarification flag and questions must agree')
        for number, step in enumerate(self.steps, 1):
            if step.number != number:
                raise ValueError('Steps must be consecutively numbered from 1')
            if len(set(step.dependencies)) != len(step.dependencies) or any(d >= number for d in step.dependencies):
                raise ValueError('Dependencies must be unique earlier step numbers')
        return self
