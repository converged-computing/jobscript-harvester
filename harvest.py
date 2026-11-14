#!/usr/bin/env python3
"""Build a corpus of workload-manager jobscripts from public GitHub repositories."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests

import store
from detect import detect, managers

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
LEGACY_REPO = "converged-computing/jobspec-database"
LEGACY_SOURCE = "jobspec-database"
MAX_BYTES = 384 * 1024


class GitHub:
    """Minimal GitHub REST client with rate-limit handling."""

    def __init__(self, token=None):
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/vnd.github+json"
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def get(self, url, max_tries=6):
        """GET url, waiting on primary and secondary rate limits."""
        return self.request("GET", url, max_tries)

    def post(self, url, payload, max_tries=6):
        """POST a JSON payload to url, waiting on primary and secondary rate limits."""
        return self.request("POST", url, max_tries, json=payload)

    def request(self, method, url, max_tries=6, **kwargs):
        """Send a request, waiting on primary and secondary rate limits."""
        backoff = 60
        for _ in range(max_tries):
            resp = self.session.request(method, url, timeout=60, **kwargs)
            if resp.status_code not in (403, 429):
                return resp
            # Retry rules from docs.github.com/rest/using-the-rest-api/rate-limits-for-the-rest-api
            if "retry-after" in resp.headers:
                wait = int(resp.headers["retry-after"])
            elif resp.headers.get("x-ratelimit-remaining") == "0":
                wait = max(0, int(resp.headers["x-ratelimit-reset"]) - time.time()) + 1
            else:
                wait, backoff = backoff, backoff * 2
            print(f"rate limited ({resp.status_code}); sleeping {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
        return resp


def without_license_search(plan):
    """Return the search plan with license: qualifiers removed from every split family."""
    split = []
    for family in plan["split"]:
        values = family["values"]
        if isinstance(values, dict):
            values = {k: [v for v in vs if not v.startswith("license:")] for k, vs in values.items()}
            keep = any(values.values())
        else:
            values = [v for v in values if not v.startswith("license:")]
            keep = bool(values)
        if keep:
            split.append({**family, "values": values})
    return {**plan, "split": split}


def fetch_repos(db, gh, limit):
    """Record license and metadata for discovered repositories."""
    rows = db.execute("""
        SELECT DISTINCT s.repo FROM repo_sources s LEFT JOIN repos r ON r.repo = s.repo
        WHERE r.repo IS NULL LIMIT ?""", (limit,)).fetchall()
    for done, (repo,) in enumerate(rows, 1):
        resp = gh.get(f"{API}/repos/{repo}")
        if resp.ok:
            d = resp.json()
            record = (repo, "ok", (d.get("license") or {}).get("spdx_id"), int(d["fork"]),
                      int(d["archived"]), d["stargazers_count"], d["default_branch"],
                      d["size"], d["pushed_at"])
        else:
            record = (repo, f"http_{resp.status_code}") + (None,) * 7
        db.execute("INSERT OR REPLACE INTO repos VALUES (?,?,?,?,?,?,?,?,?)", record)
        db.commit()
        if done % 100 == 0 or done == len(rows):
            print(f"[{done}/{len(rows)}] {repo}", file=sys.stderr)
    return len(rows)


GRAPHQL_FIELDS = ("isFork isArchived stargazerCount diskUsage pushedAt "
                  "defaultBranchRef { name } licenseInfo { spdxId }")


def graphql_query(repos):
    """Return one GraphQL query asking for the metadata of every repo, aliased r0, r1, ..."""
    parts = []
    for i, repo in enumerate(repos):
        owner, name = repo.split("/", 1)
        # json.dumps yields a valid GraphQL string literal.
        parts.append(f"r{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
                     f"{{ {GRAPHQL_FIELDS} }}")
    return "query { " + " ".join(parts) + " }"


def graphql_records(repos, body):
    """Turn a GraphQL response body into rows for the repos table.

    Returns (records, retry): repos that GitHub reports as missing are recorded like
    the REST path records them (http_404); repos with any other per-repo error are
    returned in retry and left unrecorded.
    """
    data = body.get("data") or {}
    errors = {}
    for err in body.get("errors") or []:
        path = err.get("path") or []
        if path and isinstance(path[0], str):
            errors[path[0]] = err.get("type") or "ERROR"
    records, retry = [], []
    for i, repo in enumerate(repos):
        d, error = data.get(f"r{i}"), errors.get(f"r{i}")
        if d:
            records.append((repo, "ok", (d.get("licenseInfo") or {}).get("spdxId"), int(d["isFork"]),
                            int(d["isArchived"]), d["stargazerCount"],
                            (d.get("defaultBranchRef") or {}).get("name"), d.get("diskUsage"),
                            d.get("pushedAt")))
        elif error == "NOT_FOUND":
            records.append((repo, "http_404") + (None,) * 7)
        else:
            retry.append(repo)
    return records, retry


def fetch_repos_graphql(db, gh, limit, batch=100, sleep=time.sleep):
    """Record license and metadata for discovered repositories, many per request.

    One GraphQL request covers a whole batch and costs one rate-limit point, so tens of
    thousands of repositories take minutes. A batch that fails as a whole is split in
    half and retried; a repository that still fails alone is left for a later run.
    """
    rows = [r for (r,) in db.execute("""
        SELECT DISTINCT s.repo FROM repo_sources s LEFT JOIN repos r ON r.repo = s.repo
        WHERE r.repo IS NULL LIMIT ?""", (limit,))]
    valid = [r for r in rows if r.count("/") == 1]
    stats = {"recorded": 0, "skipped": len(rows) - len(valid)}

    def run(chunk, tries=3):
        body = None
        for attempt in range(tries):
            resp = gh.post(f"{API}/graphql", {"query": graphql_query(chunk)})
            try:
                body = resp.json() if resp.ok else None
            except ValueError:
                body = None
            limited = body is not None and any(
                e.get("type") == "RATE_LIMITED" for e in body.get("errors") or [])
            if body is not None and body.get("data") and not limited:
                break
            body = None
            if limited:
                print("GraphQL rate limit reached; sleeping 300s", file=sys.stderr)
                sleep(300)
            elif attempt < tries - 1:
                sleep(5 * (attempt + 1))
        if body is None:
            if len(chunk) == 1:
                stats["skipped"] += 1
                print(f"skipped {chunk[0]}: no usable answer", file=sys.stderr)
                return
            half = len(chunk) // 2
            run(chunk[:half])
            run(chunk[half:])
            return
        records, retry = graphql_records(chunk, body)
        db.executemany("INSERT OR REPLACE INTO repos VALUES (?,?,?,?,?,?,?,?,?)", records)
        db.commit()
        stats["recorded"] += len(records)
        if retry and len(chunk) > 1:
            for repo in retry:
                run([repo])
        else:
            stats["skipped"] += len(retry)
            for repo in retry:
                print(f"skipped {repo}: GitHub returned an error for it", file=sys.stderr)

    for start in range(0, len(valid), batch):
        run(valid[start:start + batch])
        print(f"[{min(start + batch, len(valid))}/{len(valid)}] recorded {stats['recorded']}, "
              f"skipped {stats['skipped']}", file=sys.stderr)
    return stats["recorded"], stats["skipped"]


def repos_to_scan(db, include_legacy, max_repo_kb, limit):
    """Return discovered repos not yet scanned, honoring legacy and size filters."""
    rows = db.execute("""
        SELECT DISTINCT s.repo, r.status, r.size_kb FROM repo_sources s
        LEFT JOIN repos r ON r.repo = s.repo LEFT JOIN scans x ON x.repo = s.repo
        WHERE x.repo IS NULL""").fetchall()
    legacy = {r for (r,) in db.execute(
        "SELECT repo FROM repo_sources WHERE source=?", (LEGACY_SOURCE,))}
    out = []
    for repo, status, size_kb in rows:
        if repo in legacy and not include_legacy:
            continue
        if status and status != "ok":
            continue
        if max_repo_kb and size_kb and size_kb > max_repo_kb:
            continue
        out.append(repo)
    return out[:limit]


def scan(db, workers, include_legacy, max_repo_kb, limit, max_bytes):
    """Clone and scan discovered repositories in parallel."""
    from scan import scan_repo

    repos = repos_to_scan(db, include_legacy, max_repo_kb, limit)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(scan_repo, repo, max_bytes) for repo in repos]
        for done, future in enumerate(as_completed(futures), 1):
            r = future.result()
            for sha, data, counts, mgrs, paths in r["found"]:
                store.save_blob(db, sha, data, counts, mgrs)
                for path in paths:
                    store.save_occurrence(db, sha, r["repo"], r["commit"], path, "scan")
            db.execute("INSERT OR REPLACE INTO scans VALUES (?,?,?,?,?,?)", (
                r["repo"], r["status"], r["commit"], r["blobs_read"],
                r["blobs_skipped"], len(r["found"])))
            db.commit()
            print(f"[{done}/{len(repos)}] {r['repo']}: {r['status']}, kept {len(r['found'])}",
                  file=sys.stderr)
    return len(repos)


def import_legacy(db, fetch, limit, max_bytes):
    """Record repos and files from jobspec-database; optionally fetch script contents."""
    workdir = tempfile.mkdtemp(prefix="legacy-")
    dest = os.path.join(workdir, "repo")
    # Trees only (no file contents), so the listing stays small.
    subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--filter=blob:none",
                    "--no-checkout", f"https://github.com/{LEGACY_REPO}.git", dest],
                   check=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, check=True,
                            capture_output=True, text=True).stdout.strip()
    listing = subprocess.run(["git", "ls-tree", "-r", "-z", "HEAD", "data/"], cwd=dest,
                             check=True, capture_output=True).stdout
    files = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        _, kind, sha = meta.decode().split()
        parts = path.decode("utf-8", errors="replace").split("/", 3)
        if kind != "blob" or len(parts) < 4:
            continue
        repo, rest = f"{parts[1]}/{parts[2]}", parts[3]
        role = "config" if rest.startswith("jobspec-cfg/") else "script"
        db.execute("INSERT OR IGNORE INTO legacy_files VALUES (?,?,?,?)", (repo, rest, sha, role))
        store.add_source(db, repo, LEGACY_SOURCE)
        files.append((repo, rest, sha, role, path.decode("utf-8", errors="replace")))
    db.commit()

    fetched = 0
    if fetch:
        session = requests.Session()
        for repo, rest, sha, role, full in files:
            if role != "script" or fetched >= limit:
                continue
            if db.execute("SELECT 1 FROM blobs WHERE blob_sha=?", (sha,)).fetchone():
                continue
            resp = session.get(f"{RAW}/{LEGACY_REPO}/{commit}/{quote(full)}", timeout=60)
            fetched += 1
            if not resp.ok or store.git_blob_sha(resp.content) != sha:
                continue
            data = resp.content
            if len(data) > max_bytes or b"\x00" in data:
                continue
            counts = detect(data.decode("utf-8", errors="replace"))
            mgrs = managers(counts)
            if mgrs:
                store.save_blob(db, sha, data, counts, mgrs)
                store.save_occurrence(db, sha, repo, None, rest, LEGACY_SOURCE)
            db.commit()
    return len(files), fetched


def label(db, relabel):
    """Store parser labels and totals for every kept blob."""
    import parse

    rows = db.execute("SELECT blob_sha, content, managers FROM blobs").fetchall()
    done = 0
    for sha, content, mgrs in rows:
        current = {m for (m,) in db.execute(
            "SELECT manager FROM labels WHERE blob_sha=? AND parser_version=?",
            (sha, parse.PARSER_VERSION))}
        todo = [m for m in mgrs.split(",") if relabel or m not in current]
        for manager, labels in parse.parse(content, todo).items():
            db.execute("INSERT OR REPLACE INTO labels VALUES (?,?,?,?,?)", (
                sha, manager, parse.PARSER_VERSION, json.dumps(labels),
                json.dumps(parse.totals(labels))))
            done += 1
    db.commit()
    return done


def soft_label(db, backend, model, limit, runner=None):
    """Run the soft-label task over blobs that have no soft labels for this model."""
    import asyncio

    from behalf import make_runner, run_task

    import soft

    runner = runner or make_runner(backend, model)
    rows = db.execute("""
        SELECT b.blob_sha, b.content FROM blobs b LEFT JOIN soft_labels s
        ON s.blob_sha = b.blob_sha AND s.backend = ? AND s.model = ?
        WHERE s.blob_sha IS NULL LIMIT ?""", (backend, model or "", limit)).fetchall()
    for sha, content in rows:
        task = soft.SoftLabelTask()
        try:
            asyncio.run(run_task(task, runner, manifest=soft.manifest_for(content),
                                 confirm_fn=lambda name, args: True))
            status, labels = "ok", task.recorded[0]
        except Exception as err:  # recorded so one failure does not stop the batch
            status, labels = f"error: {err}"[:300], None
        db.execute("INSERT OR REPLACE INTO soft_labels VALUES (?,?,?,?,?,?)", (
            sha, backend, model or "", soft.SOFT_VERSION, status, json.dumps(labels)))
        db.commit()
        print(f"{sha[:12]} {status}", file=sys.stderr)
    return len(rows)


def agree(db):
    """Compare parser totals with soft labels and store per-field status."""
    import soft

    count = 0
    for sha, manager, totals, backend, model, labels in db.execute("""
            SELECT l.blob_sha, l.manager, l.totals, s.backend, s.model, s.labels
            FROM labels l JOIN soft_labels s ON s.blob_sha = l.blob_sha
            WHERE s.status = 'ok'""").fetchall():
        hard, soft_labels = json.loads(totals), json.loads(labels)
        for field, status in soft.agreement(hard, soft_labels).items():
            db.execute("INSERT OR REPLACE INTO agreement VALUES (?,?,?,?,?,?,?,?)", (
                sha, manager, backend, model, field, json.dumps(hard.get(field)),
                json.dumps(soft_labels.get(field)), status))
            count += 1
    db.commit()
    return count


def export(db, out):
    """Write kept jobscripts with all occurrences and licenses as JSONL."""
    count = 0
    with open(out, "w") as fh:
        for blob_sha, content, signals, mgrs in db.execute(
                "SELECT blob_sha, content, signals, managers FROM blobs").fetchall():
            occurrences = [
                {"repo": r, "commit": c, "path": p, "source": s, "license": lic}
                for r, c, p, s, lic in db.execute("""
                    SELECT o.repo, o.commit_sha, o.path, o.source, r.license
                    FROM occurrences o LEFT JOIN repos r ON r.repo = o.repo
                    WHERE o.blob_sha = ?""", (blob_sha,))]
            labels = {m: {"labels": json.loads(l), "totals": json.loads(t)}
                      for m, l, t in db.execute(
                          "SELECT manager, labels, totals FROM labels WHERE blob_sha=?",
                          (blob_sha,))}
            soft_labels = [{"backend": b, "model": m, "labels": json.loads(l)}
                           for b, m, l in db.execute(
                               "SELECT backend, model, labels FROM soft_labels "
                               "WHERE blob_sha=? AND status='ok'", (blob_sha,))]
            fh.write(json.dumps({
                "blob_sha": blob_sha, "managers": mgrs.split(","),
                "signals": json.loads(signals), "content": content,
                "occurrences": occurrences, "hard_labels": labels,
                "soft_labels": soft_labels}) + "\n")
            count += 1
    return count


def progress(db, plan_path, batch):
    """Print how much of the search plan is done and how many new repos recent batches found."""
    from websearch import frontier, repo_yield

    with open(plan_path) as fh:
        plan = without_license_search(json.load(fh))
    f = frontier(db, plan)
    print(f"queries done: {f['done']}, empty: {f['empty']}, "
          f"pending now: {f['pending']} {f['pending_by_directive']}")
    print(f"saturated with no further split (results capped): {f['truncated']}")
    print("pending queries can add more pending queries below them if they return 100 results.")
    print(f"new repos per {batch} queries (all / not in {LEGACY_SOURCE}):")
    other = {start: n for start, _, n in repo_yield(db, batch, LEGACY_SOURCE)}
    for start, end, n in repo_yield(db, batch):
        print(f"  queries {start:6d}-{end:<6d} {n:6d} / {other.get(start, 0)}")


def stats(db):
    """Print counts by stage."""
    q = lambda sql: db.execute(sql).fetchall()
    print("queries (directive, status, count, saturated, hits):", q(
        "SELECT directive, status, COUNT(*), SUM(saturated), SUM(results) FROM queries "
        "GROUP BY 1, 2"))
    print("repos by source:", q("SELECT source, COUNT(*) FROM repo_sources GROUP BY source"))
    print("repo metadata:", q("SELECT status, COUNT(*) FROM repos GROUP BY status"))
    print("scans:", q("SELECT substr(status,1,9), COUNT(*), SUM(kept) FROM scans GROUP BY 1"))
    print("blobs by managers:", q("SELECT managers, COUNT(*) FROM blobs GROUP BY 1 ORDER BY 2 DESC"))
    print("occurrences by source:", q("SELECT source, COUNT(*) FROM occurrences GROUP BY 1"))
    print("labels:", q("SELECT manager, COUNT(*) FROM labels GROUP BY 1"))
    print("soft labels:", q("SELECT backend, model, substr(status,1,5), COUNT(*) "
                            "FROM soft_labels GROUP BY 1, 2, 3"))
    print("agreement:", q("SELECT field, status, COUNT(*) FROM agreement "
                          "GROUP BY 1, 2 ORDER BY 1, 2"))
    print("licenses (repos with kept files):", q("""
        SELECT r.license, COUNT(DISTINCT r.repo) FROM repos r
        JOIN occurrences o ON o.repo = r.repo GROUP BY 1 ORDER BY 2 DESC LIMIT 15"""))


def main():
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="harvest.sqlite")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("search", help="discover repos via web code search (browser)")
    p.add_argument("--plan", default="plan.json")
    p.add_argument("--profile", default=os.path.expanduser("~/.jobscript-harvester-chrome"))
    p.add_argument("--directives", nargs="*")
    p.add_argument("--max-queries", type=int)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--retry-empty", action="store_true",
                   help="rerun queries stored with no results")
    p.add_argument("--pages-dir", default="empty-pages",
                   help="where pages of empty queries are saved")
    p = sub.add_parser("repos", help="fetch repo license and metadata (API)")
    p.add_argument("--limit", type=int, default=1_000_000)
    p.add_argument("--graphql", action="store_true",
                   help="fetch many repos per request through the GraphQL API (much faster)")
    p.add_argument("--batch", type=int, default=100, help="repos per GraphQL request")
    p = sub.add_parser("scan", help="clone and scan discovered repos")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--limit", type=int, default=1_000_000)
    p.add_argument("--max-repo-kb", type=int, default=500_000)
    p.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    p.add_argument("--include-legacy", action="store_true")
    p = sub.add_parser("import-legacy", help="record repos from jobspec-database")
    p.add_argument("--fetch", action="store_true", help="also fetch and keep script contents")
    p.add_argument("--limit", type=int, default=1_000_000)
    p.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    p = sub.add_parser("label", help="parse directives into hard labels")
    p.add_argument("--relabel", action="store_true")
    p = sub.add_parser("soft-label", help="model-extracted labels via behalf")
    p.add_argument("--backend", required=True, choices=["claude", "gemini", "aws"])
    p.add_argument("--model")
    p.add_argument("--limit", type=int, default=100)
    sub.add_parser("agree", help="compare hard and soft labels")
    p = sub.add_parser("export")
    p.add_argument("--out", default="jobscripts.jsonl")
    sub.add_parser("stats")
    p = sub.add_parser("progress", help="search plan progress and new-repo yield")
    p.add_argument("--plan", default="plan.json")
    p.add_argument("--batch", type=int, default=100)
    args = parser.parse_args()

    db = store.connect(args.db)
    if args.cmd == "search":
        from websearch import Browser, search
        with open(args.plan) as fh:
            plan = without_license_search(json.load(fh))
        browser = Browser(args.profile, headless=args.headless)
        try:
            browser.ensure_login()
            print("queries run:", search(db, browser, plan, args.directives, args.max_queries,
                                         args.pages_dir, args.retry_empty))
        finally:
            browser.close()
    elif args.cmd == "repos":
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            sys.exit("GITHUB_TOKEN is required (unauthenticated limit is 60 requests/hour)")
        if args.graphql:
            print("repos recorded, skipped: %d, %d" % fetch_repos_graphql(
                db, GitHub(token), args.limit, args.batch))
        else:
            print("repos", fetch_repos(db, GitHub(token), args.limit))
    elif args.cmd == "scan":
        print("scanned", scan(db, args.workers, args.include_legacy, args.max_repo_kb,
                              args.limit, args.max_bytes))
    elif args.cmd == "import-legacy":
        print("legacy files, fetched: %d, %d" % import_legacy(db, args.fetch, args.limit,
                                                              args.max_bytes))
    elif args.cmd == "label":
        print("labeled", label(db, args.relabel))
    elif args.cmd == "soft-label":
        print("soft-labeled", soft_label(db, args.backend, args.model, args.limit))
    elif args.cmd == "agree":
        print("compared", agree(db))
    elif args.cmd == "export":
        print("exported", export(db, args.out))
    elif args.cmd == "progress":
        progress(db, args.plan, args.batch)
    elif args.cmd == "stats":
        stats(db)


if __name__ == "__main__":
    main()
