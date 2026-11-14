#!/usr/bin/env python3
"""Export the corpus as sharded, self-describing files for publication and analysis.

Output (default ./dataset):
  scripts/scripts-XX.jsonl.gz  one JSON record per unique jobscript, sharded by the first two
                               hex digits of its git blob SHA (256 files)
  index.csv.gz                 one row per (script, manager) with flat metadata and parsed labels,
                               without content, for quick analysis
  repos.csv.gz                 one row per discovered repository
  manifest.json                counts, schema, and a SHA-256 for every file
  README.md                    dataset card
"""

import argparse
import csv
import gzip
import hashlib
import io
import itertools
import json
import os
import re
import sqlite3
import time
from urllib.parse import quote

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
EMAIL_MARK = "[email removed]"

# Flat label columns shared by index.csv.gz and the explorer index.
FLAT = ["nodes", "tasks", "tasks_per_node", "cpus_per_task", "gpus", "gpu_request",
        "memory_mib", "memory_per", "walltime_seconds", "queue", "account", "qos", "job_name",
        "constraint", "array_count", "exclusive", "n_options", "n_unknown", "n_errors",
        "ignored_directive_lines"]

SCRIPT_FIELDS = {
    "blob_sha": "git blob SHA-1 of the file content; the script's identity",
    "size": "content size in bytes",
    "managers": "workload managers whose directives were detected (slurm, pbs, lsf, cobalt, oar, flux)",
    "directive_lines": "number of directive lines detected per manager",
    "signals": "raw detection counts from detect.py, including flux command lines",
    "content": "the jobscript text (UTF-8, undecodable bytes replaced)",
    "n_occurrences": "number of (repository, path) places this exact content was found",
    "occurrences": "list of {repo, commit, path, url, source, license}; url is a permalink at the scanned commit",
    "labels": "per manager: every directive option read plus normalized fields, from parse.py",
    "totals": "per manager: job-level values that follow from the directives without assumptions",
}


def open_ro(path):
    """Open an SQLite database read-only."""
    return sqlite3.connect(f"file:{quote(os.path.abspath(path))}?mode=ro", uri=True)


def permalink(repo, commit, path):
    """Return the GitHub URL of a file at a pinned commit."""
    return f"https://github.com/{repo}/blob/{commit or 'HEAD'}/{quote(path)}"


def flat_row(labels, totals):
    """Return the FLAT columns for one manager's labels and totals."""
    memory = labels.get("memory") or []
    return {
        "nodes": totals.get("nodes"), "tasks": totals.get("tasks"),
        "tasks_per_node": labels.get("tasks_per_node"), "cpus_per_task": labels.get("cpus_per_task"),
        "gpus": totals.get("gpus"), "gpu_request": int(bool(labels.get("gpus"))),
        "memory_mib": memory[0].get("mib") if memory else None,
        "memory_per": memory[0].get("per") if memory else None,
        "walltime_seconds": totals.get("walltime_seconds"), "queue": totals.get("queue"),
        "account": labels.get("account"), "qos": labels.get("qos"), "job_name": labels.get("job_name"),
        "constraint": labels.get("constraint"), "array_count": totals.get("array_count"),
        "exclusive": int(bool(labels.get("exclusive"))), "n_options": len(labels.get("options") or []),
        "n_unknown": len(labels.get("unknown") or []), "n_errors": len(labels.get("errors") or []),
        "ignored_directive_lines": labels.get("ignored_directive_lines"),
    }


class _Groups:
    """Step through a cursor ordered by blob_sha, handing out the rows of one SHA at a time."""

    def __init__(self, cursor):
        self.groups = itertools.groupby(cursor, key=lambda row: row[0])
        self.key, self.rows = None, []
        self._advance()

    def _advance(self):
        try:
            self.key, rows = next(self.groups)
            self.rows = list(rows)
        except StopIteration:
            self.key, self.rows = None, []

    def take(self, sha):
        while self.key is not None and self.key < sha:
            self._advance()
        if self.key == sha:
            rows = self.rows
            self._advance()
            return rows
        return []


