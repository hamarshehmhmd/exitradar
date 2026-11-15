import datetime as dt

from exitradar import core, feed, inventory, net
from exitradar.inventory import Dep

EVENTS = feed.load()[1]
TODAY = dt.date(2026, 11, 15)


def run(deps, **kw):
    errors = []
    opts = core.Options(token=kw.pop("token", "t"), today=kw.pop("today", TODAY), **kw)
    f, stats = core.evaluate(deps, EVENTS, opts, errors)
    return f, stats, errors


def by(findings):
    return {(f.subject, f.rule): f for f in findings}


def test_archived_repo_not_in_feed_is_found_via_registry(fake):
    fake.gems["htmlentities"] = {"source_code_uri": "https://github.com/threedaymonk/htmlentities", "version": "4.3.4"}
    fake.repos["threedaymonk/htmlentities"] = {"archived": True, "archivedAt": "2026-10-18T17:13:35Z"}
    f, stats, errors = run([Dep("gems", "htmlentities", "Gemfile", 3)])
    x = by(f)[("gems:htmlentities", "archived")]
    assert x.severity == "warn" and "since 2026-10-18" in x.message and x.refs == ["Gemfile:3"]
    assert stats["upstream_repos"] == 1 and not errors


def test_archived_action_is_an_error(fake):
    fake.repos["someone/old-action"] = {"archived": True}
    f, _, _ = run([Dep("action", "someone/old-action", "ci.yml", 4, "v1", github="someone/old-action")])
    assert by(f)[("action:someone/old-action", "archived")].severity == "error"


def test_feed_hit_suppresses_duplicate_archived_and_deprecated(fake):
    fake.npm["tslint"] = {"repository": {"url": "git+https://github.com/palantir/tslint.git"},
                          "deprecated": "TSLint has been deprecated in favor of ESLint."}
    fake.repos["palantir/tslint"] = {"archived": True}
    f, _, _ = run([Dep("npm", "tslint", "package.json", 9)])
    rules = [x.rule for x in f]
    assert rules == ["feed"]
    assert f[0].detail["npm_deprecated"].startswith("TSLint") and f[0].detail["archived"]


def test_npm_deprecated_and_pypi_inactive(fake):
    fake.npm["left-pad"] = {"repository": "stevemao/left-pad", "deprecated": "use String.prototype.padStart()"}
    fake.repos["stevemao/left-pad"] = {}
    fake.pypi["oldlib"] = {"info": {"classifiers": ["Development Status :: 7 - Inactive"],
                                    "project_urls": {"Source": "https://github.com/a/oldlib"}}}
    fake.repos["a/oldlib"] = {}
    f, _, _ = run([Dep("npm", "left-pad", "p", 1), Dep("pypi", "oldlib", "r", 1)])
    b = by(f)
    assert "padStart" in b[("npm:left-pad", "deprecated")].message
    assert b[("pypi:oldlib", "inactive")].severity == "warn"


def test_pypi_prefers_source_url_over_docs(fake):
    fake.pypi["x"] = {"info": {"project_urls": {"Documentation": "https://github.com/docs/site",
                                                "Source Code": "https://github.com/real/x"}}}
    fake.repos["real/x"] = {"archived": True}
    f, _, _ = run([Dep("pypi", "x", "r", 1)])
    assert by(f)[("pypi:x", "archived")].upstream == "real/x"


def test_missing_package_and_missing_repo(fake):
    fake.npm["gone-repo"] = {"repository": {"url": "https://github.com/ghost/gone"}}
    f, _, _ = run([Dep("npm", "vanished-pkg", "p", 1), Dep("npm", "gone-repo", "p", 2),
                   Dep("action", "ghost/act", "ci", 1, github="ghost/act")])
    b = by(f)
    assert b[("npm:vanished-pkg", "missing")].severity == "warn"
    assert b[("npm:gone-repo", "missing")].severity == "warn"
    assert b[("action:ghost/act", "missing")].severity == "error"


