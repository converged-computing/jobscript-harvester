#!/usr/bin/env python3
"""Count shape motifs and compare them across managers and across what scripts run.

    python3 experiments/shapes/analyze.py        # reads data/shapes.csv.gz, writes results/

Counts are given two ways. "scripts" counts unique scripts. "repo weight" gives every
repository one vote per manager, split evenly over its scripts, so a repository with
thousands of generated scripts counts the same as one with a single script. A file
copied verbatim into many repositories counts as at most one repository.

Motifs come at three levels: topology, binned (the jobspec tree with bucketed counts),
and neutral (bucketed totals of groups, tasks, cores and GPUs), which is the one to use
across managers because it does not depend on how a manager spells the request.
"""

import argparse
import csv
import gzip
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from shape import bin_count  # noqa: E402

MANAGERS = ["slurm", "pbs", "lsf", "flux", "oar", "cobalt"]
NONE = "(no resources requested)"
csv.field_size_limit(1 << 30)


def load(path):
    rows = []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            row.pop("jobspec")
            row["repos"] = row["repos"].split()
            for field in ("groups", "tasks", "cores", "gpus", "memory_mib", "duration"):
                row[field] = float(row[field]) if row[field] else None
            rows.append(row)
    return rows


def add_weights(rows):
    """Per row: {repo: weight}, each (repo, manager) summing to 1."""
    per_repo = Counter()
    for row in rows:
        for repo in row["repos"]:
            per_repo[repo, row["manager"]] += 1
    for row in rows:
        # A file copied verbatim into many repositories counts as at most one.
        row["weight"] = min(1.0, sum(1.0 / per_repo[repo, row["manager"]] for repo in row["repos"]))
        row["neutral"] = neutral_key(row)
    return {m: sum(1 for (_, mm) in per_repo if mm == m) for m in MANAGERS}


def neutral_key(row):
    """Totals only, so a PBS chunk request and a Slurm node request can be the same motif."""
    if row["binned"] == NONE:
        return NONE
    part = lambda name, field: f"{name}[{bin_count(int(row[field])) if row[field] else '*'}]"
    return " ".join([part("groups", "groups"), part("tasks", "tasks"), part("cores", "cores"),
                     part("gpus", "gpus")])