def records(db_path, with_content=True, scrub_emails=False):
    """Yield one dict per unique jobscript, in blob SHA order, with occurrences and labels."""
    meta = open_ro(db_path)
    licenses = dict(meta.execute("SELECT repo, license FROM repos WHERE status='ok'"))
    meta.close()
    content = "content" if with_content else "NULL"
    blobs = open_ro(db_path).execute(
        f"SELECT blob_sha, size, {content}, signals, managers FROM blobs ORDER BY blob_sha")
    occurrences = _Groups(open_ro(db_path).execute(
        "SELECT blob_sha, repo, commit_sha, path, source FROM occurrences ORDER BY blob_sha, repo, path"))
    labels = _Groups(open_ro(db_path).execute(
        "SELECT blob_sha, manager, labels, totals FROM labels ORDER BY blob_sha, manager"))
    for sha, size, text, signals, managers in blobs:
        signals = json.loads(signals)
        occ = [{"repo": r, "commit": c, "path": p, "url": permalink(r, c, p), "source": s,
                "license": licenses.get(r)} for _, r, c, p, s in occurrences.take(sha)]
        label_rows = labels.take(sha)
        if scrub_emails:
            text = EMAIL.sub(EMAIL_MARK, text) if text is not None else None
            label_rows = [(s, m, EMAIL.sub(EMAIL_MARK, l), t) for s, m, l, t in label_rows]
        record = {
            "blob_sha": sha, "size": size, "managers": managers.split(","),
            "directive_lines": {k[:-len("_directive")]: v for k, v in signals.items()
                                if k.endswith("_directive")},
            "signals": signals, "content": text, "n_occurrences": len(occ), "occurrences": occ,
            "labels": {m: json.loads(l) for _, m, l, _ in label_rows},
            "totals": {m: json.loads(t) for _, m, _, t in label_rows},
        }
        if not with_content:
            del record["content"]
        yield record


def index_rows(record):
    """Yield one flat row per manager of a script record (first occurrence as its location)."""
    first = record["occurrences"][0] if record["occurrences"] else {}
    for manager in record["managers"]:
        row = {"blob_sha": record["blob_sha"], "manager": manager, "size": record["size"],
               "directive_lines": record["directive_lines"].get(manager),
               "n_occurrences": record["n_occurrences"], "repo": first.get("repo"),
               "path": first.get("path"), "commit": first.get("commit"),
               "license": first.get("license")}
        labels = record["labels"].get(manager)
        row.update(flat_row(labels, record["totals"][manager]) if labels else dict.fromkeys(FLAT))
        yield row


INDEX_COLUMNS = ["blob_sha", "manager", "size", "directive_lines", "n_occurrences", "repo", "path",
                 "commit", "license"] + FLAT


def _gzip_writer(path):
    """Open a text writer producing a reproducible gzip file."""
    raw = open(path, "wb")
    gz = gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0)
    return io.TextIOWrapper(gz, encoding="utf-8", newline=""), gz, raw


def _close(handles):
    for handle in handles:
        handle.close()


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_repos(db_path, path):
    """Write one row per discovered repository; return the row count."""
    db = open_ro(db_path)
    rows = db.execute("""
        SELECT s.repo, GROUP_CONCAT(DISTINCT s.source), r.status, r.license, r.fork, r.archived,
               r.stars, r.default_branch, r.size_kb, r.pushed_at,
               x.status, x.commit_sha, x.blobs_read, x.blobs_skipped, x.kept
        FROM repo_sources s LEFT JOIN repos r ON r.repo = s.repo LEFT JOIN scans x ON x.repo = s.repo
        GROUP BY s.repo ORDER BY s.repo""")
    handles = _gzip_writer(path)
    writer = csv.writer(handles[0])
    writer.writerow(["repo", "sources", "metadata_status", "license", "fork", "archived", "stars",
                     "default_branch", "size_kb", "pushed_at", "scan_status", "scan_commit",
                     "files_read", "files_skipped", "jobscripts_kept"])
    count = 0
    for row in rows:
        writer.writerow(row)
        count += 1
    _close(handles)
    return count


CARD = """# Jobscript corpus

{scripts:,} unique workload-manager jobscripts ({occurrences:,} file occurrences) from {repos_with_scripts:,}
public GitHub repositories, each with its content, a permalink to the scanned commit, and labels
parsed from its directives. Built by [jobscript-harvester](../README.md) on {created}.

| Manager | Scripts |
|---|---|
{manager_table}

## Files

- `scripts/scripts-XX.jsonl.gz`: one JSON record per unique script, sharded by the first two hex
  digits of `blob_sha` ({shards} files, {script_bytes:.0f} MB in total).
- `index.csv.gz`: one row per (script, manager) with flat metadata and labels, no content. `repo`,
  `path`, and `commit` give the first occurrence: `https://github.com/{{repo}}/blob/{{commit}}/{{path}}`.
- `repos.csv.gz`: one row per discovered repository with license and scan status.
- `manifest.json`: counts, field descriptions, and a SHA-256 per file.

## Script record

{field_list}

`blob_sha` is the git blob SHA-1 of the original bytes, so a record can be checked against GitHub.
{scrub_note}

## Loading

```python
import glob, gzip, json
import pandas as pd

index = pd.read_csv("index.csv.gz")                       # metadata and labels, no content
gpu_jobs = index[(index.manager == "slurm") & (index.gpu_request == 1)]

def scripts(pattern="scripts/scripts-*.jsonl.gz"):         # full records, with content
    for path in sorted(glob.glob(pattern)):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                yield json.loads(line)

def script(blob_sha):                                      # one record by SHA
    return next(r for r in scripts(f"scripts/scripts-{{blob_sha[:2]}}.jsonl.gz")
                if r["blob_sha"] == blob_sha)
```

## Coverage and caveats

- License and other repository metadata are present for {repos_with_metadata:,} of {repos:,}
  discovered repositories ({licensed:,} with a recognized license). `license` is null where it was not fetched
  or the repository has none; a null license does not grant reuse rights.
- Scripts can contain account names, email addresses, and home paths.
- Labels come only from directives the manager itself would read; see the project README.
"""


