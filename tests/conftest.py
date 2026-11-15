import os
import shutil

import pytest

from exitradar import net

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(os.path.dirname(HERE), "examples", "acme-platform")


class FakeNet:
    """Answers registry and GitHub calls from dicts; records every URL."""

    def __init__(self):
        self.npm, self.pypi, self.crates, self.gems = {}, {}, {}, {}
        self.repos = {}          # slug -> dict(archived, disabled, pushedAt, nameWithOwner, license, archivedAt)
        self.calls = []
        self.gql_error = None

    def __call__(self, url, data=None, headers=None, use_cache=True, timeout=20):
        self.calls.append(url)
        if url.startswith("https://registry.npmjs.org/"):
            name = url.split("/")[3].replace("%2F", "/")
            return self.npm.get(name, {"__status__": 404})
        if url.startswith("https://pypi.org/pypi/"):
            return self.pypi.get(url.split("/")[4], {"__status__": 404})
        if url.startswith("https://crates.io/"):
            return self.crates.get(url.rsplit("/", 1)[1], {"__status__": 404})
        if url.startswith("https://rubygems.org/"):
            return self.gems.get(url.rsplit("/", 1)[1][:-5], {"__status__": 404})
        if url == "https://api.github.com/graphql":
            if self.gql_error:
                return {"errors": [{"message": self.gql_error}]}
            import re
            out, errors = {}, []
            for alias, owner, name in re.findall(r'(r\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)', data["query"]):
                r = self.repos.get(f"{owner}/{name}")
                if r is None:
                    out[alias] = None
                    errors.append({"type": "NOT_FOUND", "path": [alias], "message": "nope"})
                else:
                    out[alias] = {"nameWithOwner": r.get("nameWithOwner", f"{owner}/{name}"),
                                  "isArchived": r.get("archived", False), "isDisabled": r.get("disabled", False),
                                  "pushedAt": r.get("pushedAt", "2026-11-01T00:00:00Z"), "archivedAt": r.get("archivedAt"),
                                  "url": "", "licenseInfo": {"spdxId": r.get("license", "MIT")}}
            res = {"data": out}
            if errors:
                res["errors"] = errors
            return res
        if url.startswith("https://api.github.com/repos/"):
            slug = url[len("https://api.github.com/repos/"):]
            r = self.repos.get(slug)
            if r is None:
                return {"__status__": 404}
            return {"full_name": r.get("nameWithOwner", slug), "archived": r.get("archived", False),
                    "disabled": r.get("disabled", False), "pushed_at": r.get("pushedAt", "2026-11-01T00:00:00Z"),
                    "license": {"spdx_id": r.get("license", "MIT")}}
        raise net.NetError(f"unexpected url {url}")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = FakeNet()
    monkeypatch.setattr(net, "fetch_json", f)
    monkeypatch.setattr(net, "github_token", lambda: "test-token")
    monkeypatch.setenv("EXITRADAR_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("NO_COLOR", "1")
    return f


@pytest.fixture
def fixture_repo(tmp_path):
    dst = tmp_path / "acme-platform"
    shutil.copytree(FIXTURE, dst)
    return dst
