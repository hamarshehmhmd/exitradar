import json
import os

import pytest

from exitradar import cli, report


def test_scan_offline_text_and_exit_code(fake, fixture_repo, capsys):
    rc = cli.main(["scan", str(fixture_repo), "--offline"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "ERROR image:bitnami/redis" in out
    assert "edge/notify.ts:2" in out and "found: acme-orders.web.val.run" in out
    assert "-> MiniStack" in out
    assert out.strip().splitlines()[-1].startswith("") and "error" in out.strip().splitlines()[-1]
    assert fake.calls == []


@pytest.mark.parametrize("fail_on,expect", [("never", 0), ("error", 1), ("info", 1)])
def test_fail_on(fake, fixture_repo, capsys, fail_on, expect):
    assert cli.main(["scan", str(fixture_repo), "--offline", "--fail-on", fail_on]) == expect


def test_fail_on_warn_with_only_warnings(fake, tmp_path, capsys):
    (tmp_path / "package.json").write_text('{"dependencies": {"request": "2"}}')
    assert cli.main(["scan", str(tmp_path), "--offline"]) == 0
    assert cli.main(["scan", str(tmp_path), "--offline", "--fail-on", "warn"]) == 1


def test_clean_repo(fake, tmp_path, capsys):
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "18"}}')
    fake.npm["react"] = {"repository": {"url": "git+https://github.com/facebook/react.git"}}
    fake.repos["facebook/react"] = {}
    assert cli.main(["scan", str(tmp_path)]) == 0
    assert "no upstream exits found" in capsys.readouterr().out


def test_json_output(fake, fixture_repo, capsys):
    cli.main(["scan", str(fixture_repo), "--offline", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert data["tool"] == "exitradar" and data["counts"]["error"] >= 5
    f = next(x for x in data["findings"] if x["subject"] == "pypi:localstack")
    assert f["event"]["id"] == "localstack-account-gated" and f["refs"] == ["api/requirements.txt:3"]


def test_markdown_out_and_step_summary(fake, fixture_repo, tmp_path, capsys, monkeypatch):
    summ = tmp_path / "summary.md"
    summ.write_text("previous\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summ))
    cli.main(["scan", str(fixture_repo), "--offline", "--markdown-out", str(summ)])
    md = summ.read_text(encoding="utf-8")
    assert md.startswith("previous\n## Upstream exit report")
    assert "| 🔴 | `image:bitnami/redis` |" in md
    other = tmp_path / "r.md"
    cli.main(["scan", str(fixture_repo), "--offline", "--markdown-out", str(other)])
    assert other.read_text(encoding="utf-8").startswith("## Upstream exit report")


def test_ignore_file(fake, tmp_path, capsys):
    (tmp_path / "package.json").write_text('{"dependencies": {"request": "2", "moment": "2"}}')
    (tmp_path / ".exitradar-ignore").write_text("# we know\nnpm:request\n")
    cli.main(["scan", str(tmp_path), "--offline", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert [f["subject"] for f in data["findings"]] == ["npm:moment"]


def test_multiple_roots_prefix_paths(fake, tmp_path, capsys):
    for n in ("svc-a", "svc-b"):
        (tmp_path / n).mkdir()
        (tmp_path / n / "package.json").write_text('{"dependencies": {"request": "2"}}')
    cli.main(["scan", str(tmp_path / "svc-a"), str(tmp_path / "svc-b"), "--offline", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert data["findings"][0]["refs"] == ["svc-a/package.json:1", "svc-b/package.json:1"]


def test_check_specs(fake, capsys):
    fake.repos["minio/minio"] = {"archived": True}
    fake.npm["left-pad"] = {"repository": "stevemao/left-pad", "deprecated": "use padStart"}
    fake.repos["stevemao/left-pad"] = {}
    rc = cli.main(["check", "gh:minio/minio", "npm:left-pad", "image:bitnami/kafka:3.7", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    subj = {(f["subject"], f["rule"]) for f in data["findings"]}
    assert ("github:minio/minio", "feed") in subj and ("npm:left-pad", "deprecated") in subj
    assert ("image:bitnami/kafka", "feed") in subj
    assert rc == 1


def test_check_bad_spec(fake, capsys):
    assert cli.main(["check", "left-pad"]) == 2
    assert "ECO:NAME" in capsys.readouterr().err


def test_feed_and_inventory_commands(fake, fixture_repo, capsys):
    assert cli.main(["feed"]) == 0
    out = capsys.readouterr().out
    assert "val-town-winddown" in out and "events." in out
    assert cli.main(["feed", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["events"]) >= 15
    assert cli.main(["inventory", str(fixture_repo)]) == 0
    out = capsys.readouterr().out
    assert "go (2)" in out and "github.com/golang/mock" in out


def test_not_a_directory(fake, capsys):
    assert cli.main(["scan", "/definitely/not/here", "--offline"]) == 2


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 2


def test_text_report_color_toggle(fake, fixture_repo, monkeypatch):
    from exitradar import core, feed, inventory
    ev = feed.load()[1]
    deps = inventory.walk(str(fixture_repo), feed.text_patterns(ev))
    f, stats = core.evaluate(deps, ev, core.Options(offline=True))
    assert "\x1b[" in report.text(f, stats, color=True)
    assert "\x1b[" not in report.text(f, stats, color=False)
    md = report.markdown([], stats)
    assert "No archived" in md
