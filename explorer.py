#!/usr/bin/env python3
"""Browse every jobscript in harvest.sqlite in a local web UI.

    python3 explorer.py                # then open http://127.0.0.1:8000

The first run builds explorer-index.sqlite (about a minute), a flat table of metadata and
parsed labels used for filtering. harvest.sqlite is only ever opened read-only.
"""

import argparse
import json
import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from dataset import FLAT, index_rows, open_ro, permalink, records

COLUMNS = ["blob_sha", "manager", "size", "directive_lines", "n_occurrences", "repo", "path",
           "commit", "license"] + FLAT
SORTS = {"": "s.rowid", "size": "s.size DESC", "nodes": "s.nodes DESC", "tasks": "s.tasks DESC",
         "gpus": "s.gpus DESC", "walltime": "s.walltime_seconds DESC",
         "occurrences": "s.n_occurrences DESC", "directives": "s.directive_lines DESC"}
PAGE = 50


def source_stamp(db_path):
    """Identify the source database state the index was built from."""
    stat = os.stat(db_path)
    return f"{stat.st_size}:{int(stat.st_mtime)}"


def build_index(db_path, index_path):
    """Create the flat filter table from blobs, occurrences, and labels."""
    tmp = index_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    out = sqlite3.connect(tmp)
    out.execute("CREATE TABLE scripts (%s)" % ", ".join(f'"{c}"' for c in COLUMNS))
    out.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    insert = f"INSERT INTO scripts VALUES ({','.join('?' * len(COLUMNS))})"
    batch, count = [], 0
    for record in records(db_path, with_content=False):
        for row in index_rows(record):
            batch.append([row[c] for c in COLUMNS])
        if len(batch) >= 20000:
            out.executemany(insert, batch)
            count += len(batch)
            batch = []
            print(f"\rindexing {count:,} scripts", end="", file=sys.stderr)
    out.executemany(insert, batch)
    for column in ("blob_sha", "manager", "repo", "queue", "license"):
        out.execute(f"CREATE INDEX scripts_{column} ON scripts ({column})")
    out.execute("INSERT INTO meta VALUES ('source', ?)", (source_stamp(db_path),))
    out.commit()
    out.close()
    os.replace(tmp, index_path)
    print(f"\rindexed {count + len(batch):,} script rows", file=sys.stderr)


def ensure_index(db_path, index_path, rebuild=False):
    """Build the index if it is missing or older than the source database."""
    current = None
    if os.path.exists(index_path) and not rebuild:
        try:
            idx = sqlite3.connect(index_path)
            current = idx.execute("SELECT value FROM meta WHERE key='source'").fetchone()
            idx.close()
        except sqlite3.Error:
            current = None
    if not current or current[0] != source_stamp(db_path):
        build_index(db_path, index_path)


