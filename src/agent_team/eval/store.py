"""The eval results store: `.agent-team/eval/results.jsonl`, append-only, one record per line.

JSONL rather than another SQLite file: every record is written once and never
updated, a cell is one self-contained line, and the file reads with any tool.
The run record (`runs.db` inside each cell's task repository, kept under
`work/`) stays the source of truth for what an agent did; a cell line copies
out the numbers the report needs and names the run ids it came from.

Like the run record, the store refuses to write unless git ignores `.agent-team/`.
"""

import json
from collections.abc import Iterator
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from agent_team.runs.store import STATE_DIR, check_state_dir_ignored, repo_root
from agent_team.schemas.eval import CellResult, DryRunRecord, EvalLine, EvalRecord, FieldRecord, ProbeRecord

EVAL_DIR = "eval"
RESULTS = "results.jsonl"
WORK = "work"

_LINE: TypeAdapter[EvalLine] = TypeAdapter(EvalLine)


class EvalStoreError(RuntimeError):
    """The results store cannot be opened or read; the message says what to fix."""


class EvalStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir = root / STATE_DIR / EVAL_DIR
        self.path = self.dir / RESULTS

    @classmethod
    def open(cls, repo: Path) -> "EvalStore":
        root = repo_root(repo)
        check_state_dir_ignored(root)
        return cls(root)

    @property
    def work_dir(self) -> Path:
        return self.dir / WORK

    def append(self, record: CellResult | EvalRecord | DryRunRecord | ProbeRecord | FieldRecord) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")

    def records(self) -> Iterator[EvalLine]:
        if not self.path.is_file():
            return
        with self.path.open(encoding="utf-8") as f:
            for number, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    yield _LINE.validate_python(json.loads(line))
                except (ValueError, ValidationError) as exc:
                    raise EvalStoreError(f"{self.path}:{number} is not an eval record ({type(exc).__name__})") from exc

    def evals(self) -> list[EvalRecord]:
        return [r for r in self.records() if isinstance(r, EvalRecord)]

    def cells(self, eval_id: str | None = None) -> list[CellResult]:
        return [r for r in self.records() if isinstance(r, CellResult) and (eval_id is None or r.eval_id == eval_id)]

    def probes(self, eval_id: str) -> list[ProbeRecord]:
        return [r for r in self.records() if isinstance(r, ProbeRecord) and r.eval_id == eval_id]

    def dry_run_hashes(self) -> set[str]:
        return {r.plan.plan_hash for r in self.records() if isinstance(r, DryRunRecord)}

    def fields(self) -> list[FieldRecord]:
        return [r for r in self.records() if isinstance(r, FieldRecord)]
