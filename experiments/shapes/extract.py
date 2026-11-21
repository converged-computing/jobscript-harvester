#!/usr/bin/env python3
"""Write one shape per (script, manager) from the dataset export.

    python3 experiments/shapes/extract.py            # reads dataset/, writes data/shapes.csv.gz

The output has one row per label set with the neutral totals, the three shape keys,
the jobspec as JSON, the attributes the script sets, and what the script runs (see context.py).
"""

import argparse
import csv
import glob
import gzip
import json
import multiprocessing
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import context  # noqa: E402
import shape  # noqa: E402

COLUMNS = ["blob_sha", "manager", "repos", "n_options", "specified", "assumed", "flags", "slot",
           "groups", "tasks", "cores", "gpus", "memory_mib", "duration", "duration_bin",
           "exclusive", "array_count", "topology", "binned", "exact",
           "attributes", "queue", "qos", "constraint", "gpu_types", "memory_per", "dependency",
           "launchers", "programs", "commands", "modules", "jobspec"]

# Label fields that are not resources; "attributes" lists the ones a script sets.
ATTRIBUTES = ["walltime_seconds", "job_name", "queue", "account", "qos", "constraint", "array",
              "dependency", "exclusive", "email"]


def text(value):
    """One short line for a label value."""
    return " ".join(str(value).split())[:60] if value else ""


def attributes(labels):
    dependency = text(labels.get("dependency")).split(":", 1)[0].lower()
    return {
        "attributes": " ".join(a for a in ATTRIBUTES if labels.get(a)),
        "queue": text(labels.get("queue")), "qos": text(labels.get("qos")),
        "constraint": text(labels.get("constraint")),
        "gpu_types": " ".join(sorted({text(g["type"]).lower().replace(" ", "")
                                      for g in labels.get("gpus") or [] if g.get("type")})),
        "memory_per": " ".join(sorted({str(m["per"]) for m in labels.get("memory") or []
                                       if m.get("per")})),
        "dependency": "job id" if dependency.isdigit() else dependency,
    }


def shard_rows(path):
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            ctx = context.context(record["content"])
            repos = sorted({o["repo"] for o in record["occurrences"]})
            for manager, labels in record["labels"].items():
                s = shape.shape(labels)
                row = {
                    "blob_sha": record["blob_sha"], "manager": manager,
                    "repos": " ".join(repos), "n_options": len(labels["options"]),
                    "specified": " ".join(s["specified"]), "assumed": " ".join(s["assumed"]),
                    "flags": " ".join(s["flags"]), "exclusive": int(s["exclusive"]),
                    "jobspec": shape.dumps(s["jobspec"]),
                }
                row.update(attributes(labels))
                for field in ("slot", "groups", "tasks", "cores", "gpus", "memory_mib",
                              "duration", "duration_bin", "array_count", "topology",
                              "binned", "exact"):
                    row[field] = "" if s[field] is None else s[field]
                for field in ("launchers", "programs", "commands", "modules"):
                    row[field] = " ".join(ctx[field])
                rows.append([row[c] for c in COLUMNS])
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", default=os.path.join(HERE, "..", "..", "dataset"))
    ap.add_argument("--out", default=os.path.join(HERE, "data", "shapes.csv.gz"))
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    args = ap.parse_args()

    shards = sorted(glob.glob(os.path.join(args.dataset, "scripts", "scripts-*.jsonl.gz")))
    if not shards:
        sys.exit(f"no script shards under {args.dataset}; run dataset.py first")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    total = 0
    with gzip.open(args.out, "wt", encoding="utf-8", newline="") as handle, \
            multiprocessing.Pool(args.workers) as pool:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for n, rows in enumerate(pool.imap(shard_rows, shards), 1):
            writer.writerows(rows)
            total += len(rows)
            print(f"\r{n}/{len(shards)} shards, {total:,} shapes", end="", file=sys.stderr)
    print(f"\nwrote {total:,} shapes to {os.path.relpath(args.out)}", file=sys.stderr)


if __name__ == "__main__":
    main()
