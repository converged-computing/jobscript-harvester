"""Repository metadata through the batched GraphQL path, against a fake GitHub."""

import harvest
import store


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.ok = body, status, status == 200

    def json(self):
        if self.body is None:
            raise ValueError("not JSON")
        return self.body


def node(license="MIT", fork=False, branch="main"):
    return {"isFork": fork, "isArchived": False, "stargazerCount": 7, "diskUsage": 120,
            "pushedAt": "2024-05-02T19:38:35Z",
            "defaultBranchRef": {"name": branch} if branch else None,
            "licenseInfo": {"spdxId": license} if license else None}


class FakeGitHub:
    """Answer each query from a table of known repos; fail whole requests that include `poison`."""

    def __init__(self, known, poison=None, forbidden=()):
        self.known, self.poison, self.forbidden, self.sizes = known, poison, forbidden, []

    def post(self, url, payload):
        assert url.endswith("/graphql")
        query = payload["query"]
        repos = [(m[0], f"{m[1]}/{m[2]}") for m in __import__("re").findall(
            r'(r\d+): repository\(owner: "((?:[^"\\]|\\.)*)", name: "((?:[^"\\]|\\.)*)"\)', query)]
        self.sizes.append(len(repos))
        if self.poison and any(repo == self.poison for _, repo in repos):
            return Response(None, status=502)
        data, errors = {}, []
        for alias, repo in repos:
            data[alias] = self.known.get(repo)
            if repo in self.forbidden:
                errors.append({"type": "FORBIDDEN", "path": [alias]})
            elif repo not in self.known:
                errors.append({"type": "NOT_FOUND", "path": [alias]})
        return Response({"data": data, "errors": errors} if errors else {"data": data})


def database(tmp_path, repos):
    db = store.connect(str(tmp_path / "h.sqlite"))
    for repo in repos:
        store.add_source(db, repo, "websearch")
    db.commit()
    return db


def test_graphql_query_escapes_names():
    query = harvest.graphql_query(['a/b', 'we"ird/na\\me'])
    assert 'r0: repository(owner: "a", name: "b")' in query
    assert 'r1: repository(owner: "we\\"ird", name: "na\\\\me")' in query


def test_graphql_records_all_fields_like_rest(tmp_path):
    db = database(tmp_path, ["a/licensed", "a/unlicensed", "a/empty", "a/gone"])
    gh = FakeGitHub({"a/licensed": node(), "a/unlicensed": node(license=None, fork=True),
                     "a/empty": node(branch=None)})
    assert harvest.fetch_repos_graphql(db, gh, 100, batch=100, sleep=lambda s: None) == (4, 0)
    rows = {r[0]: r[1:] for r in db.execute("SELECT * FROM repos")}
    assert rows["a/licensed"] == ("ok", "MIT", 0, 0, 7, "main", 120, "2024-05-02T19:38:35Z")
    assert rows["a/unlicensed"][:3] == ("ok", None, 1)
    assert rows["a/empty"][5] is None
    assert rows["a/gone"] == ("http_404",) + (None,) * 7
    # A second run has nothing left to fetch.
    assert harvest.fetch_repos_graphql(db, gh, 100, sleep=lambda s: None) == (0, 0)


def test_graphql_batches_and_splits_failed_requests(tmp_path):
    names = [f"o/r{i}" for i in range(10)]
    db = database(tmp_path, names)
    gh = FakeGitHub({n: node() for n in names}, poison="o/r3")
    recorded, skipped = harvest.fetch_repos_graphql(db, gh, 100, batch=4, sleep=lambda s: None)
    assert (recorded, skipped) == (9, 1)
    assert max(gh.sizes) == 4
    done = {r for (r,) in db.execute("SELECT repo FROM repos")}
    assert done == set(names) - {"o/r3"}


def test_graphql_leaves_other_errors_unrecorded(tmp_path):
    db = database(tmp_path, ["a/ok", "a/blocked", "notarepo"])
    gh = FakeGitHub({"a/ok": node()}, forbidden=("a/blocked",))
    assert harvest.fetch_repos_graphql(db, gh, 100, sleep=lambda s: None) == (1, 2)
    assert [r for (r,) in db.execute("SELECT repo FROM repos")] == ["a/ok"]


def test_rest_and_graphql_share_rate_limit_handling():
    class Session:
        headers = {}
        calls = []

        def request(self, method, url, timeout, **kwargs):
            self.calls.append((method, url, kwargs))
            return Response({"data": {}})

    gh = harvest.GitHub()
    gh.session = Session()
    gh.get("https://api.github.com/repos/a/b")
    gh.post("https://api.github.com/graphql", {"query": "q"})
    assert [c[0] for c in Session.calls] == ["GET", "POST"]
    assert Session.calls[1][2] == {"json": {"query": "q"}}