def like(text):
    """Return a LIKE pattern matching text anywhere, with wildcards escaped."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search(conn, params):
    """Return one page of script rows matching the filters."""
    get = lambda name: (params.get(name) or [""])[0].strip()
    where, args = [], []
    for column in ("manager", "license"):
        if get(column) == "(none)":
            where.append(f"s.{column} IS NULL")
        elif get(column):
            where.append(f"s.{column} = ?")
            args.append(get(column))
    for column in ("repo", "path", "queue", "job_name", "account"):
        if get(column):
            where.append(f"s.{column} LIKE ? ESCAPE '\\'")
            args.append(like(get(column)))
    for name, column, scale in (("min_nodes", "nodes", 1), ("max_nodes", "nodes", 1),
                                ("min_tasks", "tasks", 1), ("min_gpus", "gpus", 1),
                                ("min_hours", "walltime_seconds", 3600),
                                ("max_hours", "walltime_seconds", 3600)):
        if get(name):
            try:
                value = float(get(name)) * scale
            except ValueError:
                continue
            where.append(f"s.{column} {'>=' if name.startswith('min') else '<='} ?")
            args.append(value)
    for flag, clause in (("gpu", "s.gpu_request = 1"), ("array", "s.array_count IS NOT NULL"),
                         ("exclusive", "s.exclusive = 1"), ("errors", "s.n_errors > 0"),
                         ("duplicated", "s.n_occurrences > 1")):
        if get(flag):
            where.append(clause)
    sha = get("sha").lower()
    if sha:
        where.append("s.blob_sha >= ? AND s.blob_sha < ?")
        args += [sha, sha + "g"]
    joins = ""
    text = get("q")
    if text:
        joins = "JOIN h.blobs b ON b.blob_sha = s.blob_sha"
        where.append("b.content LIKE ? ESCAPE '\\'")
        args.append(like(text))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        offset = max(0, int(get("offset") or 0))
    except ValueError:
        offset = 0
    order = SORTS.get(get("sort"), SORTS[""])
    rows = conn.execute(
        "SELECT %s " % ", ".join(f's."{c}"' for c in COLUMNS) + f"FROM scripts s {joins} {clause} "
        f"ORDER BY {order} LIMIT ? OFFSET ?", args + [PAGE + 1, offset]).fetchall()
    # Counting a content search would scan every script, so only filter-only searches are counted.
    total = None if text else conn.execute(
        f"SELECT COUNT(*) FROM scripts s {clause}", args).fetchone()[0]
    return {"rows": [dict(zip(COLUMNS, r)) for r in rows[:PAGE]], "more": len(rows) > PAGE,
            "offset": offset, "total": total, "page": PAGE}


def script(conn, sha):
    """Return everything known about one script."""
    row = conn.execute("SELECT blob_sha, size, content, signals, managers FROM h.blobs "
                       "WHERE blob_sha = ?", (sha,)).fetchone()
    if not row:
        return None
    occurrences = [
        {"repo": r, "commit": c, "path": p, "url": permalink(r, c, p), "license": lic, "stars": stars,
         "pushed_at": pushed}
        for r, c, p, lic, stars, pushed in conn.execute("""
            SELECT o.repo, o.commit_sha, o.path, r.license, r.stars, r.pushed_at
            FROM h.occurrences o LEFT JOIN h.repos r ON r.repo = o.repo
            WHERE o.blob_sha = ? ORDER BY o.repo, o.path LIMIT 200""", (sha,))]
    labels = {m: {"labels": json.loads(l), "totals": json.loads(t)} for m, l, t in conn.execute(
        "SELECT manager, labels, totals FROM h.labels WHERE blob_sha = ?", (sha,))}
    return {"blob_sha": row[0], "size": row[1], "content": row[2], "signals": json.loads(row[3]),
            "managers": row[4].split(","), "occurrences": occurrences, "labels": labels}


def facets(conn):
    """Return the values offered by the filter dropdowns."""
    managers = conn.execute("SELECT manager, COUNT(*) FROM scripts GROUP BY 1 ORDER BY 2 DESC").fetchall()
    licenses = conn.execute("SELECT COALESCE(license, '(none)'), COUNT(*) FROM scripts "
                            "GROUP BY 1 ORDER BY 2 DESC LIMIT 25").fetchall()
    return {"managers": managers, "licenses": licenses,
            "scripts": conn.execute("SELECT COUNT(DISTINCT blob_sha) FROM scripts").fetchone()[0]}


class Handler(BaseHTTPRequestHandler):
    """Serve the page and its JSON API."""

    db_path = index_path = None

    def connect(self):
        conn = sqlite3.connect(f"file:{quote(os.path.abspath(self.index_path))}?mode=ro", uri=True)
        conn.execute("ATTACH DATABASE ? AS h", (f"file:{quote(os.path.abspath(self.db_path))}?mode=ro",))
        return conn

    def send(self, body, content_type="application/json", status=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        params = parse_qs(url.query)
        if url.path == "/":
            return self.send(PAGE_HTML.encode(), "text/html; charset=utf-8")
        if not url.path.startswith("/api/"):
            return self.send({"error": "not found"}, status=404)
        conn = self.connect()
        try:
            if url.path == "/api/search":
                return self.send(search(conn, params))
            if url.path == "/api/facets":
                return self.send(facets(conn))
            if url.path == "/api/script":
                found = script(conn, (params.get("sha") or [""])[0])
                return self.send(found or {"error": "not found"}, status=200 if found else 404)
            return self.send({"error": "not found"}, status=404)
        except sqlite3.Error as err:
            return self.send({"error": str(err)}, status=500)
        finally:
            conn.close()

    def log_message(self, *args):
        pass


PAGE_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jobscript explorer</title>
<style>
:root { --bg:#f6f7f9; --panel:#fff; --fg:#1a2230; --muted:#5d6878; --line:#d9dee6; --accent:#1f5fbf; --hl:#fff4c7; --code:#f3f5f8; }
@media (prefers-color-scheme: dark) { :root { --bg:#12161c; --panel:#1a2029; --fg:#e4e9f0; --muted:#97a3b4; --line:#2c3542; --accent:#7fb0ff; --hl:#3b3413; --code:#141a22; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { padding:12px 16px; border-bottom:1px solid var(--line); background:var(--panel); display:flex; gap:12px; align-items:baseline; flex-wrap:wrap; }
header h1 { font-size:17px; margin:0; font-weight:600; }
header span { color:var(--muted); }
main { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:12px; padding:12px 16px; }
@media (max-width: 1000px) { main { grid-template-columns:minmax(0,1fr); } }
.filters { grid-column:1/-1; background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 12px; display:flex; flex-wrap:wrap; gap:8px 12px; align-items:end; }
.filters label { display:flex; flex-direction:column; gap:2px; font-size:12px; color:var(--muted); }
.filters label.check { flex-direction:row; align-items:center; gap:5px; color:var(--fg); font-size:13px; }
input[type=text], input[type=number], select { font:inherit; padding:5px 7px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); width:11rem; }
input[type=number] { width:5.5rem; }
input.wide { width:16rem; }
button { font:inherit; padding:6px 12px; border:1px solid var(--line); border-radius:6px; background:var(--panel); color:var(--fg); cursor:pointer; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
@media (prefers-color-scheme: dark) { button.primary { color:#0c1320; } }
button:disabled { opacity:.45; cursor:default; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; min-width:0; overflow:hidden; display:flex; flex-direction:column; max-height:calc(100vh - 190px); min-height:320px; }
.bar { padding:8px 12px; border-bottom:1px solid var(--line); display:flex; gap:10px; align-items:center; justify-content:space-between; color:var(--muted); font-size:13px; flex-wrap:wrap; }
.scroll { overflow:auto; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th, td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
th { position:sticky; top:0; background:var(--panel); color:var(--muted); font-weight:500; font-size:12px; }
td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; }
td.path { max-width:22rem; overflow:hidden; text-overflow:ellipsis; }
tbody tr { cursor:pointer; }
tbody tr:hover, tbody tr.sel { background:var(--hl); }
.detail { padding:12px; overflow:auto; }
.detail h2 { font-size:14px; margin:14px 0 6px; font-weight:600; }
.detail h2:first-child { margin-top:0; }
.kv { display:grid; grid-template-columns:max-content minmax(0,1fr); gap:3px 14px; font-size:13px; }
.kv dt { color:var(--muted); } .kv dd { margin:0; word-break:break-word; }
.mono, pre { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:12px; }
pre { margin:0; background:var(--code); border:1px solid var(--line); border-radius:6px; padding:8px 0; overflow:auto; line-height:1.5; }
pre span { display:block; padding:0 10px; white-space:pre; min-height:1.5em; }
pre span.d { background:var(--hl); }
a { color:var(--accent); }
ul.occ { margin:0; padding-left:18px; font-size:13px; word-break:break-all; }
.empty { padding:24px; color:var(--muted); text-align:center; }
.chip { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:0 8px; font-size:12px; margin-right:4px; }
</style></head><body>
<header><h1>Jobscript explorer</h1><span id="corpus"></span></header>
<main>
  <div class="filters" id="filters">
    <label>Manager<select id="f-manager"><option value="">Any</option></select></label>
    <label>Repository contains<input type="text" id="f-repo"></label>
    <label>Path contains<input type="text" id="f-path"></label>
    <label>Queue contains<input type="text" id="f-queue"></label>
    <label>License<select id="f-license"><option value="">Any</option></select></label>
    <label>Min nodes<input type="number" id="f-min_nodes" min="0"></label>
    <label>Min tasks<input type="number" id="f-min_tasks" min="0"></label>
    <label>Min GPUs<input type="number" id="f-min_gpus" min="0"></label>
    <label>Min hours<input type="number" id="f-min_hours" min="0" step="any"></label>
    <label>Max hours<input type="number" id="f-max_hours" min="0" step="any"></label>
    <label>Script text contains<input type="text" id="f-q" class="wide" placeholder="e.g. srun lmp"></label>
    <label>Blob SHA starts with<input type="text" id="f-sha"></label>
    <label>Sort<select id="f-sort"><option value="">Blob SHA</option><option value="size">Size</option><option value="nodes">Nodes</option><option value="tasks">Tasks</option><option value="gpus">GPUs</option><option value="walltime">Walltime</option><option value="occurrences">Occurrences</option><option value="directives">Directive lines</option></select></label>
    <label class="check"><input type="checkbox" id="f-gpu">Requests GPUs</label>
    <label class="check"><input type="checkbox" id="f-array">Job array</label>
    <label class="check"><input type="checkbox" id="f-exclusive">Exclusive</label>
    <label class="check"><input type="checkbox" id="f-duplicated">Found in several places</label>
    <label class="check"><input type="checkbox" id="f-errors">Parse errors</label>
    <button class="primary" id="go">Search</button><button id="reset">Reset</button>
  </div>
  <div class="panel">
    <div class="bar"><span id="count">&nbsp;</span><span><button id="prev">Previous</button> <button id="next">Next</button></span></div>
    <div class="scroll"><table><thead><tr><th>Manager</th><th>Repository</th><th>Path</th><th class="n">Bytes</th><th class="n">Nodes</th><th class="n">Tasks</th><th class="n">GPUs</th><th class="n">Walltime</th><th>Queue</th><th class="n">Places</th></tr></thead><tbody id="rows"></tbody></table><div class="empty" id="none" hidden>No scripts match these filters.</div></div>
  </div>
  <div class="panel"><div class="detail" id="detail"><div class="empty">Select a script to see its content, where it was found, and its parsed labels.</div></div></div>
</main>
<script>
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; };
const fmt = (v) => (v === null || v === undefined ? '' : Number(v).toLocaleString('en'));
const dur = (s) => { if (s === null || s === undefined) return ''; const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return (d ? d + 'd ' : '') + String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0'); };
const TEXT = ['manager', 'repo', 'path', 'queue', 'license', 'min_nodes', 'min_tasks', 'min_gpus', 'min_hours', 'max_hours', 'q', 'sha', 'sort'];
const FLAGS = ['gpu', 'array', 'exclusive', 'duplicated', 'errors'];
const DIRECTIVE = /^\s*#\s*(SBATCH|SLURM|PBS|BSUB|COBALT|OAR|flux:)/i;
let offset = 0, selected = null;

function query() {
  const p = new URLSearchParams();
  for (const k of TEXT) if ($('f-' + k).value.trim()) p.set(k, $('f-' + k).value.trim());
  for (const k of FLAGS) if ($('f-' + k).checked) p.set(k, '1');
  p.set('offset', offset);
  return p;
}
async function run() {
  $('count').textContent = 'Searching…'; $('go').disabled = true;
  let data;
  try { data = await (await fetch('/api/search?' + query())).json(); } catch (e) { data = { error: String(e) }; }
  $('go').disabled = false;
  const body = $('rows'); body.replaceChildren();
  if (data.error) { $('count').textContent = 'Search failed: ' + data.error; return; }
  for (const r of data.rows) {
    const tr = el('tr'); tr.dataset.sha = r.blob_sha;
    if (r.blob_sha === selected) tr.className = 'sel';
    tr.append(el('td', '', r.manager), el('td', 'path', r.repo), el('td', 'path', r.path), el('td', 'n', fmt(r.size)), el('td', 'n', fmt(r.nodes)), el('td', 'n', fmt(r.tasks)), el('td', 'n', r.gpus !== null ? fmt(r.gpus) : (r.gpu_request ? 'yes' : '')), el('td', 'n', dur(r.walltime_seconds)), el('td', 'path', r.queue), el('td', 'n', fmt(r.n_occurrences)));
    tr.children[2].title = r.path || '';
    body.append(tr);
  }
  $('none').hidden = data.rows.length > 0;
  const first = data.rows.length ? offset + 1 : 0, last = offset + data.rows.length;
  $('count').textContent = data.total !== null ? `${fmt(first)}-${fmt(last)} of ${fmt(data.total)} script rows` : `${fmt(first)}-${fmt(last)}${data.more ? ', more available' : ''} (text search is not counted)`;
  $('prev').disabled = offset === 0; $('next').disabled = !data.more;
}
async function show(sha) {
  selected = sha;
  for (const tr of $('rows').children) tr.className = tr.dataset.sha === sha ? 'sel' : '';
  const d = await (await fetch('/api/script?sha=' + encodeURIComponent(sha))).json();
  const box = $('detail'); box.replaceChildren();
  if (d.error) { box.append(el('div', 'empty', 'Could not load this script.')); return; }
  box.append(el('h2', '', 'Script'));
  const kv = el('dl', 'kv');
  const add = (k, v) => { if (v === null || v === undefined || v === '') return; kv.append(el('dt', '', k)); const dd = el('dd'); if (v instanceof Node) dd.append(v); else dd.textContent = v; kv.append(dd); };
  add('Blob SHA', el('span', 'mono', d.blob_sha)); add('Managers', d.managers.join(', ')); add('Size', fmt(d.size) + ' bytes');
  add('Detection', Object.entries(d.signals).map(([k, v]) => `${k}: ${v}`).join(', '));
  box.append(kv);
  box.append(el('h2', '', `Found in ${d.occurrences.length} place${d.occurrences.length === 1 ? '' : 's'}`));
  const ul = el('ul', 'occ');
  for (const o of d.occurrences) { const li = el('li'); const a = el('a', '', `${o.repo} / ${o.path}`); a.href = o.url; a.target = '_blank'; a.rel = 'noopener'; li.append(a, document.createTextNode(` @ ${(o.commit || '').slice(0, 10)} · license: ${o.license || 'not known'}${o.stars !== null && o.stars !== undefined ? ' · ' + fmt(o.stars) + ' stars' : ''}`)); ul.append(li); }
  box.append(ul);
  for (const [m, l] of Object.entries(d.labels)) {
    box.append(el('h2', '', `Parsed labels (${m})`));
    const lk = el('dl', 'kv'); const L = l.labels, T = l.totals;
    const put = (k, v) => { if (v === null || v === undefined || v === '' || v === false) return; lk.append(el('dt', '', k), el('dd', '', typeof v === 'object' ? JSON.stringify(v) : String(v))); };
    put('Nodes', T.nodes ?? (L.nodes ? `${L.nodes.min}-${L.nodes.max}` : null)); put('Tasks', T.tasks); put('Tasks per node', L.tasks_per_node); put('CPUs per task', L.cpus_per_task);
    put('GPUs', T.gpus ?? (L.gpus.length ? L.gpus : null)); put('Memory', L.memory.length ? L.memory.map((x) => `${x.mib} MiB per ${x.per}`).join(', ') : null);
    put('Walltime', T.walltime_seconds !== null ? `${dur(T.walltime_seconds)} (${fmt(T.walltime_seconds)} s)` : null); put('Queue', L.queue); put('Account', L.account); put('QoS', L.qos); put('Job name', L.job_name);
    put('Constraint', L.constraint); put('Array', L.array); put('Dependency', L.dependency); put('Exclusive', L.exclusive ? 'yes' : null); put('PBS chunks', L.chunks); put('Hetjob components', L.components > 1 ? L.components : null);
    put('Unknown tokens', L.unknown.length ? L.unknown : null); put('Errors', L.errors.length ? L.errors : null); put('Ignored directive lines', L.ignored_directive_lines || null);
    box.append(lk);
    if (L.options.length) { const p = el('p'); p.style.margin = '6px 0 0'; for (const o of L.options) p.append(el('span', 'chip mono', o.value === null || o.value === undefined || o.value === '' ? o.option : `${o.option}=${o.value}`)); box.append(p); }
  }
  box.append(el('h2', '', 'Content'));
  const pre = el('pre');
  for (const line of d.content.split('\n')) pre.append(el('span', DIRECTIVE.test(line) ? 'd' : '', line));
  box.append(pre); box.scrollTop = 0;
}
$('rows').addEventListener('click', (e) => { const tr = e.target.closest('tr'); if (tr) show(tr.dataset.sha); });
$('go').addEventListener('click', () => { offset = 0; run(); });
$('filters').addEventListener('keydown', (e) => { if (e.key === 'Enter') { offset = 0; run(); } });
$('reset').addEventListener('click', () => { for (const k of TEXT) $('f-' + k).value = ''; for (const k of FLAGS) $('f-' + k).checked = false; offset = 0; run(); });
$('prev').addEventListener('click', () => { offset = Math.max(0, offset - 50); run(); });
$('next').addEventListener('click', () => { offset += 50; run(); });
(async () => {
  const f = await (await fetch('/api/facets')).json();
  $('corpus').textContent = `${fmt(f.scripts)} unique scripts`;
  for (const [m, n] of f.managers) $('f-manager').append(new Option(`${m} (${fmt(n)})`, m));
  for (const [l, n] of f.licenses) $('f-license').append(new Option(`${l} (${fmt(n)})`, l));
  run();
})();
</script></body></html>
"""


def main():
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default="harvest.sqlite")
    parser.add_argument("--index", default="explorer-index.sqlite")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--rebuild", action="store_true", help="rebuild the index even if current")
    parser.add_argument("--build-only", action="store_true", help="build the index and exit")
    args = parser.parse_args()
    ensure_index(args.db, args.index, args.rebuild)
    if args.build_only:
        return
    Handler.db_path, Handler.index_path = args.db, args.index
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Jobscript explorer at http://{args.host}:{args.port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
