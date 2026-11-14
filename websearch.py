"""Discover repositories with jobscripts through GitHub's web code search."""

import random
import re
import sys
import time
from html.parser import HTMLParser
from urllib.parse import quote_plus

from store import add_source

SEARCH_URL = "https://github.com/search?q={q}&type=code&p={page}"
# Web code search returns at most 100 results (5 pages) per query
# (docs.github.com/search-github/github-code-search/about-github-code-search).
RESULT_CAP = 100
MAX_PAGES = 5
RATE_LIMIT_MARKERS = ("secondary rate limit", "whoa there")
BLOB_HREF = re.compile(r"^/([^/]+)/([^/]+)/blob/([^/]+)/([^#?]+)")


class _Links(HTMLParser):
    """Collect hrefs of anchors, separating search-result anchors."""

    def __init__(self):
        super().__init__()
        self.results, self.blobs, self.login = [], [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name") == "user-login":
            self.login = attrs.get("content") or ""
        if tag != "a" or not attrs.get("href"):
            return
        if attrs.get("data-testid") == "link-to-search-result":
            self.results.append(attrs["href"])
        if BLOB_HREF.match(attrs["href"]):
            self.blobs.append(attrs["href"])


def parse_page(html):
    """Return (logged_in, hits) where hits are (repo, ref, path) from a results page."""
    parser = _Links()
    parser.feed(html)
    hrefs = parser.results or parser.blobs
    hits, seen = [], set()
    for href in hrefs:
        match = BLOB_HREF.match(href)
        if match:
            owner, name, ref, path = match.groups()
            hit = (f"{owner}/{name}", ref, path)
            if hit not in seen:
                seen.add(hit)
                hits.append(hit)
    return bool(parser.login), hits


def _is_group(value):
    """Return True if value is one parenthesized group."""
    if not (value.startswith("(") and value.endswith(")")):
        return False
    depth, quoted = 0, False
    for i, ch in enumerate(value):
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch in "()":
            depth += 1 if ch == "(" else -1
            if depth == 0 and i < len(value) - 1:
                return False
    return True


def negate(value):
    """Return NOT applied to one query clause."""
    if _is_group(value) or re.fullmatch(r'"(?:[^"\\]|\\.)*"|\S+', value):
        return f"NOT {value}"
    return f"NOT ({value})"


def children(plan, directive, clauses, depth):
    """Return (clauses, depth) for the next split below a saturated query."""
    split = plan["split"]
    while depth < len(split):
        family = split[depth]
        values = family["values"]
        if isinstance(values, dict):
            values = values.get(directive, [])
        if values:
            out = [(clauses + [v], depth + 1) for v in values]
            if family.get("complement"):
                out.append((clauses + [" ".join(negate(v) for v in values)], depth + 1))
            return out
        depth += 1
    return []


def build_query(plan, directive, clauses):
    """Return the full query string for a directive and its clauses."""
    parts = [plan["directives"][directive], plan.get("base", "")] + clauses
    return " ".join(p for p in parts if p)


class Browser:
    """Selenium-driven Chrome using a persistent profile for the GitHub login."""

    def __init__(self, profile_dir, headless=False, delay=(1.0, 3.0)):
        from selenium import webdriver

        options = webdriver.ChromeOptions()
        options.add_argument(f"--user-data-dir={profile_dir}")
        if headless:
            options.add_argument("--headless=new")
        self.driver = webdriver.Chrome(options=options)
        self.driver.set_page_load_timeout(30)
        self.delay = delay

    def get(self, url, max_tries=6):
        """Load url and return its HTML, waiting out rate-limit pages."""
        backoff = 60
        for _ in range(max_tries):
            self.driver.get(url)
            time.sleep(random.uniform(*self.delay))
            html = self.driver.page_source
            if not any(m in html.lower() for m in RATE_LIMIT_MARKERS):
                return html
            print(f"rate limited; sleeping {backoff}s", file=sys.stderr)
            time.sleep(backoff)
            backoff *= 2
        raise RuntimeError(f"still rate limited after {max_tries} tries: {url}")

    def ensure_login(self):
        """Block until the browser profile is logged in to GitHub."""
        while True:
            logged_in, _ = parse_page(self.get("https://github.com/"))
            if logged_in:
                return
            input("Log in to GitHub in the browser window, then press Enter: ")

    def close(self):
        """Close the browser."""
        self.driver.quit()


CANARY = '"#SBATCH"'  # a query that always has results while search is working


def run_query(db, browser, query, start_page):
    """Fetch result pages for query from start_page; return (hits, next_page, first_html)."""
    page, total, first_html = start_page, [], None
    while page <= MAX_PAGES:
        html = browser.get(SEARCH_URL.format(q=quote_plus(query), page=page))
        first_html = first_html if first_html is not None else html
        logged_in, hits = parse_page(html)
        if not logged_in:
            raise RuntimeError("browser session is not logged in to GitHub")
        if not hits:
            break
        for repo, ref, path in hits:
            db.execute("INSERT OR IGNORE INTO search_hits VALUES (?,?,?,?)",
                       (repo, ref, path, query))
            add_source(db, repo, "websearch")
        total += hits
        page += 1
        db.execute("UPDATE queries SET next_page=? WHERE query=?", (page, query))
        db.commit()
    return total, page, first_html


def search_working(browser):
    """Return True if the canary query returns results."""
    _, hits = parse_page(browser.get(SEARCH_URL.format(q=quote_plus(CANARY), page=1)))
    return bool(hits)


def save_page(pages_dir, query, html):
    """Write a result page to pages_dir for inspection; return its path."""
    import hashlib
    import os

    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, hashlib.sha1(query.encode()).hexdigest()[:16] + ".html")
    with open(path, "w") as fh:
        fh.write(f"<!-- {query} -->\n{html}")
    return path


