"""Command line interface."""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__, core, inventory, net, report
from . import feed as feedmod
from .inventory import Dep

FAIL = {"error": 3, "warn": 2, "info": 1, "never": 99}


def _load_events(args):
    _, events = feedmod.load()
    for extra in args.feed or []:
        _, more = feedmod.load(extra)
        events = feedmod.merge(events, more)
    return events


def _ignore_list(args, root):
    ign = list(args.ignore or [])
    path = os.path.join(root, ".exitradar-ignore")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            ign += [ln.split("#", 1)[0].strip() for ln in f if ln.split("#", 1)[0].strip()]
    return tuple(ign)


def _opts(args, root):
    token = "" if args.offline else (net.github_token() if not args.no_gh_token else "")
    return core.Options(offline=args.offline, stale_days=args.stale_days, token=token,
                        ecosystems=tuple(args.only or ()), ignore=_ignore_list(args, root))


def _emit(args, findings, stats, errors):
    fmt = args.format
    if fmt == "json":
        out = report.as_json(findings, stats, errors)
    elif fmt == "markdown":
        out = report.markdown(findings, stats, errors)
    else:
        out = report.text(findings, stats, errors, brief=args.brief)
    print(out)
    if args.markdown_out:
        with open(args.markdown_out, "a" if args.markdown_out == os.environ.get("GITHUB_STEP_SUMMARY") else "w",
                  encoding="utf-8") as f:
            f.write(report.markdown(findings, stats, errors))
    worst = max((core.SEV_ORDER[f.severity] for f in findings), default=0)
    return 1 if worst >= FAIL[args.fail_on] else 0


def cmd_scan(args):
    events = _load_events(args)
    roots = args.paths or ["."]
    deps = []
    for root in roots:
        if not os.path.isdir(root):
            print(f"exitradar: {root}: not a directory", file=sys.stderr)
            return 2
        found = inventory.walk(root, feedmod.text_patterns(events), exclude=tuple(args.exclude or ()))
        if len(roots) > 1:
            for d in found:
                d.file = os.path.basename(os.path.abspath(root)) + "/" + d.file
        deps += found
    errors = []
    if not args.offline and not args.no_gh_token and not net.github_token():
        errors.append("no GitHub token (GITHUB_TOKEN or `gh auth login`): using the unauthenticated API, 60 requests/hour")
    findings, stats = core.evaluate(deps, events, _opts(args, roots[0]), errors)
    return _emit(args, findings, stats, errors)


def cmd_inventory(args):
    events = _load_events(args)
    deps = inventory.walk(args.path, feedmod.text_patterns(events))
    by = {}
    for d in deps:
        by.setdefault(d.eco, []).append(d)
    for eco in sorted(by):
        print(f"{eco} ({len(by[eco])})")
        for d in sorted(by[eco], key=lambda d: (d.name.lower(), d.file, d.line)):
            gh = f"  github.com/{d.github}" if d.github else ""
            print(f"  {d.name}{('@' + d.version) if d.version and d.eco == 'action' else ''}  {d.where()}{gh}")
    return 0


SPEC_ECO = {"gh": "github", "github": "github", "npm": "npm", "pypi": "pypi", "pip": "pypi", "crates": "crates",
            "cargo": "crates", "gem": "gems", "gems": "gems", "go": "go", "image": "image", "docker": "image",
            "action": "action", "helm": "helm", "jsr": "jsr"}


