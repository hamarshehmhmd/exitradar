"""The curated exit-event feed and matching rules."""
from __future__ import annotations

import fnmatch
import json
import os
import re
from importlib import resources

from . import net


def load(path_or_url=None):
    if path_or_url and re.match(r"^https?://", path_or_url):
        data = net.fetch_json(path_or_url)
    elif path_or_url:
        with open(path_or_url, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.loads(resources.files("exitradar").joinpath("feed.json").read_text(encoding="utf-8"))
    events = data.get("events", [])
    for e in events:
        e.setdefault("match", {})
        e.setdefault("severity", "warn")
    return data, events


def merge(*feeds):
    by_id = {}
    for events in feeds:
        for e in events:
            by_id[e["id"]] = e
    return list(by_id.values())


def text_patterns(events):
    out = []
    for e in events:
        for rx in e["match"].get("text", []):
            out.append((e["id"], re.compile(rx, re.I)))
    return out


def image_repo(ref):
    """'docker.io/bitnami/redis:7.2@sha256:..' -> 'docker.io/bitnami/redis'."""
    ref = ref.split("@", 1)[0]
    last = ref.rsplit("/", 1)
    if ":" in last[-1]:
        ref = ref[: len(ref) - len(last[-1]) + last[-1].index(":")]
    return ref.lower()


def _image_variants(repo):
    v = {repo}
    for pre in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        if repo.startswith(pre):
            v.add(repo[len(pre):])
    for x in list(v):
        if x.startswith("library/"):
            v.add(x[len("library/"):])
    return v


def _image_match(pattern, repo):
    pattern = pattern.lower()
    for r in _image_variants(repo):
        if pattern.endswith("/"):
            if r.startswith(pattern):
                return True
        elif r == pattern:
            return True
    return False


def match_dep(dep, events, github_slug=""):
    """Return the events that apply to one Dep (github_slug: resolved repo, if any)."""
    hits = []
    slug = (github_slug or dep.github or "").lower()
    for e in events:
        m = e["match"]
        ok = False
        if dep.eco == "text":
            ok = dep.extra.get("event") == e["id"]
        elif dep.eco == "action":
            name = dep.name.lower()
            ok = any(fnmatch.fnmatch(name, p.lower()) or fnmatch.fnmatch("/".join(name.split("/")[:2]), p.lower())
                     for p in m.get("actions", []))
        elif dep.eco == "image":
            ok = any(_image_match(p, image_repo(dep.name)) for p in m.get("images", []))
        elif dep.eco == "helm":
            ok = any(p.lower() in dep.name.lower() for p in m.get("helm", []))
        elif dep.eco in ("npm", "pypi", "jsr", "gems", "crates", "go"):
            names = [n.lower() for n in m.get(dep.eco, [])]
            nm = dep.name.lower()
            if dep.eco == "pypi":
                nm = re.sub(r"[-_.]+", "-", nm)
                names = [re.sub(r"[-_.]+", "-", n) for n in names]
            ok = nm in names
        elif dep.eco == "runtime":
            ok = dep.name.lower() in [n.lower() for n in m.get("runtime", [])]
        if not ok and slug:
            ok = slug in [g.lower() for g in m.get("github", [])]
        if ok:
            hits.append(e)
    return hits