def search(db, browser, plan, directives=None, max_queries=None, pages_dir="empty-pages",
           retry_empty=False, sleep=time.sleep, blocked_tries=4):
    """Run the adaptive search plan; return the number of queries executed.

    Queries run breadth-first across all directives, so an interrupted run has
    coarse coverage of every directive before any branch is refined. A query with
    no results is checked against a canary query. If the canary also has none,
    search is treated as blocked: wait and retry, then stop. Otherwise the query
    is stored with status 'empty' and its page is saved.
    """
    from collections import deque

    executed = 0
    queue = deque((d, [], 0) for d in (directives or plan["directives"]))
    while queue:
        if max_queries is not None and executed >= max_queries:
            return executed
        directive, clauses, depth = queue.popleft()
        query = build_query(plan, directive, clauses)
        row = db.execute("SELECT status, saturated, next_page, results FROM queries "
                         "WHERE query=?", (query,)).fetchone()
        rerun = retry_empty and row and (row[0] == "empty" or (row[0] == "done" and row[3] == 0))
        if row and row[0] in ("done", "empty") and not rerun:
            if row[1]:
                queue.extend((directive, c, d) for c, d in children(plan, directive, clauses, depth))
            continue
        if rerun:
            db.execute("UPDATE queries SET status='running', next_page=1 WHERE query=?", (query,))
        start = row[2] if row and not rerun else 1
        if not row:
            db.execute("INSERT INTO queries VALUES (?,?,?,?,?,?,?)",
                       (query, directive, depth, 0, 0, "running", 1))
        for attempt in range(blocked_tries + 1):
            _, next_page, html = run_query(db, browser, query, start)
            results = db.execute("SELECT COUNT(*) FROM search_hits WHERE query=?",
                                 (query,)).fetchone()[0]
            if results or search_working(browser):
                break
            if attempt == blocked_tries:
                path = save_page(pages_dir, query, html)
                raise RuntimeError(f"search returned no results, including the canary query "
                                   f"{CANARY}; GitHub may be blocking searches. Page: {path}")
            wait = 120 * 2 ** attempt
            print(f"no results and canary empty; sleeping {wait}s", file=sys.stderr)
            sleep(wait)
        executed += 1
        status = "done" if results else "empty"
        if not results:
            save_page(pages_dir, query, html)
        # Saturated if the cap was reached or the last allowed page still had results.
        saturated = int(results >= RESULT_CAP or next_page > MAX_PAGES)
        db.execute("UPDATE queries SET results=?, saturated=?, status=? WHERE query=?",
                   (results, saturated, status, query))
        db.commit()
        print(f"[{directive} d={depth}] {results:3d}{' saturated' if saturated else ''}"
              f"{' empty' if not results else ''}: {query}", file=sys.stderr)
        if saturated:
            queue.extend((directive, c, d) for c, d in children(plan, directive, clauses, depth))
    return executed


def frontier(db, plan, directives=None):
    """Walk the plan against stored queries without searching.

    Returns counts: done, empty, pending (queries the next run would start with),
    and truncated (saturated queries with no further split). Pending queries may
    add more pending queries below them if they turn out saturated.
    """
    from collections import deque

    counts = {"done": 0, "empty": 0, "pending": 0, "truncated": 0}
    pending_by_directive = {}
    queue = deque((d, [], 0) for d in (directives or plan["directives"]))
    while queue:
        directive, clauses, depth = queue.popleft()
        row = db.execute("SELECT status, saturated FROM queries WHERE query=?",
                         (build_query(plan, directive, clauses),)).fetchone()
        if not row or row[0] not in ("done", "empty"):
            counts["pending"] += 1
            pending_by_directive[directive] = pending_by_directive.get(directive, 0) + 1
            continue
        counts[row[0]] += 1
        if row[1]:
            kids = children(plan, directive, clauses, depth)
            if not kids:
                counts["truncated"] += 1
            queue.extend((directive, c, d) for c, d in kids)
    counts["pending_by_directive"] = pending_by_directive
    return counts


def repo_yield(db, batch=100, exclude_source=None):
    """New repositories first found by each batch of queries, in the order queries ran."""
    rows = db.execute("""
        WITH first AS (SELECT repo, MIN(rowid) AS hit FROM search_hits GROUP BY repo)
        SELECT (q.rowid - 1) / ?, COUNT(*) FROM first f
        JOIN search_hits h ON h.rowid = f.hit
        JOIN queries q ON q.query = h.query
        WHERE ? IS NULL OR NOT EXISTS (
            SELECT 1 FROM repo_sources s WHERE s.repo = f.repo AND s.source = ?)
        GROUP BY 1 ORDER BY 1""", (batch, exclude_source, exclude_source)).fetchall()
    return [(b * batch + 1, (b + 1) * batch, n) for b, n in rows]
