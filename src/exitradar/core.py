"""Resolve dependencies to upstream repos, check them, and turn the results into findings."""
from __future__ import annotations

import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import feed as feedmod
from . import net
from .inventory import Dep

SEV_ORDER = {"error": 3, "warn": 2, "info": 1}
NON_OSS_LICENSES = {"BUSL-1.1": "Business Source License", "SSPL-1.0": "Server Side Public License",
                    "Elastic-2.0": "Elastic License 2.0", "BSL-1.1": "Business Source License",
                    "CC-BY-NC-4.0": "non-commercial licence", "PolyForm-Noncommercial-1.0.0": "PolyForm Noncommercial",
                    "FSL-1.1-MIT": "Functional Source License", "FSL-1.1-Apache-2.0": "Functional Source License"}


@dataclass
class Finding:
    rule: str          # feed, archived, disabled, deprecated, inactive, moved, license, stale, missing
    severity: str
    subject: str       # e.g. "npm:request", "action:actions-rs/toolchain"
    message: str
    refs: list = field(default_factory=list)       # Dep.where() strings
    upstream: str = ""                             # owner/repo
    event: dict = None
    detail: dict = field(default_factory=dict)

    def to_dict(self):
        d = {"rule": self.rule, "severity": self.severity, "subject": self.subject, "message": self.message,
             "refs": self.refs, "upstream": self.upstream}
        if self.event:
            e = self.event
            d["event"] = {k: e.get(k) for k in ("id", "title", "kind", "date", "deadline", "sources", "replacements")}
        if self.detail:
            d["detail"] = self.detail
        return d


def subject(dep: Dep):
    if dep.eco == "image":
        return "image:" + feedmod.image_repo(dep.name)
    if dep.eco == "text":
        return "code:" + dep.name
    return f"{dep.eco}:{dep.name}"


# ---------------------------------------------------------------- registry lookups
def _npm(name):
    d = net.fetch_json(f"https://registry.npmjs.org/{name.replace('/', '%2F')}/latest")
    if d.get("__status__") == 404:
        return {"missing": True}
    repo = d.get("repository")
    url = repo.get("url", "") if isinstance(repo, dict) else (repo or "")
    if isinstance(repo, str) and re.match(r"^[\w.-]+/[\w.-]+$", repo):
        url = "github.com/" + repo
    return {"github": _gh_from_url(url) or _gh_from_url(d.get("homepage", "") or ""),
            "deprecated": d.get("deprecated") or "", "version": d.get("version", "")}


def _pypi(name):
    d = net.fetch_json(f"https://pypi.org/pypi/{name}/json")
    if d.get("__status__") == 404:
        return {"missing": True}
    info = d.get("info") or {}
    urls = list((info.get("project_urls") or {}).items())
    gh = ""
    pref = ("source", "repository", "code", "github", "homepage")
    urls.sort(key=lambda kv: next((i for i, p in enumerate(pref) if p in kv[0].lower()), 99))
    for _, u in urls + [("home", info.get("home_page") or "")]:
        gh = _gh_from_url(u or "")
        if gh:
            break
    inactive = any(c.startswith("Development Status :: 7") for c in info.get("classifiers") or [])
    return {"github": gh, "inactive": inactive, "version": info.get("version", "")}


def _crates(name):
    d = net.fetch_json(f"https://crates.io/api/v1/crates/{name}")
    if d.get("__status__") == 404:
        return {"missing": True}
    c = d.get("crate") or {}
    return {"github": _gh_from_url(c.get("repository") or "") or _gh_from_url(c.get("homepage") or ""),
            "version": c.get("max_stable_version") or c.get("max_version") or ""}


def _gems(name):
    d = net.fetch_json(f"https://rubygems.org/api/v1/gems/{name}.json")
    if d.get("__status__") == 404:
        return {"missing": True}
    gh = ""
    for k in ("source_code_uri", "homepage_uri", "bug_tracker_uri"):
        gh = _gh_from_url(d.get(k) or "")
        if gh:
            break
    return {"github": gh, "version": d.get("version", "")}


REGISTRIES = {"npm": _npm, "pypi": _pypi, "crates": _crates, "gems": _gems}


