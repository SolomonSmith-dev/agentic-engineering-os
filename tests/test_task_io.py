import os
from datetime import UTC, datetime

import pytest
import yaml

from agent_team.schemas.task_file import TaskResult
from agent_team.tasks.io import (
    TaskFileError,
    find_result_block,
    load_task_file,
    sidecar_path,
    splice_result,
    without_result,
    write_result,
    write_sidecar,
)

BODY = ("kind: task  # Ledger\nid: T-0001\ntarget: {repo: /tmp/r, base: main}\nrequest: 'quoted: yes'\n"
        "paths: [src/]\nbudget:\n  tokens: 2000000\n  minutes: 45\n  usd: 5.00\nworkflow: bug-fix\nstatus: claimed\n")
RESULT = TaskResult(run_id="20261102-180411-8f3c00", outcome="pr_opened", tokens=412345, usd_list=1.87,
                    minutes=22.4, pr=112, trace="/x/trace.jsonl", finished_at=datetime(2026, 11, 2, 18, 4, 11, tzinfo=UTC))

CASES = {
    "no_result": BODY,
    "no_trailing_newline": BODY.rstrip("\n"),
    "inline_null": BODY + "result: null\n",
    "block_then_comment": BODY + "result:\n  outcome: busy\n  finished_at: 2026-11-01T00:00:00Z\n\n# Numenor note\nnext: 1\n",
    "crlf_flow": BODY.replace("\n", "\r\n") + "result: {outcome: busy}\r\n",
    "result_first": "result:\n  outcome: busy\n" + BODY,
}


@pytest.mark.parametrize("text", CASES.values(), ids=CASES.keys())
def test_splice_preserves_every_other_byte(text):
    out = splice_result(text, RESULT)
    nl = "\r\n" if "\r\n" in text else "\n"
    assert without_result(out) == without_result(text if text.endswith("\n") else text + nl)
    assert yaml.safe_load(out)["result"]["pr"] == 112
    assert ("\r\n" in out) == ("\r\n" in text)
    if "\r\n" in out:
        assert "\n" not in out.replace("\r\n", "")
    assert splice_result(out, RESULT) == out, "idempotent"


def test_comment_after_block_and_blank_line_survive():
    out = splice_result(CASES["block_then_comment"], RESULT)
    assert out.endswith("\n\n# Numenor note\nnext: 1\n")
    assert "busy" not in out


def test_result_stays_last_key_when_appended():
    out = splice_result(BODY, RESULT)
    assert list(yaml.safe_load(out))[-1] == "result"
    assert out.startswith(BODY)


def test_find_result_block_none_and_span():
    assert find_result_block(BODY) is None
    text = CASES["inline_null"]
    s, e = find_result_block(text)
    assert text[s:e] == "result: null\n"


def test_load_refuses_id_mismatch_and_bad_yaml(tmp_path):
    (tmp_path / "T-0002.yaml").write_text(BODY)
    (tmp_path / "T-0003.yaml").write_text("kind: [")
    for name in ("T-0002.yaml", "T-0003.yaml"):
        with pytest.raises(TaskFileError) as err:
            load_task_file(tmp_path / name)
        assert err.value.problems


def test_load_refuses_missing_nonmapping_and_schema_errors(tmp_path):
    (tmp_path / "T-0001.yaml").write_text("- a\n- b\n")
    (tmp_path / "T-0004.yaml").write_text("kind: task\nid: T-0004\n")
    for name in ("T-0001.yaml", "T-0004.yaml", "T-0009.yaml"):
        with pytest.raises(TaskFileError) as err:
            load_task_file(tmp_path / name)
        assert err.value.problems and all(": " in p for p in err.value.problems)


def test_load_keeps_crlf_bytes(tmp_path):
    p = tmp_path / "T-0001.yaml"
    p.write_bytes(BODY.replace("\n", "\r\n").encode())
    assert "\r\n" in load_task_file(p).text


def test_write_result_in_place(tmp_path):
    p = tmp_path / "T-0001.yaml"
    p.write_text(BODY)
    os.chmod(p, 0o640)
    loaded = load_task_file(p)
    assert write_result(loaded, RESULT) == p
    assert without_result(p.read_text()) == without_result(BODY)
    assert yaml.safe_load(p.read_text())["result"]["outcome"] == "pr_opened"
    assert (p.stat().st_mode & 0o777) == 0o640
    assert [x.name for x in tmp_path.iterdir()] == ["T-0001.yaml"], "no temp file left"


