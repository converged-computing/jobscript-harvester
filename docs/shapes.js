/* Renders docs/data/shapes.json. No dependencies. */
(() => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; };
  const int = (v) => Number(v).toLocaleString('en');
  const share = (v) => `${v < 10 ? v.toFixed(1) : Math.round(v)}%`;
  const NAMES = { slurm: 'Slurm', pbs: 'PBS', lsf: 'LSF', flux: 'Flux', oar: 'OAR', cobalt: 'Cobalt' };
  const ORDER = Object.keys(NAMES);
  const FIELDS = { groups: 'Nodes or chunks', tasks: 'Tasks', cores: 'Cores', gpus: 'GPUs', memory_mib: 'Memory', duration: 'Walltime' };
  const STATED = { specified_groups: 'Nodes or chunks', specified_tasks: 'Tasks', specified_tasks_per_group: 'Tasks per node', specified_cores_per_task: 'Cores per task', specified_cores_per_group: 'Cores per node or chunk', specified_gpus: 'GPUs', specified_memory: 'Memory', no_resources: 'No resources at all', assumed_tasks: 'Task count assumed', assumed_cores: 'Core count assumed' };
  const duration = (s) => { if (s === null || s === undefined) return ''; if (s < 3600) return `${Math.round(s / 60)} min`; const h = s / 3600; return h < 48 ? `${+h.toFixed(1)} h` : `${+(h / 24).toFixed(1)} d`; };
  const memory = (mib) => (mib === null || mib === undefined ? '' : mib < 1024 ? `${Math.round(mib)} MiB` : `${+(mib / 1024).toFixed(1)} GiB`);
  const label = (token) => token.replace(/^run:/, '').replace(/^module:/, '');
  const kind = (token) => (token.startsWith('module:') ? 'module' : 'program');

  // Horizontal bars: rows of [label, percent, detail].
  const bars = (box, rows) => {
    box.replaceChildren();
    const max = Math.max(1, ...rows.map((r) => r[1]));
    for (const [text, v, detail] of rows) {
      const l = el('span', 'l', text); l.title = text;
      const t = el('span', 't'); const i = el('i'); i.style.width = `${100 * v / max}%`; t.append(i);
      box.append(l, t, el('span', 'v', detail ? `${share(v)} · ${detail}` : share(v)));
    }
    if (!rows.length) box.append(el('span', 'muted', 'Nothing to show.'));
  };

  const RESOURCES = { groups: ['Nodes or chunks', 'nodes or chunks'], tasks: ['Tasks', 'tasks'], cores: ['Cores', 'cores in total'], gpus: ['GPUs', 'GPUs in total'], memory_mib: ['Memory', 'memory in total'], duration: ['Walltime', 'walltime limit'], array_count: ['Array size', 'array elements'] };
  const ATTRIBUTES = { walltime_seconds: 'Walltime limit', job_name: 'Job name', queue: 'Queue or partition', account: 'Account or project', qos: 'Quality of service', constraint: 'Constraint or feature', array: 'Job array', dependency: 'Depends on another job', exclusive: 'Exclusive nodes', email: 'Mail address' };
  const VALUES = { queue: ['Queues and partitions', 'The names people use most.'], qos: ['Quality of service', 'The names people use most.'], constraint: ['Constraints', 'Node features people ask for.'], gpu_types: ['GPU models', 'Most GPU requests don\'t name a model. These are the ones that do.'], memory_per: ['How memory is requested', 'Per node, per CPU, per job and so on.'], dependency: ['Dependencies', 'How jobs wait on other jobs.'] };
  const UNIT = ['', 'scripts', 'repositories'];
  // Ranges are stored as "3-4", "1025+", "<=4G", ">7d"; a column is labelled by where it ends.
  const short = (range) => range.replace(/^\d+-/, '').replace(/^<=/, '').replace(/^(\d+)\+$/, (_, n) => `>${n - 1}`);
  const full = (range, what) => `${range.replace(/^<=/, 'up to ').replace(/^>/, 'over ').replace(/\+$/, ' or more').replace('-', ' to ')} ${what}`;

  const tip = $('tip');
  const hint = (node, value, text) => {
    const show = (x, y) => { tip.replaceChildren(el('b', null, value), document.createTextNode(text)); tip.hidden = false; const w = tip.offsetWidth || 0, h = tip.offsetHeight || 0; tip.style.left = `${Math.max(4, Math.min(x + 12, window.innerWidth - w - 4))}px`; tip.style.top = `${Math.max(4, y - h - 10)}px`; };
    node.addEventListener('pointermove', (e) => show(e.clientX, e.clientY));
    node.addEventListener('pointerleave', () => { tip.hidden = true; });
    node.addEventListener('focus', () => { const r = node.getBoundingClientRect(); show(r.left + r.width / 2, r.top + 20); });
    node.addEventListener('blur', () => { tip.hidden = true; });
  };

  // Column chart: rows of [range, height, value text, tooltip text]. The tallest column carries its value.
  const columns = (box, rows, what, max, axis) => {
    const top = Math.max(...rows.map((r) => r[1]));
    max = max || top || 1;
    const hist = el('div', 'hist'); hist.setAttribute('role', 'list');
    for (const [range, v, value, text] of rows) {
      const c = el('div', 'c'); c.tabIndex = 0; c.setAttribute('role', 'listitem'); c.setAttribute('aria-label', `${full(range, what)}: ${value}, ${text}`);
      const p = el('div', 'p'); const b = el('div', v ? 'b' : 'b zero'); b.style.height = v ? `max(2px, ${100 * v / max}%)` : '0';
      if (v && v === top) b.append(el('span', null, value));
      p.append(b); c.append(p, el('div', 'x', short(range)));
      hint(c, value, `${full(range, what)} · ${text}`);
      hist.append(c);
    }
    box.append(hist, el('p', 'axis', axis));
  };
  const asTable = (box, head, rows) => {
    const d = el('details', 'table'); d.append(el('summary', null, 'Show as table'));
    const t = el('table'), h = el('tr'); head.forEach((x, i) => h.append(el('th', i ? 'n' : null, x)));
    const thead = el('thead'); thead.append(h); const body = el('tbody');
    for (const r of rows) { const tr = el('tr'); r.forEach((x, i) => tr.append(el('td', i ? 'n' : null, x))); body.append(tr); }
    t.append(thead, body); const scroll = el('div', 'scroll'); scroll.append(t); d.append(scroll); box.append(d);
  };

  const matrix = (table, D, key, scale) => {
    const present = ORDER.filter((m) => D.managers[m]);
    const value = (a, b) => { const r = D.similarity.find((s) => (s.a === a && s.b === b) || (s.a === b && s.b === a)); return r ? r[key] : null; };
    const head = el('tr'); head.append(el('th'));
    present.forEach((m) => head.append(el('th', 'n', NAMES[m])));
    const thead = el('thead'); thead.append(head);
    const body = el('tbody');
    for (const a of present) {
      const row = el('tr'); const th = el('th', null, NAMES[a]); th.scope = 'row'; row.append(th);
      for (const b of present) {
        const v = a === b ? null : value(a, b);
        const td = el('td', 'n', v === null ? '·' : v.toFixed(2));
        if (v !== null) td.style.background = `color-mix(in srgb, var(--bar) ${Math.round(Math.min(1, v / scale) * 55)}%, transparent)`;
        row.append(td);
      }
      body.append(row);
    }
    table.replaceChildren(thead, body);
  };

  const render = (D) => {
    if (!D.resources) throw new Error('it is out of date');
    const present = ORDER.filter((m) => D.managers[m]);
    $('n-shapes').textContent = int(D.shapes);
    $('generated').textContent = D.generated;
    const slurm = D.managers.slurm;
    const tile = (k, v, s) => { const t = el('div', 'tile'); t.append(el('div', 'k', k), el('div', 'v', v), el('div', 's', s)); return t; };
    $('tiles').append(
      tile('Shapes', int(D.shapes), 'one per script and manager'),
      tile('Distinct exact shapes', int(slurm.distinct_exact), 'in Slurm scripts'),
      tile('Distinct binned shapes', int(slurm.distinct_binned), 'in Slurm scripts'),
      tile('Tree structures', int(slurm.distinct_topology), 'in Slurm scripts'),
      tile('Programs and modules', int(D.tokens.length), `named in ${D.min_repos}+ repositories`));

    const select = $('m-manager');
    const selects = [...document.querySelectorAll('select.mgr')], measures = [...document.querySelectorAll('select.measure')];
    selects.forEach((s) => present.forEach((m) => { const o = el('option', null, NAMES[m]); o.value = m; s.append(o); }));
    Object.keys(RESOURCES).forEach((r) => { const o = el('option', null, RESOURCES[r][0]); o.value = r; $('x-resource').append(o); });

    const histogram = (card, m, r, by, max) => {
      const total = [0, D.managers[m].scripts, D.managers[m].repos][by], entry = D.resources[m][r];
      const pct = (n) => share(100 * n / total);
      const rows = entry.bins.map((b) => [b[0], max ? 100 * b[by] / total : b[by], max ? pct(b[by]) : int(b[by]), max ? `${int(b[by])} ${UNIT[by]}` : `${pct(b[by])} of ${UNIT[by]}`]);
      if (!entry.stated[by - 1]) { card.append(el('p', 'muted', 'No script gives this.')); return; }
      columns(card, rows, RESOURCES[r][1], max, `${RESOURCES[r][0]} requested (up to)`);
      asTable(card, ['Range', 'Repositories', 'Scripts'], entry.bins.map((b) => [b[0], int(b[2]), int(b[1])]));
    };
    const resources = () => {
      const m = select.value, by = Number(measures[0].value), total = [0, D.managers[m].scripts, D.managers[m].repos][by];
      $('r-count').textContent = `${int(total)} ${UNIT[by]} in all`;
      const box = $('r-charts'); box.replaceChildren();
      for (const r of Object.keys(RESOURCES)) {
        const n = D.resources[m][r].stated[by - 1];
        const card = el('div', 'card'); card.append(el('h3', null, RESOURCES[r][0]), el('p', 'note', `${int(n)} ${UNIT[by]} give this (${share(100 * n / total)}).`));
        histogram(card, m, r, by); box.append(card);
      }
    };
    const across = () => {
      const r = $('x-resource').value, by = Number(measures[0].value), box = $('x-charts'); box.replaceChildren();
      const total = (m) => [0, D.managers[m].scripts, D.managers[m].repos][by];
      const max = Math.max(1e-9, ...present.flatMap((m) => D.resources[m][r].bins.map((b) => 100 * b[by] / total(m))));
      for (const m of present) {
        const n = D.resources[m][r].stated[by - 1];
        const card = el('div', 'card'); card.append(el('h3', null, NAMES[m]), el('p', 'note', `${int(n)} of ${int(total(m))} ${UNIT[by]} give this (${share(100 * n / total(m))}).`));
        histogram(card, m, r, by, max); box.append(card);
      }
    };
    const attributes = () => {
      const m = select.value, by = Number(measures[0].value), total = [0, D.managers[m].scripts, D.managers[m].repos][by];
      $('a-note').textContent = `Percent of ${int(total)} ${NAMES[m]} ${UNIT[by]}.`;
      const row = ([name, ...n]) => [name, 100 * n[by - 1] / total, `${int(n[by - 1])} ${by === 2 ? 'repos' : 'scripts'}`];
      bars($('a-set'), D.attributes[m].map(([a, ...n]) => row([ATTRIBUTES[a] || a, ...n])).sort((a, b) => b[1] - a[1]));
      const box = $('a-values'); box.replaceChildren();
      for (const v of Object.keys(VALUES)) {
        const rows = (D.values[m][v] || []).map(row).sort((a, b) => b[1] - a[1]).slice(0, 12);
        if (!rows.length) continue;
        const card = el('div', 'card'), list = el('div', 'bars'); card.append(el('h3', null, VALUES[v][0]), el('p', 'note', VALUES[v][1]), list);
        bars(list, rows); box.append(card);
      }
      if (!box.children.length) box.append(el('p', 'muted', 'Nothing here is used by five or more repositories.'));
    };

    const manager = () => {
      const m = select.value, info = D.managers[m], level = $('m-level').value;
      $('m-count').textContent = `${int(info.scripts)} scripts in ${int(info.repos)} repositories`;
      bars($('m-motifs'), D.motifs[level][m].map(([motif, pct, repos]) => [motif, pct, `${int(repos)} repos`]));
      bars($('m-topology'), D.topology.filter((t) => t.share[m] > 0).sort((a, b) => b.share[m] - a.share[m]).map((t) => [t.motif, t.share[m]]));
      bars($('m-specified'), Object.keys(STATED).filter((k) => info.pct[k]).map((k) => [STATED[k], info.pct[k]]));
      const body = $('m-fields').tBodies[0]; body.replaceChildren();
      for (const f of Object.keys(FIELDS)) {
        const q = D.fields[m][f]; if (!q || q.p50 === null) continue;
        const show = f === 'duration' ? duration : f === 'memory_mib' ? memory : int;
        const row = el('tr'); row.append(el('td', null, FIELDS[f]), el('td', 'n', share(q.repo_share_pct)));
        ['p25', 'p50', 'p75', 'p90'].forEach((k) => row.append(el('td', 'n', show(q[k]))));
        body.append(row);
      }
      $('c-name').textContent = NAMES[m];
      const distinct = $('c-distinct').tBodies[0]; distinct.replaceChildren();
      for (const [motif, repos, mine, others] of D.distinctive[m]) {
        const row = el('tr'); row.append(el('td', 'mono', motif), el('td', 'n', int(repos)), el('td', 'n', share(mine)), el('td', 'n', share(others)));
        distinct.append(row);
      }
    };
    const all = () => { manager(); resources(); across(); attributes(); };
    selects.forEach((s) => s.addEventListener('change', () => { selects.forEach((o) => { o.value = s.value; }); all(); }));
    measures.forEach((s) => s.addEventListener('change', () => { measures.forEach((o) => { o.value = s.value; }); resources(); across(); attributes(); }));
    $('x-resource').addEventListener('change', across);
    $('m-level').addEventListener('change', manager);
    all();

    matrix($('c-resources'), D, 'resources', 0.7);
    matrix($('c-duration'), D, 'duration', 0.7);

    // Programs and modules.
    const list = $('p-list'), detail = $('p-detail');
    let selected = null;
    const open = (t) => {
      selected = t[0];
      const [token, repos, scripts, top, topPct, specific, gpu, groups, tasks, cores, dur] = t;
      detail.replaceChildren(el('h3', null, `${label(token)} (${kind(token)})`));
      const kv = el('dl', 'kv');
      const add = (k, v) => { if (v !== null && v !== undefined && v !== '') kv.append(el('dt', null, k), el('dd', null, v)); };
      add('Found in', `${int(repos)} repositories, ${int(scripts)} scripts`);
      add('Most common shape', top ? `${top} (${share(topPct)})` : 'none');
      add('Requests GPUs', share(gpu));
      add('Median request', [groups && `${int(groups)} node${groups === 1 ? '' : 's'}`, tasks && `${int(tasks)} task${tasks === 1 ? '' : 's'}`, cores && `${int(cores)} core${cores === 1 ? '' : 's'}`].filter(Boolean).join(', '));
      add('Median walltime', duration(dur));
      add('How unusual', `${specific.toFixed(2)} (0 is typical, 1 is unlike anything else)`);
      detail.append(kv);
      const near = D.neighbours[token];
      if (near) {
        detail.append(el('h3', null, 'Similar shapes'));
        const chips = el('div', 'chips');
        near.forEach(([name, d]) => { const c = el('button', 'chip link', `${label(name)} · ${d.toFixed(2)}`); c.title = `${kind(name)}, distance ${d}`; c.addEventListener('click', () => { const hit = D.tokens.find((x) => x[0] === name); if (hit) { open(hit); draw(); } }); chips.append(c); });
        detail.append(chips);
      }
      const per = D.by_manager[token];
      if (per) {
        detail.append(el('h3', null, 'Under each manager'));
        const table = el('table'); const head = el('tr');
        ['Manager', 'Repositories', 'Most common shape (totals)', 'Share'].forEach((h, i) => head.append(el('th', i === 1 || i === 3 ? 'n' : null, h)));
        const thead = el('thead'); thead.append(head); const body = el('tbody');
        per.forEach(([m, n, motif, pct]) => { const row = el('tr'); row.append(el('td', null, NAMES[m]), el('td', 'n', int(n)), el('td', 'mono', motif || 'none'), el('td', 'n', share(pct))); body.append(row); });
        table.append(thead, body);
        const scroll = el('div', 'scroll'); scroll.append(table); detail.append(scroll);
      }
    };
    const draw = () => {
      const text = $('p-text').value.trim().toLowerCase(), prefix = $('p-kind').value, by = Number($('p-sort').value);
      const rows = D.tokens.filter((t) => t[0].startsWith(prefix) && (!text || label(t[0]).includes(text))).sort((a, b) => (b[by] || 0) - (a[by] || 0));
      $('p-count').textContent = `${int(rows.length)} of ${int(D.tokens.length)}`;
      list.replaceChildren();
      for (const t of rows.slice(0, 300)) {
        const item = el('button', 'item'); item.setAttribute('role', 'option'); item.setAttribute('aria-selected', String(t[0] === selected));
        item.append(el('div', 'r', `${label(t[0])} · ${int(t[1])} repos`), el('div', 'p mono', t[3] || 'no resources requested'));
        item.addEventListener('click', () => { open(t); draw(); });
        list.append(item);
      }
      if (!rows.length) list.append(el('p', 'muted pad', 'No match.'));
    };
    ['p-text', 'p-kind', 'p-sort'].forEach((id) => $(id).addEventListener('input', draw));
    draw();
  };

  fetch('data/shapes.json', { cache: 'no-cache' }).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }).then(render)
    .catch((err) => { const p = el('p', 'muted', `Could not load data/shapes.json (${err.message}). Run counts.py and site.py in experiments/shapes, then serve this folder over HTTP.`); document.querySelector('main').prepend(p); });
})();