def cmd_check(args):
    events = _load_events(args)
    deps = []
    for spec in args.specs:
        eco, sep, name = spec.partition(":")
        if not sep or eco.lower() not in SPEC_ECO:
            print(f"exitradar: '{spec}': use ECO:NAME, e.g. npm:request, pypi:localstack, gh:minio/minio, "
                  "image:bitnami/redis, action:actions-rs/toolchain", file=sys.stderr)
            return 2
        eco = SPEC_ECO[eco.lower()]
        if eco == "github":
            deps.append(Dep("github", name, "(command line)", 0, github=name))
        elif eco == "action":
            deps.append(Dep("action", name.split("@")[0], "(command line)", 0, github="/".join(name.split("/")[:2])))
        elif eco == "go" and name.startswith("github.com/"):
            p = name.split("/")
            deps.append(Dep("go", name, "(command line)", 0, github=f"{p[1]}/{p[2]}" if len(p) > 2 else ""))
        else:
            deps.append(Dep(eco, name, "(command line)", 0))
    errors = []
    findings, stats = core.evaluate(deps, events, _opts(args, "."), errors)
    for f in findings:
        f.refs = [r for r in f.refs if r != "(command line):0"] or ["(command line)"]
    return _emit(args, findings, stats, errors)


def cmd_feed(args):
    events = _load_events(args)
    if args.json:
        import json
        print(json.dumps({"events": events}, indent=2, ensure_ascii=False))
        return 0
    on = report.use_color()
    for e in sorted(events, key=lambda e: e.get("deadline") or e.get("date") or "", reverse=True):
        when = f"deadline {e['deadline']}" if e.get("deadline") else (e.get("date") or "")
        print(f"{report._c(on, e['severity'], report.LABEL[e['severity']])} {e['id']:<28} {report._c(on, 'dim', when)}")
        print(f"      {e['title']}")
    print(f"\n{len(events)} events. Add your own with --feed FILE_OR_URL (same JSON format).")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="exitradar", description=(
        "Find dependencies whose upstream was archived, deprecated, relicensed, acquired or shut down: "
        "npm, PyPI, Go, Cargo, RubyGems, GitHub Actions, container images, Helm charts, Terraform, runtimes "
        "and hosted services."))
    p.add_argument("--version", action="version", version=f"exitradar {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--format", choices=["text", "json", "markdown"], default="text")
        sp.add_argument("--markdown-out", metavar="FILE", help="also write a Markdown report (appends to $GITHUB_STEP_SUMMARY)")
        sp.add_argument("--fail-on", choices=list(FAIL), default="error", help="exit 1 at this severity or worse (default: error)")
        sp.add_argument("--offline", action="store_true", help="no network: match the bundled feed only")
        sp.add_argument("--stale-days", type=int, default=730, help="flag upstreams with no commits for this long (default 730)")
        sp.add_argument("--feed", action="append", metavar="FILE_OR_URL", help="extra exit-event feed (JSON)")
        sp.add_argument("--ignore", action="append", metavar="SUBJECT", help="ignore e.g. npm:moment or image:redis (also .exitradar-ignore)")
        sp.add_argument("--no-gh-token", action="store_true", help="don't use GITHUB_TOKEN / gh auth token")
        sp.add_argument("--brief", action="store_true", help="one line per finding")

    s = sub.add_parser("scan", help="scan one or more repos")
    s.add_argument("paths", nargs="*")
    s.add_argument("--only", action="append", choices=["action", "npm", "pypi", "go", "crates", "gems", "image", "helm", "terraform", "jsr"],
                   help="limit to an ecosystem (repeatable)")
    s.add_argument("--exclude", action="append", metavar="DIR", help="skip a path prefix (repeatable)")
    common(s)
    s.set_defaults(func=cmd_scan)

    c = sub.add_parser("check", help="check named dependencies, e.g. npm:request gh:minio/minio image:bitnami/redis")
    c.add_argument("specs", nargs="+")
    common(c)
    c.set_defaults(func=cmd_check, only=None)

    i = sub.add_parser("inventory", help="list what a repo depends on (no network)")
    i.add_argument("path", nargs="?", default=".")
    i.add_argument("--feed", action="append")
    i.set_defaults(func=cmd_inventory)

    f = sub.add_parser("feed", help="list the exit events exitradar knows about")
    f.add_argument("--json", action="store_true")
    f.add_argument("--feed", action="append")
    f.set_defaults(func=cmd_feed)
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    try:
        return args.func(args)
    except net.NetError as e:
        print(f"exitradar: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