def test_moved_license_stale_disabled(fake):
    fake.repos["old/name"] = {"nameWithOwner": "new/name"}
    fake.repos["hc/thing"] = {"license": "BUSL-1.1"}
    fake.repos["sleepy/lib"] = {"pushedAt": "2022-01-01T00:00:00Z"}
    fake.repos["bad/repo"] = {"disabled": True}
    deps = [Dep("go", "github.com/old/name", "go.mod", 3, github="old/name"),
            Dep("go", "github.com/hc/thing", "go.mod", 4, github="hc/thing"),
            Dep("go", "github.com/sleepy/lib", "go.mod", 5, github="sleepy/lib"),
            Dep("go", "github.com/bad/repo", "go.mod", 6, github="bad/repo")]
    b = by(run(deps)[0])
    assert "new/name" in b[("go:github.com/old/name", "moved")].message
    assert "BUSL-1.1" in b[("go:github.com/hc/thing", "license")].message
    assert "2022-01-01" in b[("go:github.com/sleepy/lib", "stale")].message
    assert b[("go:github.com/bad/repo", "disabled")].severity == "error"
    b2 = by(run(deps, stale_days=5000)[0])
    assert ("go:github.com/sleepy/lib", "stale") not in b2


def test_feed_matches_after_rename(fake):
    fake.repos["olddeno/deployctl"] = {"nameWithOwner": "denoland/deployctl", "archived": True}
    f, _, _ = run([Dep("go", "github.com/olddeno/deployctl", "go.mod", 1, github="olddeno/deployctl")])
    assert ("go:github.com/olddeno/deployctl", "feed") in by(f)


def test_warn_becomes_error_after_deadline(fake):
    d = [Dep("runtime", "deno", "deno.json", 1)]
    assert run(d, offline=True)[0][0].severity == "warn"
    assert run(d, offline=True, today=dt.date(2027, 10, 10))[0][0].severity == "error"


def test_offline_makes_no_requests(fake, fixture_repo):
    deps = inventory.walk(str(fixture_repo), feed.text_patterns(EVENTS))
    f, stats, _ = run(deps, offline=True)
    assert fake.calls == []
    subjects = {x.subject for x in f}
    assert {"image:bitnami/redis", "npm:request", "code:val-town-winddown", "code:deno-deploy-shutdown",
            "image:minio/minio", "action:actions-rs/toolchain", "pypi:localstack"} <= subjects
    assert "npm:react" not in subjects


def test_graphql_failure_is_reported_not_fatal(fake):
    fake.gql_error = "API rate limit exceeded"
    fake.npm["request"] = {"repository": {"url": "https://github.com/request/request"}}
    f, _, errors = run([Dep("action", "a/b", "ci", 1, github="a/b"), Dep("npm", "request", "p", 1)])
    assert any("rate limit" in e for e in errors)
    assert [x.subject for x in f] == ["npm:request"]   # feed still works


def test_rest_fallback_without_token(fake):
    fake.repos["a/b"] = {"archived": True}
    f, _, _ = run([Dep("action", "a/b", "ci", 1, github="a/b")], token="")
    assert any(u == "https://api.github.com/repos/a/b" for u in fake.calls)
    assert by(f)[("action:a/b", "archived")]


def test_registry_errors_are_collected(fake, monkeypatch):
    def boom(url, **kw):
        raise net.NetError("offline")
    monkeypatch.setattr(net, "fetch_json", boom)
    f, _, errors = run([Dep("npm", "request", "p", 1)])
    assert errors and [x.rule for x in f] == ["feed"]


def test_ignore_and_ecosystem_filter(fake):
    deps = [Dep("npm", "request", "p", 1), Dep("npm", "moment", "p", 2), Dep("image", "bitnami/redis:7", "c", 1)]
    f, _, _ = run(deps, offline=True, ignore=("npm:moment", "image:bitnami/*"))
    assert [x.subject for x in f] == ["npm:request"]
    f, _, _ = run(deps, offline=True, ecosystems=("image",))
    assert [x.subject for x in f] == ["image:bitnami/redis"]


def test_references_grouped_per_dependency(fake):
    deps = [Dep("image", "bitnami/redis:7", "a.yml", 1), Dep("image", "docker.io/bitnami/redis:6", "b.yml", 9)]
    f, stats, _ = run(deps, offline=True)
    assert len(f) == 2
    # different image refs normalise separately (docker.io prefix kept) but both match
    assert all(x.event["id"] == "bitnami-catalog-removal" for x in f)
    deps = [Dep("npm", "request", "a/package.json", 3), Dep("npm", "request", "b/package.json", 4)]
    f, stats, _ = run(deps, offline=True)
    assert len(f) == 1 and f[0].refs == ["a/package.json:3", "b/package.json:4"]
    assert stats["references"] == 2 and stats["dependencies"] == 1
