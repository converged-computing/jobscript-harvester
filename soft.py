"""Soft labels from a model via behalf, and agreement with parsed labels."""

import json

from behalf import Task, ToolSpec

SOFT_VERSION = "1"

# Fields compared against parse.totals(); everything else is soft-only.
OVERLAP = ("nodes", "tasks", "gpus", "walltime_seconds", "queue", "array_count")

SOFT_FIELDS = {
    "application": "main program the job runs (e.g. lammps, gmx, python script name), or null",
    "software": "list of other software, libraries, or tools used",
    "launcher": "parallel launcher used (srun, mpirun, mpiexec, jsrun, flux run, aprun, torchrun), or null",
    "modules": "list of environment modules loaded",
    "containers": "list of container images or runtimes used",
    "inputs": "list of input files or datasets the job reads",
    "purpose": "one sentence describing what the job does",
    "nodes": "total nodes the job uses, integer or null",
    "tasks": "total tasks or MPI ranks, integer or null",
    "gpus": "total GPUs across the job, integer or null",
    "walltime_seconds": "time limit in seconds, integer or null",
    "queue": "queue or partition name, or null",
    "array_count": "number of array elements, integer or null",
}

SYSTEM_PROMPT = """You label HPC batch job scripts. Read the whole script, including
directives, environment setup, launcher lines, and shell variables. Call the
record_labels tool exactly once with a JSON object containing these keys:

{fields}

Use null (or an empty list) when the script does not determine a value. Do not
guess values that the script does not state or compute."""


class SoftLabelTask(Task):
    """Extract soft labels for one jobscript."""

    name = "jobscript-soft-label"

    def __init__(self):
        self.recorded = []

    def manifest_schema(self):
        """Manifest: the script text as the goal."""
        return {"goal": str}

    def build_tools(self, manifest):
        """A single action that records the labels."""

        async def record(args):
            labels = args.get("labels", args)
            if isinstance(labels, str):
                labels = json.loads(labels)
            self.recorded.append(labels)
            return {"content": [{"type": "text", "text": "recorded"}]}

        return [ToolSpec("record_labels", "Record the labels for this script.",
                         {"labels": dict}, record)]

    def execute_system_prompt(self, manifest):
        """Instructions listing the label fields."""
        fields = "\n".join(f"- {k}: {v}" for k, v in SOFT_FIELDS.items())
        return SYSTEM_PROMPT.format(fields=fields)

    def validate_result(self, result):
        """Require exactly one recorded label object."""
        if len(self.recorded) != 1 or not isinstance(self.recorded[0], dict):
            raise ValueError(f"expected one recorded label object, got {len(self.recorded)}")


def manifest_for(script):
    """Return the behalf manifest for a script."""
    return {"goal": f"Label this job script:\n\n```\n{script}\n```"}


def _norm(field, value):
    """Normalize a value for comparison."""
    if value is None or value == "":
        return None
    if field == "queue":
        return str(value).strip().lower()
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def agreement(hard_totals, soft):
    """Return {field: status} where status is match, mismatch, hard_only, soft_only, or none."""
    out = {}
    for field in OVERLAP:
        h, s = _norm(field, hard_totals.get(field)), _norm(field, soft.get(field))
        if h is None and s is None:
            out[field] = "none"
        elif h is None:
            out[field] = "soft_only"
        elif s is None:
            out[field] = "hard_only"
        else:
            out[field] = "match" if h == s else "mismatch"
    return out
