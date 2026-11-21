#!/usr/bin/env python3
"""Write docs/data/shapes.json, the data behind docs/shapes.html, from results/.

    python3 experiments/shapes/site.py
"""

import argparse
import csv
import datetime
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
MANAGERS = ["slurm", "pbs", "lsf", "flux", "oar", "cobalt"]


def table(results, name):
    with open(os.path.join(results, name), encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def num(text):
    if text in ("", None):
        return None
    value = float(text)
    return int(value) if value == int(value) else value


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", default=os.path.join(HERE, "results"))
    ap.add_argument("--out", default=os.path.join(HERE, "..", "..", "docs", "data", "shapes.json"))
    ap.add_argument("--top", type=int, default=15, help="motifs kept per manager and level")
    ap.add_argument("--min-repos", type=int, default=50, help="programs and modules kept")
    args = ap.parse_args()

    with open(os.path.join(args.results, "summary.json"), encoding="utf-8") as handle:
        summary = json.load(handle)

    motifs = {}
    for level in ("binned", "neutral"):
        rows = table(args.results, f"motifs_{level}.csv")
        motifs[level] = {}
        for m in MANAGERS:
            ranked = sorted(rows, key=lambda r: -float(r[f"{m}_repo_share_pct"]))[:args.top]
            motifs[level][m] = [[r["motif"], num(r[f"{m}_repo_share_pct"]), num(r[f"{m}_repos"]),
                                 num(r[f"{m}_scripts"])] for r in ranked
                                if float(r[f"{m}_repo_share_pct"]) > 0]
    topology = [{"motif": r["motif"], "scripts": num(r["scripts"]),
                 "share": {m: num(r[f"{m}_repo_share_pct"]) for m in MANAGERS}}
                for r in table(args.results, "motifs_topology.csv") if int(r["repos"]) >= 20]

    fields = {m: {} for m in MANAGERS}
    for r in table(args.results, "fields_by_manager.csv"):
        fields[r["manager"]][r["field"]] = {k: num(r[k]) for k in
                                            ("repo_share_pct", "p25", "p50", "p75", "p90")}

    similarity = [{"a": r["a"], "b": r["b"], "resources": num(r["js_neutral"]),
                   "duration": num(r["js_duration"]), "shared": num(r["shared_neutral_motifs"])}
                  for r in table(args.results, "manager_similarity.csv")]

    distinctive = {m: [] for m in MANAGERS}
    for r in table(args.results, "manager_distinctive.csv"):
        if len(distinctive[r["manager"]]) < 8:
            distinctive[r["manager"]].append([r["motif"], num(r["repos"]), num(r["repo_share_pct"]),
                                              num(r["other_managers_share_pct"])])

    tokens = [[r["token"], num(r["repos"]), num(r["scripts"]), r["top_motif"], num(r["top_motif_pct"]),
               num(r["js_from_all_scripts"]), num(r["gpu_pct"]), num(r["median_groups"]),
               num(r["median_tasks"]), num(r["median_cores"]), num(r["median_duration_s"])]
              for r in table(args.results, "tokens.csv") if int(r["repos"]) >= args.min_repos]
    kept = {t[0] for t in tokens}
    neighbours = {r["token"]: [[r[f"neighbour{i}"], num(r[f"js{i}"])] for i in range(1, 6)]
                  for r in table(args.results, "token_neighbours.csv") if r["token"] in kept}
    by_manager = {}
    for r in table(args.results, "token_by_manager.csv"):
        if r["token"] in kept:
            by_manager.setdefault(r["token"], []).append(
                [r["manager"], num(r["repos"]), r["top_motif"], num(r["top_motif_pct"])])

    resources = {m: {} for m in MANAGERS}
    for r in table(args.results, "resource_counts.csv"):
        entry = resources[r["manager"]].setdefault(r["resource"], {"stated": [0, 0], "bins": []})
        pair = [num(r["scripts"]), num(r["repos"])]
        if r["range"] == "(stated)":
            entry["stated"] = pair
        else:
            entry["bins"].append([r["range"], *pair])
    attributes = {m: [] for m in MANAGERS}
    for r in table(args.results, "attributes.csv"):
        attributes[r["manager"]].append([r["attribute"], num(r["scripts"]), num(r["repos"])])
    values = {m: {} for m in MANAGERS}
    for r in table(args.results, "attribute_values.csv"):
        rows = values[r["manager"]].setdefault(r["attribute"], [])
        if len(rows) < args.top:
            rows.append([r["value"], num(r["scripts"]), num(r["repos"])])

    data = {"generated": datetime.date.today().isoformat(), "shapes": summary["shapes"],
            "managers": summary["managers"], "motifs": motifs, "topology": topology,
            "fields": fields, "similarity": similarity, "distinctive": distinctive,
            "tokens": tokens, "neighbours": neighbours, "by_manager": by_manager,
            "resources": resources, "attributes": attributes, "values": values,
            "min_repos": args.min_repos}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"))
    print(f"wrote {os.path.relpath(args.out)} ({os.path.getsize(args.out) // 1024} KB, "
          f"{len(tokens)} programs and modules)")


if __name__ == "__main__":
    main()