def write(name, header, rows, out):
    path = os.path.join(out, name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    print(f"  {name}: {len(rows):,} rows", file=sys.stderr)


def js_divergence(p, q):
    """Jensen-Shannon divergence in bits between two count dicts (0 same, 1 disjoint)."""
    sp, sq = sum(p.values()), sum(q.values())
    if not sp or not sq:
        return None
    total = 0.0
    for key in set(p) | set(q):
        a, b = p.get(key, 0) / sp, q.get(key, 0) / sq
        mid = (a + b) / 2
        if a:
            total += 0.5 * a * math.log2(a / mid)
        if b:
            total += 0.5 * b * math.log2(b / mid)
    return total


def weighted_quantile(pairs, q):
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    if not total:
        return None
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= q * total:
            return value
    return pairs[-1][0]


def fmt(x, digits=3):
    if x is None:
        return ""
    return round(x, digits) if isinstance(x, float) else x


# ------------------------------------------------------------- motifs

def motif_tables(rows, repo_totals, out):
    for level in ("topology", "binned", "neutral"):
        scripts, weight, repos = Counter(), Counter(), defaultdict(set)
        for row in rows:
            key = (row[level], row["manager"])
            scripts[key] += 1
            weight[key] += row["weight"]
            repos[key].update(row["repos"])
        motifs = Counter()
        for (motif, _), n in scripts.items():
            motifs[motif] += n
        table = []
        for motif, n in motifs.most_common(None if level == "topology" else 1000):
            line = [motif, n, len(set().union(*(repos[motif, m] for m in MANAGERS if (motif, m) in repos)))]
            for m in MANAGERS:
                share = weight[motif, m] / repo_totals[m] if repo_totals[m] else 0
                line += [scripts[motif, m], len(repos.get((motif, m), ())), fmt(100 * share, 2)]
            table.append(line)
        header = ["motif", "scripts", "repos"]
        for m in MANAGERS:
            header += [f"{m}_scripts", f"{m}_repos", f"{m}_repo_share_pct"]
        write(f"motifs_{level}.csv", header, table, out)


def manager_similarity(rows, out):
    table = []
    dist = {level: defaultdict(Counter) for level in ("topology", "binned", "neutral", "duration_bin")}
    resourced = defaultdict(Counter)
    for row in rows:
        for level in dist:
            dist[level][row["manager"]][row[level]] += row["weight"]
        if row["neutral"] != NONE:
            resourced[row["manager"]][row["neutral"]] += row["weight"]
    for i, a in enumerate(MANAGERS):
        for b in MANAGERS[i + 1:]:
            shared = set(resourced[a]) & set(resourced[b])
            cover_a = sum(resourced[a][k] for k in shared) / sum(resourced[a].values())
            cover_b = sum(resourced[b][k] for k in shared) / sum(resourced[b].values())
            table.append([a, b,
                          fmt(js_divergence(dist["topology"][a], dist["topology"][b])),
                          fmt(js_divergence(dist["binned"][a], dist["binned"][b])),
                          fmt(js_divergence(dist["neutral"][a], dist["neutral"][b])),
                          fmt(js_divergence(resourced[a], resourced[b])),
                          fmt(js_divergence(dist["duration_bin"][a], dist["duration_bin"][b])),
                          len(shared), fmt(100 * cover_a, 1), fmt(100 * cover_b, 1)])
    write("manager_similarity.csv",
          ["a", "b", "js_topology", "js_binned", "js_neutral", "js_neutral_resourced_only",
           "js_duration", "shared_neutral_motifs", "pct_of_a_in_shared", "pct_of_b_in_shared"], table, out)


def manager_distinctive(rows, repo_totals, out, min_repos=5):
    weight, repos = defaultdict(Counter), defaultdict(set)
    for row in rows:
        weight[row["manager"]][row["neutral"]] += row["weight"]
        repos[row["manager"], row["neutral"]].update(row["repos"])
    table = []
    for m in MANAGERS:
        rest_total = sum(repo_totals[o] for o in MANAGERS if o != m)
        scored = []
        for motif, w in weight[m].items():
            if len(repos[m, motif]) < min_repos:
                continue
            rest = sum(weight[o][motif] for o in MANAGERS if o != m)
            p, q = (w + 0.5) / (repo_totals[m] + 1), (rest + 0.5) / (rest_total + 1)
            scored.append((math.log2((p / (1 - p)) / (q / (1 - q))), motif, w, rest))
        for score, motif, w, rest in sorted(scored, reverse=True)[:25]:
            table.append([m, motif, len(repos[m, motif]), fmt(100 * w / repo_totals[m], 2),
                          fmt(100 * rest / rest_total, 2), fmt(score, 2)])
    write("manager_distinctive.csv",
          ["manager", "motif", "repos", "repo_share_pct", "other_managers_share_pct", "log2_odds_ratio"],
          table, out)


def field_summary(rows, out):
    table = []
    for m in MANAGERS:
        mine = [r for r in rows if r["manager"] == m]
        total = sum(r["weight"] for r in mine)
        for field in ("groups", "tasks", "cores", "gpus", "memory_mib", "duration"):
            pairs = [(r[field], r["weight"]) for r in mine
                     if r[field] is not None and (field != "gpus" or r[field] > 0)]
            given = sum(w for _, w in pairs)
            table.append([m, field, len(pairs), fmt(100 * given / total, 1) if total else "",
                          *(fmt(weighted_quantile(pairs, q)) for q in (0.1, 0.25, 0.5, 0.75, 0.9, 0.99))])
    write("fields_by_manager.csv",
          ["manager", "field", "scripts_with_value", "repo_share_pct", "p10", "p25", "p50",
           "p75", "p90", "p99"], table, out)


# ------------------------------------------------------- what scripts run

def tokens_of(row):
    out = [f"run:{t}" for t in set(row["programs"].split()) | set(row["commands"].split())]
    out += [f"module:{t}" for t in row["modules"].split()]
    return out


def token_tables(rows, out, min_repos=30):
    token_repos = defaultdict(set)
    # Files where no directive was read are mostly not jobscripts (docs, generators).
    rows = [row for row in rows if row["n_options"] != "0"]
    for row in rows:
        row["tokens"] = tokens_of(row)
        for token in row["tokens"]:
            token_repos[token].update(row["repos"])
    keep = {t for t, r in token_repos.items() if len(r) >= min_repos}

    baseline = Counter()
    profile, scripts = defaultdict(Counter), Counter()
    by_manager = defaultdict(Counter)
    manager_repos = defaultdict(set)
    values = defaultdict(lambda: defaultdict(list))
    gpu_weight, weight_total = Counter(), Counter()
    for row in rows:
        baseline[row["binned"]] += row["weight"]
        for token in row["tokens"]:
            if token not in keep:
                continue
            scripts[token] += 1
            profile[token][row["binned"]] += row["weight"]
            by_manager[token, row["manager"]][row["neutral"]] += row["weight"]
            manager_repos[token, row["manager"]].update(row["repos"])
            weight_total[token] += row["weight"]
            if row["gpus"]:
                gpu_weight[token] += row["weight"]
            for field in ("groups", "tasks", "cores", "duration"):
                if row[field] is not None:
                    values[token][field].append((row[field], row["weight"]))
    base_total = sum(baseline.values())

    def entropy(counter):
        total = sum(counter.values())
        h = -sum(v / total * math.log2(v / total) for v in counter.values() if v)
        return h / math.log2(len(counter)) if len(counter) > 1 else 0.0

    table = []
    for token in sorted(keep, key=lambda t: -len(token_repos[t])):
        prof = profile[token]
        total = sum(prof.values())
        resourced = {k: v for k, v in prof.items() if k != NONE}
        top, top_w = max(resourced.items(), key=lambda kv: kv[1]) if resourced else ("", 0)
        table.append([token, scripts[token], len(token_repos[token]),
                      fmt(100 * prof.get(NONE, 0) / total, 1), top, fmt(100 * top_w / total, 1),
                      fmt(entropy(prof), 3), fmt(js_divergence(prof, baseline), 3),
                      fmt(100 * gpu_weight[token] / weight_total[token], 1),
                      *(fmt(weighted_quantile(values[token][f], 0.5)) for f in
                        ("groups", "tasks", "cores", "duration"))])
    write("tokens.csv",
          ["token", "scripts", "repos", "no_resources_pct", "top_motif", "top_motif_pct",
           "motif_entropy", "js_from_all_scripts", "gpu_pct", "median_groups", "median_tasks",
           "median_cores", "median_duration_s"], table, out)

    # Token and motif pairs that occur together far more than chance.
    pair_repos = defaultdict(set)
    for row in rows:
        if row["binned"] == NONE:
            continue
        for token in row["tokens"]:
            if token in keep:
                pair_repos[token, row["binned"]].update(row["repos"])
    lifts = []
    for (token, motif), repos in pair_repos.items():
        if len(repos) < 10:
            continue
        share = profile[token][motif] / sum(profile[token].values())
        base = baseline[motif] / base_total
        lifts.append([token, motif, len(repos), fmt(100 * share, 1), fmt(100 * base, 2),
                      fmt(share / base, 1)])
    lifts.sort(key=lambda r: -r[5])
    write("token_motif_lift.csv",
          ["token", "motif", "repos", "pct_of_token", "pct_of_all", "lift"], lifts[:2000], out)

    # Tokens whose shape profiles are closest to each other.
    big = [t for t in keep if len(token_repos[t]) >= 100]
    neighbours = []
    for a in big:
        scored = sorted((js_divergence(profile[a], profile[b]), b) for b in big if b != a)[:5]
        neighbours.append([a, len(token_repos[a])] + [x for d, b in scored for x in (b, fmt(d, 3))])
    neighbours.sort(key=lambda r: -r[1])
    write("token_neighbours.csv",
          ["token", "repos"] + [f"{k}{i}" for i in range(1, 6) for k in ("neighbour", "js")],
          neighbours, out)

    # The same token under different managers.
    cross = []
    for token in keep:
        present = [m for m in MANAGERS if len(manager_repos[token, m]) >= 10]
        if len(present) < 2:
            continue
        for m in present:
            prof = by_manager[token, m]
            total = sum(prof.values())
            resourced = {k: v for k, v in prof.items() if k != NONE}
            top, top_w = max(resourced.items(), key=lambda kv: kv[1]) if resourced else ("", 0)
            others = [js_divergence(prof, by_manager[token, o]) for o in present if o != m]
            cross.append([token, m, len(manager_repos[token, m]), top, fmt(100 * top_w / total, 1),
                          fmt(statistics.mean(others), 3)])
    cross.sort(key=lambda r: (r[0], MANAGERS.index(r[1])))
    write("token_by_manager.csv",
          ["token", "manager", "repos", "top_motif", "top_motif_pct", "mean_js_to_other_managers"],
          cross, out)


def summary(rows, repo_totals, out):
    info = {"shapes": len(rows), "managers": {}}
    for m in MANAGERS:
        mine = [r for r in rows if r["manager"] == m]
        if not mine:
            continue
        n = len(mine)
        counts = Counter()
        for r in mine:
            counts["no_options"] += r["n_options"] == "0"
            counts["no_resources"] += r["binned"] == NONE
            counts["slot_" + r["slot"]] += r["binned"] != NONE
            for name in r["specified"].split():
                counts["specified_" + name] += 1
            for name in r["assumed"].split():
                counts["assumed_" + name] += r["binned"] != NONE
            for name in r["flags"].split():
                counts["flag_" + name] += 1
            counts["runs_something"] += bool(r["programs"] or r["commands"])
            counts["uses_launcher"] += bool(r["launchers"])
            counts["loads_modules"] += bool(r["modules"])
        info["managers"][m] = {
            "scripts": n, "repos": repo_totals[m],
            "distinct_exact": len({r["exact"] for r in mine}),
            "distinct_binned": len({r["binned"] for r in mine}),
            "distinct_topology": len({r["topology"] for r in mine}),
            "pct": {k: round(100 * v / n, 1) for k, v in sorted(counts.items())},
        }
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(info, handle, indent=1)
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--shapes", default=os.path.join(HERE, "data", "shapes.csv.gz"))
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    rows = load(args.shapes)
    repo_totals = add_weights(rows)
    print(f"{len(rows):,} shapes", file=sys.stderr)
    summary(rows, repo_totals, args.out)
    motif_tables(rows, repo_totals, args.out)
    manager_similarity(rows, args.out)
    manager_distinctive(rows, repo_totals, args.out)
    field_summary(rows, args.out)
    token_tables(rows, args.out)


if __name__ == "__main__":
    main()
