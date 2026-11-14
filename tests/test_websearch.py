import pytest
import gzip
import json
import pathlib

import store
import websearch

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
PLAN = json.loads((pathlib.Path(__file__).parent.parent / "plan.json").read_text())


def page(name):
    return gzip.open(FIXTURES / name, "rt").read()


def test_logged_out_search_page_has_no_session_or_hits():
    logged_in, hits = websearch.parse_page(page("github_search_logged_out.html.gz"))
    assert not logged_in and hits == []


def test_blob_links_from_real_tree_page():
    _, hits = websearch.parse_page(page("github_tree_page.html.gz"))
    paths = {p for _, _, p in hits}
    assert "t/batch/directives/valid/001-simple.sh" in paths
    assert all(repo == "flux-framework/flux-core" for repo, _, _ in hits)


def test_result_anchors_take_precedence_over_other_blob_links():
    # data-testid selector taken from jobspec-database scripts/search.py
    html = ('<meta name="user-login" content="someone">'
            '<a href="/a/b/blob/main/other.sh">x</a>'
            '<a data-testid="link-to-search-result" href="/o/r/blob/abc123/run.sh#L2">r</a>')
    assert websearch.parse_page(html) == (True, [("o/r", "abc123", "run.sh")])


def test_negate():
    assert websearch.negate("path:*.sh") == "NOT path:*.sh"
    assert websearch.negate('"#SBATCH --gres"') == 'NOT "#SBATCH --gres"'
    assert websearch.negate('("# flux:" OR "#flux:")') == 'NOT ("# flux:" OR "#flux:")'


def test_children_with_complement_and_per_directive_values():
    kids = websearch.children(PLAN, "slurm", [], 0)
    assert [c for c, _ in kids] == [["language:Shell"], ["language:Python"],
                                    ["NOT language:Shell NOT language:Python"]]
    # option family (index 4) is per directive
    option = websearch.children(PLAN, "lsf", ["x"], 4)
    assert option[0][0] == ["x", '"#BSUB -n"'] and option[0][1] == 5


def test_children_skip_families_without_values():
    plan = {"split": [{"values": {"slurm": ["a"]}}, {"values": ["b"]}]}
    assert websearch.children(plan, "pbs", [], 0) == [(["b"], 2)]
    assert websearch.children(plan, "pbs", [], 2) == []


def test_build_query():
    q = websearch.build_query(PLAN, "slurm", ["language:Shell"])
    assert q == '"#SBATCH" NOT is:fork language:Shell'


class FakeBrowser:
    """Serves 20 hits per page; queries with fewer than 2 clauses saturate."""

    def __init__(self):
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(url).query)
        query, page_no = qs["q"][0], int(qs["p"][0])
        clauses = query.count("language:") + query.count("is:archived")
        n = 5 if clauses >= 2 else 20
        if clauses >= 2 and page_no > 1:
            n = 0
        links = "".join(f'<a data-testid="link-to-search-result" '
                        f'href="/o/r{abs(hash(query)) % 97}/blob/c/{page_no}-{i}.sh">x</a>'
                        for i in range(n))
        return '<meta name="user-login" content="me">' + links


def small_plan():
    return {"base": "NOT is:fork", "directives": {"slurm": '"#SBATCH"'},
            "split": [{"complement": True, "values": ["language:Shell"]},
                      {"complement": True, "values": ["is:archived"]}]}


def test_adaptive_search_splits_saturated_queries(tmp_path):
    db = store.connect(tmp_path / "h.sqlite")
    n = websearch.search(db, FakeBrowser(), small_plan())
    rows = dict(db.execute("SELECT query, saturated FROM queries").fetchall())
    # root (saturated) -> 2 language children (saturated) -> 4 leaves (not saturated)
    assert n == 7 and len(rows) == 7
    assert sum(rows.values()) == 3


