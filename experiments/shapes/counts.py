#!/usr/bin/env python3
"""Count how much of each resource scripts ask for, and which attributes they set.

    python3 experiments/shapes/counts.py         # reads data/shapes.csv.gz, writes results/

Every count is given as unique scripts and as repositories with at least one such
script. A value is counted only when the script states it; assumed counts are left out.
"""

import argparse
import csv
import gzip
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from shape import DURATIONS, bin_count, bin_memory, bin_duration  # noqa: E402

MANAGERS = ["slurm", "pbs", "lsf", "flux", "oar", "cobalt"]
csv.field_size_limit(1 << 30)

COUNT_BINS = ["1", "2"] + [f"{2 ** i + 1}-{2 ** (i + 1)}" for i in range(1, 10)] + ["1025+"]
MEMORY_BINS = [f"<={g}G" for g in (1, 2, 4, 8, 16, 32, 64, 128, 256)] + [">256G"]
DURATION_BINS = [name for _, name in DURATIONS] + [">7d"]
RESOURCES = {  # column -> (bins, how to bin a value)
    "groups": (COUNT_BINS, lambda v: bin_count(int(v))),
    "tasks": (COUNT_BINS, lambda v: bin_count(int(v))),
    "cores": (COUNT_BINS, lambda v: bin_count(int(v))),
    "gpus": (COUNT_BINS, lambda v: bin_count(int(v))),
    "memory_mib": (MEMORY_BINS, bin_memory),
    "duration": (DURATION_BINS, bin_duration),
    "array_count": (COUNT_BINS, lambda v: bin_count(int(v))),
}
VALUES = ["queue", "qos", "constraint", "gpu_types", "memory_per", "dependency"]
SEVERAL = {"gpu_types", "memory_per"}       # columns that hold a list


class Tally:
    """Unique scripts and distinct repositories per key."""

    def __init__(self):
        self.scripts, self.repos = Counter(), defaultdict(set)

    def add(self, key, repos):
        self.scripts[key] += 1
        self.repos[key].update(repos)

    def get(self, key):
        return self.scripts[key], len(self.repos[key])


def write(out, name, header, rows):
    with open(os.path.join(out, name), "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    print(f"  {name}: {len(rows):,} rows", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--shapes", default=os.path.join(HERE, "data", "shapes.csv.gz"))
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    ap.add_argument("--min-repos", type=int, default=5, help="attribute values kept")
    args = ap.parse_args()

    totals, bins, stated, attrs, values = Tally(), Tally(), Tally(), Tally(), Tally()
    with gzip.open(args.shapes, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            m, repos = row["manager"], row["repos"].split()
            totals.add(m, repos)
            for field, (_, to_bin) in RESOURCES.items():
                value = float(row[field]) if row[field] else 0
                if value > 0:
                    stated.add((m, field), repos)
                    bins.add((m, field, to_bin(value)), repos)
            for name in row["attributes"].split():
                attrs.add((m, name), repos)
            for field in VALUES:
                found = row[field].split() if field in SEVERAL else [row[field]]
                for value in found:
                    if value:
                        values.add((m, field, value), repos)

    os.makedirs(args.out, exist_ok=True)
    pct = lambda n, total: round(100 * n / total, 2) if total else 0
    rows = []
    for m in MANAGERS:
        scripts, repos = totals.get(m)
        for field, (names, _) in RESOURCES.items():
            s, r = stated.get((m, field))
            rows.append([m, field, "(stated)", s, r, pct(s, scripts), pct(r, repos)])
            for name in names:
                s, r = bins.get((m, field, name))
                rows.append([m, field, name, s, r, pct(s, scripts), pct(r, repos)])
    write(args.out, "resource_counts.csv",
          ["manager", "resource", "range", "scripts", "repos", "script_pct", "repo_pct"], rows)

    rows = []
    for m in MANAGERS:
        scripts, repos = totals.get(m)
        for (mm, name) in sorted(attrs.scripts, key=lambda k: -len(attrs.repos[k])):
            if mm == m:
                s, r = attrs.get((m, name))
                rows.append([m, name, s, r, pct(s, scripts), pct(r, repos)])
    write(args.out, "attributes.csv",
          ["manager", "attribute", "scripts", "repos", "script_pct", "repo_pct"], rows)

    rows = []
    for key in sorted(values.scripts, key=lambda k: (MANAGERS.index(k[0]), k[1], -len(values.repos[k]))):
        s, r = values.get(key)
        if r >= args.min_repos:
            scripts, repos = totals.get(key[0])
            rows.append([*key, s, r, pct(s, scripts), pct(r, repos)])
    write(args.out, "attribute_values.csv",
          ["manager", "attribute", "value", "scripts", "repos", "script_pct", "repo_pct"], rows)


if __name__ == "__main__":
    main()