def export(db_path, out, scrub_emails=False):
    """Write the dataset to out and return the manifest."""
    os.makedirs(os.path.join(out, "scripts"), exist_ok=True)
    files, managers = [], {}
    index_handles = _gzip_writer(os.path.join(out, "index.csv.gz"))
    index = csv.DictWriter(index_handles[0], fieldnames=INDEX_COLUMNS)
    index.writeheader()
    shard, handles, shard_count, scripts, occurrences, index_count = None, None, 0, 0, 0, 0

    def finish_shard():
        if handles:
            _close(handles)
            files.append({"name": f"scripts/scripts-{shard}.jsonl.gz", "records": shard_count})

    for record in records(db_path, scrub_emails=scrub_emails):
        prefix = record["blob_sha"][:2]
        if prefix != shard:
            finish_shard()
            shard, shard_count = prefix, 0
            handles = _gzip_writer(os.path.join(out, "scripts", f"scripts-{shard}.jsonl.gz"))
        handles[0].write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        shard_count += 1
        scripts += 1
        occurrences += record["n_occurrences"]
        for manager in record["managers"]:
            managers[manager] = managers.get(manager, 0) + 1
        for row in index_rows(record):
            index.writerow(row)
            index_count += 1
    finish_shard()
    _close(index_handles)
    files.append({"name": "index.csv.gz", "records": index_count})
    files.append({"name": "repos.csv.gz",
                  "records": write_repos(db_path, os.path.join(out, "repos.csv.gz"))})
    for entry in files:
        path = os.path.join(out, entry["name"])
        entry["bytes"], entry["sha256"] = os.path.getsize(path), _sha256(path)

    db = open_ro(db_path)
    one = lambda sql: db.execute(sql).fetchone()[0]
    manifest = {
        "name": "jobscript-corpus", "created": time.strftime("%Y-%m-%d", time.gmtime()),
        "scripts": scripts, "occurrences": occurrences, "label_rows": index_count,
        "scripts_by_manager": dict(sorted(managers.items(), key=lambda kv: -kv[1])),
        "repos": one("SELECT COUNT(DISTINCT repo) FROM repo_sources"),
        "repos_with_scripts": one("SELECT COUNT(DISTINCT repo) FROM occurrences"),
        "repos_with_metadata": one("SELECT COUNT(*) FROM repos WHERE status='ok'"),
        "repos_with_license": one("SELECT COUNT(*) FROM repos WHERE status='ok' AND license IS NOT NULL "
                                  "AND license != 'NOASSERTION'"),
        "parser_version": one("SELECT MAX(parser_version) FROM labels"),
        "emails_scrubbed": scrub_emails, "script_fields": SCRIPT_FIELDS, "index_columns": INDEX_COLUMNS,
        "files": files,
    }
    with open(os.path.join(out, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    script_bytes = sum(f["bytes"] for f in files if f["name"].startswith("scripts/")) / 1e6
    with open(os.path.join(out, "README.md"), "w") as fh:
        fh.write(CARD.format(
            scripts=scripts, occurrences=occurrences, repos=manifest["repos"], created=manifest["created"],
            manager_table="\n".join(f"| {m} | {n:,} |" for m, n in manifest["scripts_by_manager"].items()),
            shards=sum(1 for f in files if f["name"].startswith("scripts/")), script_bytes=script_bytes,
            field_list="\n".join(f"- `{k}`: {v}" for k, v in SCRIPT_FIELDS.items()),
            scrub_note=("Email addresses were replaced with `[email removed]`, so `size` and `blob_sha` "
                        "describe the original file, not the scrubbed text." if scrub_emails else ""),
            repos_with_metadata=manifest["repos_with_metadata"], licensed=manifest["repos_with_license"],
            repos_with_scripts=manifest["repos_with_scripts"]))
    return manifest


def main():
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default="harvest.sqlite")
    parser.add_argument("--out", default="dataset")
    parser.add_argument("--scrub-emails", action="store_true",
                        help="replace email addresses in content and labels")
    args = parser.parse_args()
    manifest = export(args.db, args.out, args.scrub_emails)
    total = sum(f["bytes"] for f in manifest["files"])
    print(f"{manifest['scripts']:,} scripts in {len(manifest['files'])} files, "
          f"{total / 1e6:.0f} MB, largest {max(f['bytes'] for f in manifest['files']) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