def test_write_result_in_place_preserves_crlf_no_newline(tmp_path):
    p = tmp_path / "T-0001.yaml"
    raw = BODY.replace("\n", "\r\n").rstrip("\r\n")
    p.write_bytes(raw.encode())
    write_result(load_task_file(p), RESULT)
    data = p.read_bytes().decode()
    assert data.startswith(raw + "\r\n") and "\n" not in data.replace("\r\n", "")


def test_write_result_goes_to_sidecar_when_file_changed(tmp_path):  # Review Focus 1
    p = tmp_path / "T-0001.yaml"
    p.write_text(BODY)
    loaded = load_task_file(p)
    changed = BODY.replace("status: claimed", "status: running")
    p.write_text(changed)
    written = write_result(loaded, RESULT)
    assert written == sidecar_path(p, "T-0001")
    assert p.read_text() == changed, "task file untouched"
    side = yaml.safe_load(written.read_text())
    assert side["kind"] == "task-result" and side["id"] == "T-0001"
    assert side["result"]["outcome"] == "pr_opened"


def test_write_sidecar_for_unparseable_file(tmp_path):
    p = tmp_path / "T-0003.yaml"
    p.write_text("kind: [")
    written = write_sidecar(p, p.stem, RESULT)
    assert written == tmp_path / "T-0003.result.yaml"
    assert written.read_text().startswith("kind: task-result\nid: T-0003\nresult:\n")


def test_column_zero_comment_between_result_fields_does_not_truncate_block(tmp_path):
    text = (
        "kind: task\n"
        "id: T-0001\n"
        "target: {repo: /tmp/r, base: main}\n"
        "request: 'quoted: yes'\n"
        "paths: [src/]\n"
        "budget:\n  tokens: 2000000\n  minutes: 45\n  usd: 5.00\n"
        "workflow: bug-fix\n"
        "status: claimed\n"
        "result:\n"
        "  run_id: old-run\n"
        "# Column-zero comment between fields\n"
        "  outcome: failed\n"
        "  finished_at: 2026-11-01T00:00:00Z\n"
    )
    p = tmp_path / "T-0001.yaml"
    p.write_text(text)
    span = find_result_block(text)
    assert span is not None
    assert "outcome: failed" in text[span[0] : span[1]]
    assert "finished_at: 2026-11-01" in text[span[0] : span[1]]
    loaded = load_task_file(p)
    write_result(loaded, RESULT)
    written = p.read_text()
    assert written.count("outcome:") == 1
    assert "outcome: failed" not in written
    reloaded = load_task_file(p)
    assert reloaded.task.result is not None
    assert reloaded.task.result.outcome == RESULT.outcome


def test_write_result_flow_mapping_with_column_zero_closing_brace(tmp_path):
    text = (
        "kind: task\n"
        "id: T-0001\n"
        "target:\n  repo: /tmp/r\n  base: main\n"
        "request: 'quoted: yes'\n"
        "paths:\n  - src/\n"
        "budget:\n  tokens: 2000000\n  minutes: 45\n  usd: 5.00\n"
        "workflow: bug-fix\n"
        "status: claimed\n"
        "result: {\n"
        "  run_id: old-run,\n"
        "  outcome: busy,\n"
        "  finished_at: 2026-11-01T00:00:00Z\n"
        "}\n"
    )
    p = tmp_path / "T-0001.yaml"
    p.write_text(text)
    loaded = load_task_file(p)
    assert write_result(loaded, RESULT) == p
    data = p.read_text()
    assert "}" not in data
    reloaded = load_task_file(p)
    assert reloaded.task.result is not None
    assert reloaded.task.result.outcome == "pr_opened"


def test_find_result_block_unbalanced_flow_mapping_returns_none():
    unbalanced = (
        "kind: task\n"
        "id: T-0001\n"
        "result: {\n"
        "  run_id: old-run,\n"
        "  outcome: busy\n"
    )
    assert find_result_block(unbalanced) is None

