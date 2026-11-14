import json
import pathlib

import pytest

pytest.importorskip("behalf")

import harvest
import soft
import store
from detect import detect, managers

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class FakeRunner:
    """Calls record_labels with fixed labels, like behalf's own test runner."""

    def __init__(self, labels, calls=1):
        self.labels, self.calls, self.prompts = labels, calls, []

    async def converse(self, task):
        raise AssertionError("conversation must be skipped when a manifest is given")

    async def run_agent(self, system_prompt, user_prompt, tools, confirm_fn):
        self.prompts.append((system_prompt, user_prompt))
        record = {t.name: t for t in tools}["record_labels"]
        for _ in range(self.calls):
            await record.handler({"labels": self.labels})


def db_with(tmp_path, name):
    db = store.connect(tmp_path / "h.sqlite")
    data = (FIXTURES / name).read_bytes()
    counts = detect(data.decode())
    store.save_blob(db, store.git_blob_sha(data), data, counts, managers(counts))
    db.commit()
    return db


def test_agreement_statuses():
    hard = {"nodes": 2, "tasks": None, "gpus": 8, "walltime_seconds": 60,
            "queue": "Debug", "array_count": None}
    s = {"nodes": 2, "tasks": 16, "gpus": 4, "walltime_seconds": None, "queue": "debug"}
    assert soft.agreement(hard, s) == {
        "nodes": "match", "tasks": "soft_only", "gpus": "mismatch",
        "walltime_seconds": "hard_only", "queue": "match", "array_count": "none"}


def test_pipeline_label_soft_agree_export(tmp_path):
    db = db_with(tmp_path, "slurm_sbatch_doc.sh")
    assert harvest.label(db, relabel=False) == 1
    runner = FakeRunner({"application": "hostname", "nodes": None, "walltime_seconds": 60})
    assert harvest.soft_label(db, "claude", "test-model", 10, runner=runner) == 1
    # the model sees the script but not the parser output
    system, user = runner.prompts[0]
    assert "#SBATCH --time=1" in user and "walltime_seconds" in system
    assert "totals" not in user
    harvest.agree(db)
    status = dict(db.execute("SELECT field, status FROM agreement").fetchall())
    assert status["walltime_seconds"] == "match"
    out = tmp_path / "out.jsonl"
    harvest.export(db, out)
    record = json.loads(out.read_text())
    assert record["hard_labels"]["slurm"]["totals"]["walltime_seconds"] == 60
    assert record["soft_labels"][0]["labels"]["application"] == "hostname"


def test_soft_label_records_failure_when_nothing_recorded(tmp_path):
    db = db_with(tmp_path, "slurm_sbatch_doc.sh")
    harvest.soft_label(db, "claude", "m", 10, runner=FakeRunner({}, calls=0))
    status = db.execute("SELECT status FROM soft_labels").fetchone()[0]
    assert status.startswith("error: expected one recorded label object")


def test_label_is_idempotent(tmp_path):
    db = db_with(tmp_path, "pbs_openpbs_doc.py")
    assert harvest.label(db, relabel=False) == 1
    assert harvest.label(db, relabel=False) == 0
    assert harvest.label(db, relabel=True) == 1
