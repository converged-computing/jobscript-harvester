#!/usr/bin/env python3
"""Write the data files behind the GitHub Pages site in docs/.

    python3 build_docs.py [--repo-url URL] [--data-url URL]

docs/data/stats.json holds every figure and chart on the page; docs/data/sample-<manager>.json
holds a deterministic sample of scripts for the in-page browser (emails removed).
"""

import argparse
import json
import os
import time
from collections import Counter

from dataset import EMAIL, EMAIL_MARK, flat_row, open_ro, permalink

MANAGERS = ["slurm", "pbs", "lsf", "flux", "oar", "cobalt"]
WALLTIME = [("up to 1 h", 3600), ("1-4 h", 4 * 3600), ("4-12 h", 12 * 3600), ("12-24 h", 86400),
            ("1-3 d", 3 * 86400), ("3-7 d", 7 * 86400), ("over 7 d", float("inf"))]
NODES = [("1", 1), ("2-4", 4), ("5-16", 16), ("17-64", 64), ("65-256", 256), ("over 256", float("inf"))]
COVERAGE = ["walltime", "nodes", "tasks", "cpus_per_task", "memory", "gpu", "queue", "account",
            "array", "job_name"]


def bucket(value, edges):
    """Return the label of the first bucket whose upper edge holds value."""
    return next(label for label, upper in edges if value <= upper)


def capped_leaves(db):
    """Count saturated queries with no further split, using the project's own plan walker."""
    try:
        import sys
        import types
        sys.modules.setdefault("requests", types.ModuleType("requests"))
        from harvest import without_license_search
        from websearch import frontier
        with open("plan.json") as fh:
            return frontier(db, without_license_search(json.load(fh)))["truncated"]
    except Exception:  # the site still builds without this one figure
        return None