def test_search_resume_skips_done_queries(tmp_path):
    db = store.connect(tmp_path / "h.sqlite")
    websearch.search(db, FakeBrowser(), small_plan(), max_queries=3)
    browser = FakeBrowser()
    remaining = websearch.search(db, browser, small_plan())
    assert remaining == 4
    assert db.execute("SELECT COUNT(*) FROM queries WHERE status='done'").fetchone()[0] == 7


def test_harvest_plan_drops_license_search():
    import harvest
    plan = harvest.without_license_search(PLAN)
    assert all(f["family"] != "license" for f in plan["split"])
    queries = [c for c, _ in websearch.children(plan, "slurm", ["x"], 3)]
    assert not any("license:" in " ".join(q) for q in queries)
    assert len(plan["split"]) == len(PLAN["split"]) - 1


class SelectiveBrowser:
    """20 hits per page for queries without `empty_term`; none for those with it."""

    def __init__(self, empty_term=None, blocked=False):
        self.empty_term, self.blocked, self.urls = empty_term, blocked, []

    def get(self, url):
        from urllib.parse import parse_qs, urlparse
        self.urls.append(url)
        qs = parse_qs(urlparse(url).query)
        query, page_no = qs["q"][0], int(qs["p"][0])
        empty = self.blocked or (self.empty_term and self.empty_term in query) or page_no > 1
        links = "" if empty else "".join(
            f'<a data-testid="link-to-search-result" href="/o/r{abs(hash(query)) % 97}/blob/c/{i}.sh">x</a>'
            for i in range(5))
        return '<meta name="user-login" content="me">' + links


def flat_plan():
    return {"base": "", "directives": {"pbs": '"#PBS"'},
            "split": [{"complement": True, "values": ["language:Python"]}]}


def test_genuine_empty_query_is_stored_and_page_saved(tmp_path):
    db = store.connect(tmp_path / "h.sqlite")
    # root query not saturated (5 hits), so no split; make the root itself empty
    browser = SelectiveBrowser(empty_term='"#PBS"')
    websearch.search(db, browser, flat_plan(), pages_dir=str(tmp_path / "pages"))
    status, results = db.execute("SELECT status, results FROM queries").fetchone()
    assert (status, results) == ("empty", 0)
    assert len(list((tmp_path / "pages").iterdir())) == 1
    assert any(websearch.CANARY in u or "%23SBATCH" in u for u in browser.urls)


def test_blocked_search_waits_then_stops(tmp_path):
    db = store.connect(tmp_path / "h.sqlite")
    waits = []
    with pytest.raises(RuntimeError, match="canary"):
        websearch.search(db, SelectiveBrowser(blocked=True), flat_plan(),
                         pages_dir=str(tmp_path / "pages"), sleep=waits.append, blocked_tries=2)
    assert waits == [120, 240]
    # the query is left unfinished so a later run retries it
    assert db.execute("SELECT status FROM queries").fetchone()[0] == "running"


def test_retry_empty_reruns_zero_result_queries(tmp_path):
    db = store.connect(tmp_path / "h.sqlite")
    db.execute("INSERT INTO queries VALUES (?,?,?,?,?,?,?)", ('"#PBS"', "pbs", 0, 0, 0, "done", 1))
    db.commit()
    assert websearch.search(db, SelectiveBrowser(), flat_plan()) == 0
    assert websearch.search(db, SelectiveBrowser(), flat_plan(), retry_empty=True) == 1
    assert db.execute("SELECT status, results FROM queries").fetchone() == ("done", 5)


def test_breadth_first_runs_every_directive_root_first(tmp_path):
    db = store.connect(tmp_path / "h.sqlite")
    plan = {"base": "", "directives": {"slurm": '"#SBATCH"', "pbs": '"#PBS"'},
            "split": [{"complement": True, "values": ["language:Shell"]}]}
    websearch.search(db, FakeBrowser(), plan, max_queries=2)
    assert sorted(q for (q,) in db.execute("SELECT query FROM queries")) == ['"#PBS"', '"#SBATCH"']