def _gh_from_url(url):
    m = re.search(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", url or "")
    if not m:
        return ""
    repo = re.sub(r"\.git$", "", m.group(2))
    if m.group(1).lower() in ("sponsors", "orgs", "features", "marketplace"):
        return ""
    return f"{m.group(1)}/{repo}"


# ---------------------------------------------------------------- GitHub
GQL_FIELDS = "nameWithOwner isArchived isDisabled pushedAt archivedAt url licenseInfo { spdxId name }"


def github_repos(slugs, token):
    """slug -> {archived, disabled, pushed_at, name_with_owner, license, missing}."""
    slugs = sorted(set(s for s in slugs if s))
    res = {}
    if not slugs:
        return res
    if token:
        for i in range(0, len(slugs), 50):
            chunk = slugs[i:i + 50]
            parts = []
            for j, s in enumerate(chunk):
                o, n = s.split("/", 1)
                parts.append(f'r{j}: repository(owner: {_gq(o)}, name: {_gq(n)}) {{ {GQL_FIELDS} }}')
            q = "query { " + " ".join(parts) + " }"
            d = net.fetch_json("https://api.github.com/graphql", data={"query": q},
                               headers={"Authorization": f"bearer {token}"})
            if not isinstance(d.get("data"), dict):
                msg = "; ".join(e.get("message", "") for e in d.get("errors") or []) or d.get("message", "no data")
                raise net.NetError(f"GitHub GraphQL: {msg}")
            data = d["data"]
            notfound = {(e.get("path") or [""])[0] for e in d.get("errors") or [] if e.get("type") == "NOT_FOUND"}
            for j, s in enumerate(chunk):
                r = data.get(f"r{j}")
                if r:
                    res[s] = _from_gql(r)
                elif f"r{j}" in notfound:
                    res[s] = {"missing": True}
    else:
        def one(s):
            d = net.fetch_json(f"https://api.github.com/repos/{s}")
            if d.get("__status__") == 404:
                return s, {"missing": True}
            return s, {"archived": d.get("archived", False), "disabled": d.get("disabled", False),
                       "pushed_at": d.get("pushed_at"), "name_with_owner": d.get("full_name", s),
                       "license": ((d.get("license") or {}).get("spdx_id") or "")}
        with ThreadPoolExecutor(8) as ex:
            for s, r in ex.map(one, slugs):
                res[s] = r
    return res


def _gq(s):
    return '"' + s.replace("\\", "").replace('"', "") + '"'


def _from_gql(r):
    return {"archived": r.get("isArchived", False), "disabled": r.get("isDisabled", False),
            "pushed_at": r.get("pushedAt"), "archived_at": r.get("archivedAt"),
            "name_with_owner": r.get("nameWithOwner"),
            "license": ((r.get("licenseInfo") or {}).get("spdxId") or "")}


# ---------------------------------------------------------------- scan
@dataclass
class Options:
    offline: bool = False
    stale_days: int = 730
    token: str = ""
    ecosystems: tuple = ()
    ignore: tuple = ()
    today: dt.date = None
    max_lookups: int = 2000


def _ignored(subj, opts):
    s = subj.lower()
    return any(s == i.lower() or s.split(":", 1)[-1] == i.lower() or (i.endswith("*") and s.startswith(i[:-1].lower()))
               for i in opts.ignore)


def evaluate(deps, events, opts: Options, errors=None):
    errors = errors if errors is not None else []
    today = opts.today or dt.date.today()
    if opts.ecosystems:
        deps = [d for d in deps if d.eco in opts.ecosystems or d.eco in ("text", "runtime")]
    groups = {}
    for d in deps:
        groups.setdefault((subject(d)), []).append(d)
    groups = {k: v for k, v in groups.items() if not _ignored(k, opts)}

    # 1. registry metadata
    reg = {}
    if not opts.offline:
        todo = [(k, v[0]) for k, v in groups.items() if v[0].eco in REGISTRIES and not v[0].github]
        todo = todo[: opts.max_lookups]

        def look(item):
            k, d = item
            try:
                return k, REGISTRIES[d.eco](d.name), None
            except net.NetError as e:
                return k, {}, str(e)
        with ThreadPoolExecutor(12) as ex:
            for k, info, err in ex.map(look, todo):
                reg[k] = info
                if err:
                    errors.append(err)

    # 2. upstream repos
    slug_of = {}
    for k, v in groups.items():
        d = v[0]
        slug = d.github or (reg.get(k) or {}).get("github", "")
        if not slug and d.eco == "terraform" and re.match(r"^[\w-]+/[\w-]+$", d.name) and "github.com" not in d.name:
            ns, typ = d.name.split("/")
            slug = f"{ns}/terraform-provider-{typ}" if ns in ("hashicorp", "integrations") else ""
        slug_of[k] = slug
    gh = {}
    if not opts.offline:
        try:
            gh = github_repos(slug_of.values(), opts.token)
        except net.NetError as e:
            errors.append(str(e))

    findings = []
    for k, v in sorted(groups.items()):
        d = v[0]
        refs = [x.where() for x in v]
        slug = slug_of.get(k, "")
        info = reg.get(k) or {}
        repo = gh.get(slug) or {}
        upstream = repo.get("name_with_owner") or slug
        evs = feedmod.match_dep(d, events, slug) + (feedmod.match_dep(d, events, upstream) if upstream != slug else [])
        seen = set()
        for e in evs:
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            sev = e.get("severity", "warn")
            dl = e.get("deadline")
            if dl and sev == "warn" and _date(dl) and _date(dl) <= today:
                sev = "error"
            det = {}
            if d.eco == "text":
                det["matched"] = ", ".join(sorted({x.extra.get("match", "") for x in v}))
            findings.append(Finding("feed", sev, k, e["title"], refs, upstream, e, det))
        covered = bool(seen)
        if info.get("missing"):
            findings.append(Finding("missing", "warn", k, f"not found on the public {d.eco} registry: removed, renamed, or a private package (ignore it in .exitradar-ignore)", refs, upstream))
        if info.get("deprecated") and covered:
            for f in findings:
                if f.subject == k and f.rule == "feed":
                    f.detail["npm_deprecated"] = info["deprecated"]
        elif info.get("deprecated"):
            findings.append(Finding("deprecated", "warn", k, f"deprecated on npm: {info['deprecated']}", refs, upstream))
        if info.get("inactive"):
            findings.append(Finding("inactive", "warn", k, "marked 'Development Status :: 7 - Inactive' on PyPI", refs, upstream))
        if repo.get("missing") and slug:
            findings.append(Finding("missing", "error" if d.eco == "action" else "warn", k,
                                    f"upstream repo github.com/{slug} is gone (deleted, private or renamed without redirect)", refs, slug))
        if repo and not repo.get("missing"):
            if repo.get("disabled"):
                findings.append(Finding("disabled", "error", k, f"github.com/{upstream} is disabled by GitHub", refs, upstream))
            if repo.get("archived") and not covered:
                when = (repo.get("archived_at") or "")[:10]
                findings.append(Finding("archived", "error" if d.eco == "action" else "warn", k,
                                        f"github.com/{upstream} is archived{' since ' + when if when else ''}: read-only, no fixes or security patches",
                                        refs, upstream, detail={"archived_at": when}))
            elif repo.get("archived") and covered:
                for f in findings:
                    if f.subject == k and f.rule == "feed":
                        f.detail["archived"] = True
            nwo = repo.get("name_with_owner") or ""
            if slug and nwo and nwo.lower() != slug.lower():
                findings.append(Finding("moved", "info", k, f"github.com/{slug} now redirects to github.com/{nwo} (renamed or transferred)", refs, nwo))
            lic = repo.get("license") or ""
            if lic in NON_OSS_LICENSES and not covered:
                findings.append(Finding("license", "info", k, f"upstream is under {lic} ({NON_OSS_LICENSES[lic]}), not an open source licence", refs, upstream))
            pushed = _date((repo.get("pushed_at") or "")[:10])
            if pushed and not repo.get("archived") and (today - pushed).days > opts.stale_days and not covered:
                findings.append(Finding("stale", "info", k, f"no commits to github.com/{upstream} since {pushed.isoformat()} ({(today - pushed).days // 30} months)", refs, upstream))
    findings.sort(key=lambda f: (-SEV_ORDER[f.severity], f.subject))
    stats = {"dependencies": len(groups), "references": sum(len(v) for v in groups.values()),
             "upstream_repos": len({s for s in slug_of.values() if s}), "registry_lookups": len(reg),
             "ecosystems": sorted({v[0].eco for v in groups.values()})}
    return findings, stats


def _date(s):
    try:
        return dt.date.fromisoformat(s[:10])
    except (TypeError, ValueError):
        return None
