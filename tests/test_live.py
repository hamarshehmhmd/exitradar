"""Real network checks. Run with EXITRADAR_LIVE=1 (uses GITHUB_TOKEN or `gh auth token` if available)."""
import json
import os

import pytest

from exitradar import cli

pytestmark = pytest.mark.skipif(not os.environ.get("EXITRADAR_LIVE"), reason="set EXITRADAR_LIVE=1")


def test_live_check(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("EXITRADAR_CACHE", str(tmp_path))
    cli.main(["check", "go:github.com/golang/mock", "npm:request", "npm:react", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    subj = {(f["subject"], f["rule"]) for f in data["findings"]}
    assert ("go:github.com/golang/mock", "archived") in subj
    assert ("npm:request", "feed") in subj
    assert not any(s == "npm:react" for s, _ in subj)
