import pytest

import harvest
import scan
import store


@pytest.mark.network
def test_scan_real_repo_with_size_filtered_blobs():
    """Clones flux-core with a small blob limit so large blobs stay missing."""
    r = scan.scan_repo("flux-framework/flux-core", max_bytes=4096)
    assert r["status"] == "ok" and len(r["commit"]) == 40
    assert r["blobs_skipped"] > 0 and r["blobs_read"] > 0
    paths = {p for *_, paths in r["found"] for p in paths}
    assert "t/batch/directives/valid/001-simple.sh" in paths
    for sha, data, *_ in r["found"]:
        assert store.git_blob_sha(data) == sha


@pytest.mark.network
def test_scan_missing_repo_reports_error():
    r = scan.scan_repo("converged-computing/this-repo-does-not-exist-xyz", max_bytes=4096)
    assert r["status"].startswith("git_error")


def test_repos_to_scan_skips_legacy_unless_included(tmp_path):
    db = store.connect(tmp_path / "h.sqlite")
    store.add_source(db, "a/new", "websearch")
    store.add_source(db, "b/old", "websearch")
    store.add_source(db, "b/old", harvest.LEGACY_SOURCE)
    assert harvest.repos_to_scan(db, False, None, 10) == ["a/new"]
    assert sorted(harvest.repos_to_scan(db, True, None, 10)) == ["a/new", "b/old"]


@pytest.mark.network
def test_import_legacy_listing_and_fetch(tmp_path):
    db = store.connect(tmp_path / "h.sqlite")
    files, fetched = harvest.import_legacy(db, fetch=True, limit=5, max_bytes=harvest.MAX_BYTES)
    assert files > 40000 and fetched == 5
    repos = db.execute("SELECT COUNT(*) FROM repo_sources WHERE source=?",
                       (harvest.LEGACY_SOURCE,)).fetchone()[0]
    assert repos > 11000
    assert db.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0] >= 1
