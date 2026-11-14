/* Renders docs/data/stats.json and the per-manager sample files. No dependencies. */
(() => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; };
  const int = (v) => Number(v).toLocaleString('en');
  const compact = (v) => new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(v);
  const pct = (a, b) => (b ? `${(100 * a / b).toFixed(a / b < 0.1 ? 1 : 0)}%` : '');
  const NAMES = { slurm: 'Slurm', pbs: 'PBS', lsf: 'LSF', flux: 'Flux', oar: 'OAR', cobalt: 'Cobalt' };
  const COVER = { walltime: 'Walltime', nodes: 'Nodes', tasks: 'Tasks', cpus_per_task: 'CPUs per task', memory: 'Memory', gpu: 'GPUs', queue: 'Queue or partition', account: 'Account', array: 'Job array', job_name: 'Job name' };
  const DIRECTIVE = /^\s*#\s*(SBATCH|SLURM|PBS|BSUB|COBALT|OAR|flux:)/i;
  const duration = (s) => { if (s === null || s === undefined) return null; const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.round(s % 3600 / 60); return `${d ? d + ' d ' : ''}${h} h ${m ? m + ' min' : ''}`.trim(); };

  // Horizontal bars: rows of [label, value]; text shows the value, or a share of `whole`.
  const bars = (box, rows, { whole, value = int, title } = {}) => {
    box.replaceChildren();
    const max = Math.max(1, ...rows.map((r) => r[1]));
    for (const [label, v] of rows) {
      const l = el('span', 'l', label); l.title = title ? title(label) : label;
      const t = el('span', 't'); const i = el('i'); i.style.width = `${100 * v / (whole || max)}%`; t.append(i);
      box.append(l, t, el('span', 'v', whole ? `${pct(v, whole)} · ${value(v)}` : value(v)));
    }
  };
  // Vertical columns for ordered buckets: rows of [label, value].
  const cols = (box, rows) => {
    box.replaceChildren();
    const max = Math.max(1, ...rows.map((r) => r[1]));
    for (const [label, v] of rows) {
      const c = el('div', 'c'); const b = el('div', 'b'); b.style.height = `${Math.max(0.5, 100 * v / max)}%`;
      b.title = `${label}: ${int(v)}`; c.append(el('span', 'n', compact(v)), b, el('span', 'x', label)); box.append(c);
    }
  };
  const lineChart = (box, points) => {
    const W = 560, H = 230, m = { t: 12, r: 14, b: 26, l: 44 };
    const xmax = points[points.length - 1][0], ymax = points[points.length - 1][1];
    const x = (v) => m.l + (W - m.l - m.r) * v / xmax, y = (v) => H - m.b - (H - m.t - m.b) * v / ymax;
    const ns = 'http://www.w3.org/2000/svg';
    const s = (tag, attrs, text) => { const e = document.createElementNS(ns, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (text !== undefined) e.textContent = text; return e; };
    const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': `Cumulative repositories discovered, reaching ${int(ymax)} after ${int(xmax)} queries` });
    for (let i = 0; i <= 4; i++) { const v = ymax * i / 4; svg.append(s('line', { class: 'grid-line', x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }), s('text', { x: m.l - 6, y: y(v) + 4, 'text-anchor': 'end' }, compact(v))); }
    for (let i = 0; i <= 4; i++) { const v = xmax * i / 4; svg.append(s('text', { x: x(v), y: H - 8, 'text-anchor': i === 0 ? 'start' : i === 4 ? 'end' : 'middle' }, i === 4 ? `${compact(v)} queries` : compact(v))); }
    const d = points.map(([a, b], i) => `${i ? 'L' : 'M'}${x(a).toFixed(1)},${y(b).toFixed(1)}`).join('');
    svg.append(s('path', { class: 'area', d: `${d}L${x(xmax)},${y(0)}L${x(0)},${y(0)}Z` }), s('path', { class: 'line', d }), s('circle', { class: 'dot', cx: x(xmax), cy: y(ymax), r: 4 }));
    box.replaceChildren(svg);
  };

  const render = (S) => {
    const T = S.totals;
    document.querySelectorAll('[data-n]').forEach((e) => { e.textContent = int(T[e.dataset.n]); });
    $('generated').textContent = S.generated;
    if (S.links && S.links.repo) { const a = el('a', '', 'jobscript-harvester'); a.href = S.links.repo; $('repo-link').replaceChildren(a); }
    const tiles = [
      ['Unique jobscripts', int(T.scripts), `${int(T.occurrences)} files, deduplicated by content`],
      ['Repositories with jobscripts', int(T.repos_with_scripts), `of ${int(T.repos_scanned)} scanned`],
      ['Script text', `${(T.bytes / 1e9).toFixed(2)} GB`, 'uncompressed, duplicates excluded'],
      ['Search queries', int(T.queries), `${int(T.hits)} hits`],
      ['Files read while scanning', compact(T.files_read), `${pct(T.occurrences, T.files_read)} were jobscripts`],
      ['Scripts with parsed labels', pct(T.scripts, T.scripts), `${int(T.label_rows)} label records`],
    ];
    $('tiles').replaceChildren(...tiles.map(([k, v, s]) => { const t = el('div', 'tile'); t.append(el('div', 'k', k), el('div', 'v', v), el('div', 's', s)); return t; }));

    bars($('funnel'), S.funnel);
    lineChart($('discovery'), S.discovery);
    const tb = $('search-table').tBodies[0]; tb.replaceChildren();
    for (const r of S.search) { const tr = el('tr'); tr.append(el('td', '', NAMES[r.directive] || r.directive), ...[r.queries, r.with_results, r.empty, r.saturated, r.hits, r.repos].map((v) => el('td', 'n', int(v)))); tb.append(tr); }
    if (T.capped_leaves !== null && T.capped_leaves !== undefined) $('capped-note').textContent = `${int(T.capped_leaves)} queries were still saturated after the last available split, so results beyond their first 100 could not be reached.`;

    const scripts = T.scripts;
    bars($('by-manager'), S.managers.map((m) => [NAMES[m.manager], m.scripts]), { whole: scripts });
    $('multi-note').textContent = `${int(S.multi_manager_scripts)} scripts carry directives for more than one manager and count under each.`;
    cols($('sizes'), S.sizes);
    cols($('per-repo'), S.scripts_per_repo.map((r) => [r[0], r[1]]));
    const tail = S.scripts_per_repo[S.scripts_per_repo.length - 1];
    $('tail-note').textContent = `The ${int(tail[1])} repositories with over 1,000 files hold ${pct(tail[2], T.occurrences)} of all kept files.`;
    cols($('duplication'), S.duplication);
    bars($('extensions'), S.extensions);
    bars($('licenses'), S.licenses);
    $('license-note').textContent = `Repositories with scripts, by license reported by GitHub. License data has been fetched for ${int(T.repos_with_metadata)} of ${int(T.repos_discovered)} repositories so far (${pct(T.repos_with_metadata, T.repos_discovered)}), so this is a partial view.`;
    bars($('top-repos'), S.top_repos);
    $('reuse-note').textContent = `Scripts are copied from public repositories and remain under their repositories' terms. A license has been recorded for ${int(T.repos_with_license)} repositories so far; where the license field is empty, no reuse rights should be assumed. Scripts can contain account names, email addresses, and home paths.`;
    const dl = $('data-link');
    if (S.links && S.links.data) { const a = el('a', '', S.links.data); a.href = S.links.data; dl.replaceChildren(document.createTextNode('The full corpus, with every script\'s content, is available at '), a, document.createTextNode('.')); }
    else dl.textContent = 'The full corpus, with every script\'s content, is published as a set of compressed files alongside the SQLite database it was built from.';

    // Labels, per manager
    const sel = $('label-manager');
    for (const l of S.labels) sel.append(new Option(NAMES[l.manager], l.manager));
    const drawLabels = () => {
      const l = S.labels.find((x) => x.manager === sel.value);
      $('label-rows').textContent = `${int(l.rows)} scripts`;
      bars($('coverage'), l.coverage.filter((c) => c[1] > 0).map(([k, v]) => [COVER[k], v]), { whole: l.rows });
      bars($('options'), l.options, { whole: l.rows });
      cols($('walltime'), l.walltime); cols($('nodes'), l.nodes);
      $('parse-note').textContent = `${int(l.with_errors)} ${NAMES[l.manager]} scripts have a directive value the parser could not read, and ${int(l.with_unknown)} use an option outside the manager's documented set. Both are kept in the labels for inspection.`;
    };
    sel.addEventListener('change', drawLabels); drawLabels();

    // Sample browser
    $('sample-size').textContent = int(Math.max(...Object.values(S.sample.per_manager)));
    $('sample-max').textContent = int(S.sample.max_bytes);
    const bm = $('b-manager'); for (const m of S.managers) bm.append(new Option(NAMES[m.manager], m.manager));
    const cache = {}; let rows = [], shown = null;
    const detail = (r) => {
      const box = $('b-detail'); box.replaceChildren();
      box.append(el('h3', '', 'Where it was found'));
      const kv = el('dl', 'kv'); const add = (k, v) => { if (v === null || v === undefined || v === '') return; const dd = el('dd'); if (v instanceof Node) dd.append(v); else dd.textContent = v; kv.append(el('dt', '', k), dd); };
      const a = el('a', '', `${r.repo} / ${r.path}`); a.href = r.url; a.target = '_blank'; a.rel = 'noopener';
      add('File', a); add('Commit', el('span', 'mono', r.commit.slice(0, 12))); add('Blob SHA', el('span', 'mono', r.blob_sha)); add('License', r.license || 'not recorded');
      add('Size', `${int(r.size)} bytes`); add('Found in', r.n_occurrences > 1 ? `${int(r.n_occurrences)} places` : null);
      box.append(kv, el('h3', '', 'Parsed labels'));
      const lk = el('dl', 'kv'); const put = (k, v) => { if (v !== null && v !== undefined && v !== '') lk.append(el('dt', '', k), el('dd', '', String(v))); };
      put('Nodes', r.nodes); put('Tasks', r.tasks); put('CPUs per task', r.cpus_per_task); put('GPUs', r.gpus !== null ? r.gpus : (r.gpu_request ? 'requested' : null));
      put('Memory', r.memory_mib !== null ? `${int(r.memory_mib)} MiB per ${r.memory_per}` : null); put('Walltime', duration(r.walltime_seconds)); put('Queue', r.queue); put('Array size', r.array_count);
      if (!lk.children.length) lk.append(el('dt', '', 'Resources'), el('dd', '', 'none stated in directives'));
      box.append(lk);
      if (r.options.length) { box.append(el('h3', '', 'Options set')); const c = el('div', 'chips'); for (const [o, v] of r.options) c.append(el('span', 'chip', v === null || v === '' ? o : `${o}=${v}`)); box.append(c); }
      box.append(el('h3', '', 'Script'));
      const pre = el('pre'); for (const line of r.content.split('\n')) pre.append(el('span', DIRECTIVE.test(line) ? 'd' : '', line)); box.append(pre); box.scrollTop = 0;
    };
    const list = () => {
      const q = $('b-text').value.trim().toLowerCase(), gpu = $('b-gpu').checked;
      const hits = rows.filter((r) => (!gpu || r.gpu_request) && (!q || r.content.toLowerCase().includes(q) || r.repo.toLowerCase().includes(q) || r.path.toLowerCase().includes(q)));
      $('b-count').textContent = `${int(hits.length)} of ${int(rows.length)} sample scripts`;
      const box = $('b-list'); box.replaceChildren();
      for (const r of hits) { const b = el('button', 'item'); b.type = 'button'; b.setAttribute('role', 'option'); b.setAttribute('aria-selected', String(r === shown)); b.append(el('div', 'r', r.repo), el('div', 'p', r.path)); b.addEventListener('click', () => { shown = r; for (const o of box.children) o.setAttribute('aria-selected', 'false'); b.setAttribute('aria-selected', 'true'); detail(r); }); box.append(b); }
      if (!hits.length) { const none = el('p', 'muted', 'No sample scripts match.'); none.style.padding = '.75rem'; box.append(none); }
    };
    const load = async () => {
      const m = bm.value; $('b-count').textContent = 'Loading…';
      try { cache[m] = cache[m] || await (await fetch(`data/sample-${m}.json`)).json(); } catch (e) { $('b-count').textContent = 'The sample could not be loaded.'; return; }
      rows = cache[m]; shown = rows[0] || null; list(); if (shown) detail(shown);
    };
    bm.addEventListener('change', load); $('b-text').addEventListener('input', list); $('b-gpu').addEventListener('change', list);
    load();
  };

  fetch('data/stats.json').then((r) => r.json()).then(render).catch(() => {
    $('tiles').replaceChildren(el('p', 'muted', 'The data files could not be loaded. If you opened this page from disk, serve the folder instead: python3 -m http.server -d docs'));
  });
})();
