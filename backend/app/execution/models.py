from typing import Annotated, Literal, get_args
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from app.plan_schema import PlanStep, TaskPlan
from app.execution.python_models import PythonCall, PythonEvidence

PathText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=240)]
AgentRole = Literal['Orchestrator', 'Planner', 'Safety', 'Executor', 'Verifier']
AGENT_ROLES = get_args(AgentRole)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: Literal['read_text', 'write_text', 'validate_json']
    path: PathText
    content: str | None = Field(max_length=8000)
    required_keys: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(max_length=20)

    @model_validator(mode='after')
    def relevant_arguments(self):
        if (self.name == 'write_text') != (self.content is not None):
            raise ValueError('Only write_text accepts content, and requires it')
        if self.name != 'validate_json' and self.required_keys:
            raise ValueError('Only validate_json accepts required_keys')
        return self


class ExecutableStep(PlanStep):
    tool: Annotated[ToolCall | PythonCall, Field(discriminator='name')]


class ExecutionPlan(TaskPlan):
    workflow: Literal['workspace', 'five_agent', 'python'] | None = None
    steps: list[ExecutableStep] = Field(min_length=1, max_length=12)

    @model_validator(mode='after')
    def five_agent_contract(self):
        python = [s for s in self.steps if isinstance(s.tool, PythonCall)]
        if python and (self.workflow != 'python' or len(self.steps) != 1 or self.steps[0].dependencies):
            raise ValueError('Python is one standalone sandbox step; no file tools or dependencies')
        if self.workflow == 'python' and len(python) != 1:
            raise ValueError('Python workflow requires exactly one Python tool')
        if self.workflow == 'five_agent':
            if [s.tool.name for s in self.steps] != ['read_text', 'write_text', 'validate_json']:
                raise ValueError('Five-agent JSON workflow requires research read, development write, then JSON test')
            research, development, testing = self.steps
            if (1 not in development.dependencies or 2 not in testing.dependencies
                    or research.tool.path == development.tool.path
                    or testing.tool.path != development.tool.path or not testing.tool.required_keys):
                raise ValueError('Five-agent workflow requires source evidence, a new destination and dependent JSON checks')
        return self

    def display_plan(self) -> TaskPlan:
        data = self.model_dump()
        data.pop('workflow')
        for step in data['steps']:
            del step['tool']
        return TaskPlan.model_validate(data)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision: Literal['approve', 'reject']


class ToolResult(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str
    sha256: str
    byte_count: int
    content: str | None = None
    checks: list[str] = Field(default_factory=list)


class Verification(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    passed: bool
    checks: list[str]


class StepState(BaseModel):
    number: int
    dependencies: list[int]
    tool: ToolCall | PythonCall
    status: Literal['pending', 'executing', 'executed', 'verified', 'failed']
    attempts: int
    result: ToolResult | PythonEvidence | None
    verification: Verification | None
    error: str | None


class ExecutionResponse(BaseModel):
    run_id: str
    task_id: str
    phase: Literal['planning', 'awaiting_user_input', 'awaiting_approval', 'executing', 'verifying', 'completed', 'failed', 'cancelled']
    plan_digest: str | None
    approval_required: bool
    approval_decision: str | None
    steps: list[StepState]
    error: str | None
    agents: list['AgentState'] = Field(default_factory=list)


class TextCheck(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str
    category: Literal['integrity', 'static_code', 'correctness']
    status: Literal['passed', 'failed', 'not_checked'] = 'not_checked'
    detail: str = 'This check did not run.'


class TextVerification(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    code_executed: Literal[False] = False
    factual_correctness_verified: Literal[False] = False
    answer_sha256: str | None = None
    checks: list[TextCheck] = Field(default_factory=list)


class AgentState(BaseModel):
    role: AgentRole
    status: Literal['pending', 'running', 'succeeded', 'failed', 'skipped']
    attempts: int = 0
    input_summary: str
    output_summary: str
    input_steps: list[int] = Field(default_factory=list)
    evidence: list[ToolResult] = Field(default_factory=list)
    sandbox_evidence: list[PythonEvidence] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    text_verification: TextVerification | None = None
    error: str | None = None
