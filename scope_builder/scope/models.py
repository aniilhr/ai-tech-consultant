"""Typed data that flows between the Scope Builder agents. Every hand-off is one of these models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["supported_by_configuration", "partially_supported", "custom_development", "needs_clarification"]
Priority = Literal["must", "should", "could"]
ChangeType = Literal["added", "modified", "removed", "unchanged"]
Role = Literal["consultant", "designer", "developer", "qa", "project_manager"]


# ---------------------------------------------------------------- Extractor agent output
class ExtractedRequirement(BaseModel):
    title: str = Field(description="Short name, 3-8 words")
    description: str = Field(description="What the client needs, in concrete terms")
    priority: Priority = "should"
    source_quote: str = Field(description="Verbatim sentence from the transcript that states this need")
    speaker: str = ""
    ambiguous: bool = Field(default=False, description="True when the client was vague (e.g. 'make search better')")
    clarification_question: str = Field(default="", description="Question to ask when ambiguous")


class ExtractedItem(BaseModel):
    text: str
    source_quote: str = ""


class Extraction(BaseModel):
    requirements: list[ExtractedRequirement] = Field(default_factory=list)
    decisions: list[ExtractedItem] = Field(default_factory=list)
    assumptions: list[ExtractedItem] = Field(default_factory=list)
    dependencies: list[ExtractedItem] = Field(default_factory=list)
    open_questions: list[ExtractedItem] = Field(default_factory=list)


class Requirement(ExtractedRequirement):
    id: str
    quote_verified: bool = True
    origin: str = Field(default="baseline", description="baseline | change:<scope version>")


class Item(ExtractedItem):
    quote_verified: bool = True


# ---------------------------------------------------------------- Capability checker output
class CapabilityJudgement(BaseModel):
    status: Status
    rationale: str = Field(description="One or two sentences citing the evidence")
    product_features: list[str] = Field(default_factory=list, description="Existing settings/features that cover it")


class Evidence(BaseModel):
    title: str
    url: str | None = None
    snippet: str = ""


class CapabilityCheck(BaseModel):
    requirement_id: str
    status: Status
    rationale: str
    product_features: list[str] = Field(default_factory=list)
    brain_question: str
    brain_answered: bool
    brain_answer: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    mode: str = "offline"
    guard_note: str = ""


# ---------------------------------------------------------------- Estimator output
class TaskDraft(BaseModel):
    title: str
    role: Role = "developer"
    low: float = Field(ge=0)
    likely: float = Field(ge=0)
    high: float = Field(ge=0)
    notes: str = ""


class RequirementTasks(BaseModel):
    requirement_id: str
    tasks: list[TaskDraft]


class EstimateOut(BaseModel):
    items: list[RequirementTasks]


class Task(TaskDraft):
    id: str
    requirement_id: str | None


class Totals(BaseModel):
    low: float = 0
    likely: float = 0
    high: float = 0


class Estimate(BaseModel):
    tasks: list[Task]
    totals: Totals
    by_requirement: dict[str, Totals]
    method: str
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- Change analyst output
class ChangeDraft(BaseModel):
    change_type: ChangeType
    baseline_requirement_id: str | None = Field(default=None, description="ID from the baseline list, null when added")
    title: str
    description: str = ""
    source_quote: str = ""
    client_requested_extra_quote: bool = Field(
        default=False, description="True if consultant/client said it must be quoted separately"
    )


class ChangeAnalysis(BaseModel):
    changes: list[ChangeDraft]


class ChangeItem(BaseModel):
    change_type: ChangeType
    requirement_id: str
    baseline_requirement_id: str | None
    title: str
    summary: str
    source_quote: str = ""
    quote_verified: bool = True
    out_of_scope: bool = False
    hours_delta: Totals = Field(default_factory=Totals)


class ChangeReport(BaseModel):
    baseline_scope_id: int
    baseline_version: int
    items: list[ChangeItem]
    added: int
    modified: int
    removed: int
    out_of_scope_hours: Totals
    total_delta: Totals


# ---------------------------------------------------------------- Scope document
class ScopeData(BaseModel):
    requirements: list[Requirement]
    decisions: list[Item]
    assumptions: list[Item]
    dependencies: list[Item]
    open_questions: list[Item]
    capability_checks: dict[str, CapabilityCheck]
    estimate: Estimate
    llm: dict[str, object] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
