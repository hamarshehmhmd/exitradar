import json

import pytest

from exitradar import feed
from exitradar.inventory import Dep

EVENTS = feed.load()[1]


def ev(dep, slug=""):
    return [e["id"] for e in feed.match_dep(dep, EVENTS, slug)]


def test_bundled_feed_is_well_formed():
    data, events = feed.load()
    ids = [e["id"] for e in events]
    assert len(ids) == len(set(ids)) >= 15
    for e in events:
        assert e["severity"] in ("error", "warn", "info")
        assert e["sources"] and all(s.startswith("https://") for s in e["sources"])
        assert e["title"] and e["summary"] and e["kind"]
        assert e["match"], e["id"]
        for d in ("date", "deadline"):
            if e.get(d):
                assert len(e[d]) == 10 and e[d][4] == "-"


@pytest.mark.parametrize("ref,repo", [
    ("bitnami/redis:7.2", "bitnami/redis"),
    ("docker.io/bitnami/redis@sha256:abc", "docker.io/bitnami/redis"),
    ("localhost:5000/team/app:1", "localhost:5000/team/app"),
    ("Redis", "redis"),
    ("registry.k8s.io/ingress-nginx/controller:v1.11.3@sha256:x", "registry.k8s.io/ingress-nginx/controller"),
])
def test_image_repo(ref, repo):
    assert feed.image_repo(ref) == repo


def test_image_matching_prefixes_and_exact():
    assert ev(Dep("image", "docker.io/bitnami/postgresql:16", "f", 1)) == ["bitnami-catalog-removal"]
    assert ev(Dep("image", "bitnamilegacy/postgresql:16", "f", 1)) == []
    assert ev(Dep("image", "docker.io/library/redis:7", "f", 1)) == ["redis-relicense"]
    assert ev(Dep("image", "redis/redis-stack:7", "f", 1)) == []
    assert ev(Dep("image", "quay.io/minio/minio:latest", "f", 1)) == ["minio-unmaintained"]
    assert "minio-unmaintained" in ev(Dep("image", "bitnami/minio", "f", 1))
    assert ev(Dep("image", "k8s.gcr.io/ingress-nginx/controller:v1", "f", 1)) == ["ingress-nginx-retired", "k8s-gcr-frozen"]


def test_action_glob_and_subpath():
    assert ev(Dep("action", "actions-rs/toolchain", "f", 1, github="actions-rs/toolchain")) == ["actions-rs-archived"]
    assert ev(Dep("action", "actions/upload-release-asset", "f", 1)) == ["actions-release-archived"]
    assert ev(Dep("action", "actions/checkout", "f", 1)) == []


def test_package_names_pypi_normalised():
    assert ev(Dep("pypi", "LocalStack_Core", "f", 1)) == ["localstack-account-gated"]
    assert ev(Dep("npm", "request-promise", "f", 1)) == ["request-deprecated"]
    assert ev(Dep("npm", "requests", "f", 1)) == []


def test_matches_through_resolved_github_slug():
    assert ev(Dep("go", "github.com/minio/minio", "f", 1), "minio/minio") == ["minio-unmaintained"]
    assert ev(Dep("npm", "whatever", "f", 1), "Denoland/Deployctl") == ["deno-deploy-shutdown"]


def test_helm_substring():
    assert ev(Dep("helm", "https://charts.bitnami.com/bitnami/redis", "f", 1)) == ["bitnami-catalog-removal"]


def test_custom_feed_file_merges_and_overrides(tmp_path):
    p = tmp_path / "mine.json"
    p.write_text(json.dumps({"events": [
        {"id": "request-deprecated", "title": "overridden", "kind": "archived", "severity": "error",
         "sources": ["https://x"], "summary": "s", "match": {"npm": ["request"]}},
        {"id": "internal-sunset", "title": "Our old SDK", "kind": "shutdown", "sources": ["https://wiki"],
         "summary": "s", "match": {"npm": ["@acme/old-sdk"]}},
    ]}))
    _, more = feed.load(str(p))
    merged = feed.merge(EVENTS, more)
    by = {e["id"]: e for e in merged}
    assert by["request-deprecated"]["title"] == "overridden"
    assert by["internal-sunset"]["severity"] == "warn"   # default
    assert len(merged) == len(EVENTS) + 1
