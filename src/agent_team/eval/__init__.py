"""agent_team.eval: the step 13 eval harness. Tasks, arms, budgets, the hidden verifier and the report."""

from agent_team.eval.arms import (
    ARM_DESCRIPTIONS,
    BASELINE_CLASS,
    EVAL_ENVIRONMENT,
    MIN_CALL_USD,
    PROBE_BUDGET_USD,
    ArmContext,
    CappedExecutor,
    baseline_model,
    refusal,
    treatment_models,
)
from agent_team.eval.field import FieldError, record_last
from agent_team.eval.report import aggregate, decision, render_field, render_report
from agent_team.eval.runner import (
    EvalError,
    build_plan,
    estimate,
    hidden_paths,
    new_eval_id,
    prepare_canonical,
    render_plan,
    run_eval,
)
from agent_team.eval.store import EvalStore, EvalStoreError
from agent_team.eval.tasks import LoadedTask, TaskError, load_tasks, materialize

__all__ = [
    "ARM_DESCRIPTIONS",
    "BASELINE_CLASS",
    "EVAL_ENVIRONMENT",
    "MIN_CALL_USD",
    "PROBE_BUDGET_USD",
    "ArmContext",
    "CappedExecutor",
    "EvalError",
    "EvalStore",
    "EvalStoreError",
    "FieldError",
    "LoadedTask",
    "TaskError",
    "aggregate",
    "baseline_model",
    "build_plan",
    "decision",
    "estimate",
    "hidden_paths",
    "load_tasks",
    "materialize",
    "new_eval_id",
    "prepare_canonical",
    "record_last",
    "refusal",
    "render_field",
    "render_plan",
    "render_report",
    "run_eval",
    "treatment_models",
]