def corpus_stats(db):
    """Return totals, the pipeline funnel, and corpus breakdowns."""
    q = lambda sql: db.execute(sql).fetchall()
    one = lambda sql: db.execute(sql).fetchone()[0]
    totals = {
        "queries": one("SELECT COUNT(*) FROM queries WHERE status IN ('done','empty')"),
        "queries_with_results": one("SELECT COUNT(*) FROM queries WHERE status='done'"),
        "queries_empty": one("SELECT COUNT(*) FROM queries WHERE status='empty'"),
        "queries_saturated": one("SELECT COALESCE(SUM(saturated),0) FROM queries"),
        "capped_leaves": capped_leaves(db),
        "hits": one("SELECT COUNT(*) FROM search_hits"),
        "repos_discovered": one("SELECT COUNT(DISTINCT repo) FROM repo_sources WHERE source='websearch'"),
        "repos_scanned": one("SELECT COUNT(*) FROM scans"),
        "repos_scan_failed": one("SELECT COUNT(*) FROM scans WHERE status != 'ok'"),
        "repos_with_scripts": one("SELECT COUNT(*) FROM scans WHERE kept > 0"),
        "files_read": one("SELECT COALESCE(SUM(blobs_read),0) FROM scans"),
        "occurrences": one("SELECT COUNT(*) FROM occurrences"),
        "scripts": one("SELECT COUNT(*) FROM blobs"),
        "bytes": one("SELECT SUM(size) FROM blobs"),
        "label_rows": one("SELECT COUNT(*) FROM labels"),
        "repos_with_metadata": one("SELECT COUNT(*) FROM repos WHERE status='ok'"),
        "repos_with_license": one("SELECT COUNT(*) FROM repos WHERE status='ok' AND license IS NOT NULL "
                                  "AND license != 'NOASSERTION'"),
        "legacy_unscanned": one("""SELECT COUNT(DISTINCT s.repo) FROM repo_sources s
            LEFT JOIN scans x ON x.repo = s.repo WHERE x.repo IS NULL AND s.source='jobspec-database'"""),
    }
    funnel = [
        ["Search queries run", totals["queries"]], ["Search hits", totals["hits"]],
        ["Repositories discovered", totals["repos_discovered"]],
        ["Repositories scanned", totals["repos_scanned"]],
        ["Repositories with jobscripts", totals["repos_with_scripts"]],
        ["Jobscript files kept", totals["occurrences"]], ["Unique jobscripts", totals["scripts"]]]
    by_manager = Counter()
    bytes_by_manager = Counter()
    multi = 0
    for managers, count, size in q("SELECT managers, COUNT(*), SUM(size) FROM blobs GROUP BY 1"):
        parts = managers.split(",")
        multi += count if len(parts) > 1 else 0
        for part in parts:
            by_manager[part] += count
            bytes_by_manager[part] += size
    search = q("""SELECT q.directive, COUNT(*), SUM(q.status='done'), SUM(q.status='empty'),
                         SUM(q.saturated), SUM(q.results) FROM queries q
                  WHERE q.status IN ('done','empty') GROUP BY 1""")
    repos_by_directive = dict(q("""SELECT q.directive, COUNT(DISTINCT h.repo) FROM search_hits h
                                   JOIN queries q ON q.query = h.query GROUP BY 1"""))
    batches = q("""WITH first AS (SELECT repo, MIN(rowid) AS hit FROM search_hits GROUP BY repo)
                   SELECT (q.rowid - 1) / 100, COUNT(*) FROM first f
                   JOIN search_hits h ON h.rowid = f.hit JOIN queries q ON q.query = h.query
                   GROUP BY 1 ORDER BY 1""")
    discovery, running = [[0, 0]], 0
    for batch, new in batches:
        running += new
        discovery.append([(batch + 1) * 100, running])
    return {
        "totals": totals, "funnel": funnel, "multi_manager_scripts": multi,
        "managers": [{"manager": m, "scripts": by_manager[m], "bytes": bytes_by_manager[m]}
                     for m in MANAGERS],
        "search": [{"directive": d, "queries": n, "with_results": done, "empty": empty,
                    "saturated": sat, "hits": hits, "repos": repos_by_directive.get(d, 0)}
                   for d, n, done, empty, sat, hits in sorted(search, key=lambda r: -r[1])],
        "discovery": discovery,
        "sizes": q("""SELECT CASE WHEN size<512 THEN 'under 512 B' WHEN size<2048 THEN '512 B-2 KB'
                      WHEN size<8192 THEN '2-8 KB' WHEN size<32768 THEN '8-32 KB'
                      WHEN size<131072 THEN '32-128 KB' ELSE 'over 128 KB' END b, COUNT(*)
                      FROM blobs GROUP BY b ORDER BY MIN(size)"""),
        "scripts_per_repo": q("""SELECT CASE WHEN kept=0 THEN '0' WHEN kept=1 THEN '1' WHEN kept<=5 THEN '2-5'
                      WHEN kept<=20 THEN '6-20' WHEN kept<=100 THEN '21-100'
                      WHEN kept<=1000 THEN '101-1,000' ELSE 'over 1,000' END b, COUNT(*), SUM(kept)
                      FROM scans WHERE status='ok' GROUP BY b ORDER BY MIN(kept)"""),
        "duplication": q("""SELECT CASE WHEN c=1 THEN '1' WHEN c=2 THEN '2' WHEN c<=5 THEN '3-5'
                      WHEN c<=20 THEN '6-20' ELSE 'over 20' END b, COUNT(*)
                      FROM (SELECT COUNT(*) c FROM occurrences GROUP BY blob_sha) GROUP BY b ORDER BY MIN(c)"""),
        "extensions": q("""SELECT CASE WHEN instr(replace(path,'/','_'),'.')=0 THEN '(none)'
                      ELSE '.' || lower(replace(path, rtrim(path, replace(path,'.','')), '')) END ext, COUNT(*)
                      FROM occurrences GROUP BY ext ORDER BY 2 DESC LIMIT 15"""),
        "licenses": q("""SELECT COALESCE(r.license,'(no license)'), COUNT(DISTINCT r.repo)
                      FROM repos r JOIN occurrences o ON o.repo = r.repo WHERE r.status='ok'
                      GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""),
        "top_repos": q("SELECT repo, kept FROM scans WHERE status='ok' ORDER BY kept DESC LIMIT 15"),
    }


def label_stats(db):
    """Return per-manager label coverage, distributions, and the most used directive options."""
    out = {m: {"rows": 0, "coverage": Counter(), "walltime": Counter(), "nodes": Counter(),
               "options": Counter(), "with_errors": 0, "with_unknown": 0} for m in MANAGERS}
    for manager, labels, totals in db.execute("SELECT manager, labels, totals FROM labels"):
        labels, totals = json.loads(labels), json.loads(totals)
        flat, stats = flat_row(labels, totals), out[manager]
        stats["rows"] += 1
        present = {"walltime": flat["walltime_seconds"] is not None, "nodes": labels["nodes"] is not None,
                   "tasks": flat["tasks"] is not None, "cpus_per_task": flat["cpus_per_task"] is not None,
                   "memory": flat["memory_mib"] is not None, "gpu": bool(flat["gpu_request"]),
                   "queue": flat["queue"] is not None, "account": flat["account"] is not None,
                   "array": labels["array"] is not None, "job_name": flat["job_name"] is not None}
        stats["coverage"].update(k for k, v in present.items() if v)
        if flat["walltime_seconds"] is not None:
            stats["walltime"][bucket(flat["walltime_seconds"], WALLTIME)] += 1
        if flat["nodes"]:
            stats["nodes"][bucket(flat["nodes"], NODES)] += 1
        stats["options"].update({o["option"] for o in labels["options"]})
        stats["with_errors"] += bool(labels["errors"])
        stats["with_unknown"] += bool(labels["unknown"])
    return [{"manager": m, "rows": s["rows"],
             "coverage": [[k, s["coverage"][k]] for k in COVERAGE],
             "walltime": [[label, s["walltime"][label]] for label, _ in WALLTIME],
             "nodes": [[label, s["nodes"][label]] for label, _ in NODES],
             "options": s["options"].most_common(15),
             "with_errors": s["with_errors"], "with_unknown": s["with_unknown"]}
            for m, s in out.items()]


def sample(db, manager, count, max_bytes):
    """Return a deterministic sample of single-manager scripts (blob SHAs are uniformly spread)."""
    licenses = dict(db.execute("SELECT repo, license FROM repos WHERE status='ok'"))
    rows = db.execute("""SELECT blob_sha, size, content FROM blobs
                         WHERE managers = ? AND size <= ? AND blob_sha >= '7' ORDER BY blob_sha LIMIT ?""",
                      (manager, max_bytes, count)).fetchall()
    out = []
    for sha, size, content in rows:
        occ = db.execute("SELECT repo, commit_sha, path FROM occurrences WHERE blob_sha = ? "
                         "ORDER BY repo, path", (sha,)).fetchall()
        label = db.execute("SELECT labels, totals FROM labels WHERE blob_sha = ? AND manager = ?",
                           (sha, manager)).fetchone()
        if not occ or not label or "�" in content:
            continue
        labels, totals = json.loads(EMAIL.sub(EMAIL_MARK, label[0])), json.loads(label[1])
        flat = flat_row(labels, totals)
        repo, commit, path = occ[0]
        out.append({"blob_sha": sha, "manager": manager, "size": size, "repo": repo, "path": path,
                    "commit": commit, "url": permalink(repo, commit, path), "license": licenses.get(repo),
                    "n_occurrences": len(occ),
                    **{k: flat[k] for k in ("nodes", "tasks", "cpus_per_task", "gpus", "gpu_request",
                                            "memory_mib", "memory_per", "walltime_seconds", "queue",
                                            "array_count")},
                    "options": [[o["option"], o["value"]] for o in labels["options"]],
                    "content": EMAIL.sub(EMAIL_MARK, content)})
    return out


def main():
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default="harvest.sqlite")
    parser.add_argument("--out", default="docs")
    parser.add_argument("--repo-url", help="link to the source repository shown on the page")
    parser.add_argument("--data-url", help="link to the full dataset download shown on the page")
    parser.add_argument("--sample", type=int, default=250, help="sample scripts per manager")
    parser.add_argument("--sample-max-bytes", type=int, default=6000)
    args = parser.parse_args()
    db = open_ro(args.db)
    data_dir = os.path.join(args.out, "data")
    os.makedirs(data_dir, exist_ok=True)
    stats = corpus_stats(db)
    stats["labels"] = label_stats(db)
    stats["generated"] = time.strftime("%Y-%m-%d", time.gmtime())
    stats["links"] = {"repo": args.repo_url, "data": args.data_url}
    stats["sample"] = {"per_manager": {}, "max_bytes": args.sample_max_bytes}
    for manager in MANAGERS:
        rows = sample(db, manager, args.sample, args.sample_max_bytes)
        stats["sample"]["per_manager"][manager] = len(rows)
        with open(os.path.join(data_dir, f"sample-{manager}.json"), "w") as fh:
            json.dump(rows, fh, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(data_dir, "stats.json"), "w") as fh:
        json.dump(stats, fh, ensure_ascii=False, separators=(",", ":"))
    open(os.path.join(args.out, ".nojekyll"), "w").close()
    print("wrote", ", ".join(sorted(os.listdir(data_dir))))


if __name__ == "__main__":
    main()
