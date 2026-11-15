"""Walk a repository and list what it depends on, with file:line for every reference."""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "vendor", "__pycache__", ".tox", "dist", "build",
             "target", ".terraform", ".next", ".cache", "site-packages", ".mypy_cache", ".pytest_cache"}
TEXT_EXT = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".sh", ".yml", ".yaml", ".json", ".toml",
            ".md", ".env", ".tf", ".rb", ".go", ".rs", ".html", ".txt", ".cfg", ".ini", ""}
MAX_TEXT = 512 * 1024


@dataclass
class Dep:
    eco: str          # action, npm, pypi, go, crates, gems, image, helm, terraform, runtime, text, jsr
    name: str
    file: str
    line: int
    version: str = ""
    github: str = ""  # owner/repo when known without a registry lookup
    extra: dict = field(default_factory=dict)

    @property
    def key(self):
        return (self.eco, self.name.lower())

    def where(self):
        return f"{self.file}:{self.line}"


def _lineno(text, pos):
    return text.count("\n", 0, pos) + 1


def _find_line(text, needle, default=1):
    i = text.find(needle)
    return _lineno(text, i) if i >= 0 else default


GH_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _norm_gh(s):
    s = s.strip().rstrip("/")
    s = re.sub(r"\.git$", "", s)
    m = re.search(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", s)
    if m:
        return f"{m.group(1)}/{re.sub(r'.git$', '', m.group(2))}"
    return ""


# ---------------------------------------------------------------- parsers
USES = re.compile(r"""^\s*(?:-\s*)?uses:\s*["']?([^\s"'#]+)""", re.M)
IMAGE = re.compile(r"""^\s*(?:-\s*)?image:\s*["']?([^\s"'#{}]+)""", re.M)
FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.M | re.I)
HELM_URL = re.compile(r"""(oci://[^\s"']+|https?://[A-Za-z0-9.-]+(?:/[^\s"']*)?)""")


def parse_workflow(rel, text, out):
    for m in USES.finditer(text):
        ref = m.group(1)
        line = _lineno(text, m.start(1))
        if ref.startswith("./") or ref.startswith("."):
            continue
        if ref.startswith("docker://"):
            out.append(Dep("image", ref[len("docker://"):], rel, line))
            continue
        name, _, ver = ref.partition("@")
        parts = name.split("/")
        if len(parts) >= 2:
            slug = "/".join(parts[:2])
            out.append(Dep("action", name, rel, line, ver, github=slug))
    parse_images_yaml(rel, text, out)
    parse_helm_text(rel, text, out)


def parse_images_yaml(rel, text, out):
    for m in IMAGE.finditer(text):
        img = m.group(1)
        if "$" in img or not re.match(r"^[a-z0-9]", img):
            continue
        out.append(Dep("image", img, rel, _lineno(text, m.start(1))))


def parse_dockerfile(rel, text, out):
    stages = set()
    for m in FROM.finditer(text):
        img = m.group(1)
        line = _lineno(text, m.start(1))
        rest = text[m.end():text.find("\n", m.end()) if text.find("\n", m.end()) >= 0 else len(text)]
        am = re.search(r"\bAS\s+(\S+)", rest, re.I)
        if img.lower() not in stages and "$" not in img and img.lower() != "scratch":
            out.append(Dep("image", img, rel, line))
        if am:
            stages.add(am.group(1).lower())
        if img.split(":")[0].split("/")[-1] == "deno":
            out.append(Dep("runtime", "deno", rel, line))


def parse_helm_text(rel, text, out):
    for m in re.finditer(r"(?:helm\s+repo\s+add\s+\S+\s+|repository:\s*[\"']?|helm\s+(?:install|upgrade|pull|template)\b[^\n]*?)(oci://[^\s\"']+|https?://[^\s\"']+)", text):
        out.append(Dep("helm", m.group(1).rstrip("/"), rel, _lineno(text, m.start(1))))


def parse_chart(rel, text, out):
    """Chart.yaml `dependencies:` entries (name + repository)."""
    m = re.search(r"^dependencies:\s*$", text, re.M)
    if not m:
        return
    end = re.search(r"^\S", text[m.end():], re.M)
    section = text[m.end(): m.end() + end.start()] if end else text[m.end():]
    base = m.end()
    for item in re.finditer(r"^\s*-\s+(.*?)(?=^\s*-\s+|\Z)", section, re.M | re.S):
        body = item.group(0)
        nm = re.search(r"name:\s*[\"']?([\w.-]+)", body)
        rm = re.search(r"repository:\s*[\"']?([^\s\"']+)", body)
        if nm and rm and re.match(r"^(https?|oci)://", rm.group(1)):
            pos = base + item.start() + body.index(rm.group(1))
            out.append(Dep("helm", rm.group(1).rstrip("/") + "/" + nm.group(1), rel, _lineno(text, pos)))


def _pkgjson_line(text, name):
    return _find_line(text, f'"{name}"')


def parse_package_json(rel, text, out):
    try:
        data = json.loads(text)
    except ValueError:
        return
    for sect in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, ver in (data.get(sect) or {}).items():
            if not isinstance(ver, str):
                continue
            gh = ""
            if ver.startswith("github:"):
                gh = ver[7:].split("#")[0]
            elif GH_SLUG.match(ver.split("#")[0]) and not ver.startswith(("npm:", "file:", "link:", "workspace:")):
                gh = ver.split("#")[0]
            elif "github.com" in ver:
                gh = _norm_gh(ver)
            out.append(Dep("npm", name, rel, _pkgjson_line(text, name), ver, github=gh))
    engines = data.get("engines") or {}
    if "deno" in engines:
        out.append(Dep("runtime", "deno", rel, _pkgjson_line(text, "deno")))


def parse_deno_json(rel, text, out):
    out.append(Dep("runtime", "deno", rel, 1))
    try:
        data = json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M))
    except ValueError:
        return
    for alias, spec in (data.get("imports") or {}).items():
        if not isinstance(spec, str):
            continue
        line = _find_line(text, spec)
        if spec.startswith("npm:"):
            name = re.match(r"npm:(@?[^@]+)", spec).group(1)
            out.append(Dep("npm", name, rel, line))
        elif spec.startswith("jsr:"):
            name = re.match(r"jsr:/?(@[^/@]+/[^/@]+)", spec)
            if name:
                out.append(Dep("jsr", name.group(1), rel, line))


REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([<>=!~]=?[^;#\s]*)?")


def _pep508_name(spec):
    m = REQ_LINE.match(spec)
    return (m.group(1), (m.group(3) or "")) if m else (None, "")


def parse_requirements(rel, text, out):
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        if "github.com" in line:
            gh = _norm_gh(line)
            name = line.split("@")[0].strip() if " @ " in line else gh.split("/")[-1]
            out.append(Dep("pypi", name, rel, i, github=gh))
            continue
        name, ver = _pep508_name(line)
        if name:
            out.append(Dep("pypi", name, rel, i, ver))


def parse_pyproject(rel, text, out):
    try:
        data = tomllib.loads(text)
    except Exception:
        return
    specs = []
    proj = data.get("project") or {}
    specs += proj.get("dependencies") or []
    for group in (proj.get("optional-dependencies") or {}).values():
        specs += group
    for group in (data.get("dependency-groups") or {}).values():
        specs += [s for s in group if isinstance(s, str)]
    for s in specs:
        name, ver = _pep508_name(s)
        if name:
            out.append(Dep("pypi", name, rel, _find_line(text, s), ver))
    poetry = (data.get("tool") or {}).get("poetry") or {}
    tables = [poetry.get("dependencies") or {}, poetry.get("dev-dependencies") or {}]
    for g in (poetry.get("group") or {}).values():
        tables.append(g.get("dependencies") or {})
    for t in tables:
        for name, v in t.items():
            if name.lower() == "python":
                continue
            gh = _norm_gh(v.get("git", "")) if isinstance(v, dict) else ""
            out.append(Dep("pypi", name, rel, _find_line(text, name), v if isinstance(v, str) else "", github=gh))


def parse_go_mod(rel, text, out):
    inblock = False
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.split("//", 1)[0].strip()
        if line.startswith("require ("):
            inblock = True
            continue
        if inblock and line == ")":
            inblock = False
            continue
        m = None
        if inblock:
            m = re.match(r"(\S+)\s+(\S+)", line)
        elif line.startswith("require "):
            m = re.match(r"require\s+(\S+)\s+(\S+)", line)
        if m:
            mod = m.group(1)
            gh = ""
            if mod.startswith("github.com/"):
                parts = mod.split("/")
                if len(parts) >= 3:
                    gh = f"{parts[1]}/{parts[2]}"
            out.append(Dep("go", mod, rel, i, m.group(2), github=gh))


def parse_cargo(rel, text, out):
    try:
        data = tomllib.loads(text)
    except Exception:
        return
    tables = [data.get(k) or {} for k in ("dependencies", "dev-dependencies", "build-dependencies")]
    tables.append((data.get("workspace") or {}).get("dependencies") or {})
    for t in tables:
        for name, v in t.items():
            if isinstance(v, dict) and v.get("path"):
                continue
            real = v.get("package", name) if isinstance(v, dict) else name
            gh = _norm_gh(v.get("git", "")) if isinstance(v, dict) else ""
            out.append(Dep("crates", real, rel, _find_line(text, name), v if isinstance(v, str) else "", github=gh))


def parse_gemfile(rel, text, out):
    for m in re.finditer(r"""^\s*gem\s+["']([^"']+)["'](.*)$""", text, re.M):
        gh = ""
        gm = re.search(r"""github:\s*["']([^"']+)["']""", m.group(2))
        if gm:
            gh = gm.group(1)
        out.append(Dep("gems", m.group(1), rel, _lineno(text, m.start(1)), github=gh))


def parse_terraform(rel, text, out):
    for m in re.finditer(r"""source\s*=\s*"([^"]+)\"""", text):
        src = m.group(1)
        line = _lineno(text, m.start(1))
        if "github.com" in src:
            out.append(Dep("terraform", src, rel, line, github=_norm_gh(src)))
        elif re.match(r"^[\w-]+/[\w-]+$", src):
            out.append(Dep("terraform", src, rel, line))


def scan_text_patterns(rel, text, patterns, out):
    """patterns: list of (event_id, compiled regex)."""
    best = {}
    for eid, rx in patterns:
        m = rx.search(text)
        if m and (eid not in best or m.start() < best[eid].start()):
            best[eid] = m
    for eid, m in best.items():
        out.append(Dep("text", eid, rel, _lineno(text, m.start()), extra={"event": eid, "match": m.group(0)}))


# ---------------------------------------------------------------- walk
def classify(rel):
    base = os.path.basename(rel)
    low = base.lower()
    parts = rel.replace("\\", "/").split("/")
    if ".github" in parts and ("workflows" in parts) and low.endswith((".yml", ".yaml")):
        return "workflow"
    if low in ("action.yml", "action.yaml"):
        return "workflow"
    if low == "dockerfile" or low.startswith("dockerfile.") or low.endswith(".dockerfile") or low == "containerfile":
        return "dockerfile"
    if low == "chart.yaml":
        return "chart"
    if low == "package.json":
        return "package.json"
    if low in ("deno.json", "deno.jsonc"):
        return "deno"
    if low == "pyproject.toml":
        return "pyproject"
    if re.match(r"^requirements.*\.(txt|in)$", low) or (parts[-2:-1] == ["requirements"] and low.endswith(".txt")):
        return "requirements"
    if low == "go.mod":
        return "go.mod"
    if low == "cargo.toml":
        return "cargo"
    if low in ("gemfile", "gems.rb"):
        return "gemfile"
    if low.endswith(".tf"):
        return "terraform"
    if low.endswith((".yml", ".yaml")):
        return "yaml"
    return ""


PARSERS = {
    "workflow": parse_workflow, "dockerfile": parse_dockerfile, "chart": parse_chart,
    "package.json": parse_package_json, "deno": parse_deno_json, "pyproject": parse_pyproject,
    "requirements": parse_requirements, "go.mod": parse_go_mod, "cargo": parse_cargo,
    "gemfile": parse_gemfile, "terraform": parse_terraform,
}


def _yaml_generic(rel, text, out):
    parse_images_yaml(rel, text, out)
    parse_helm_text(rel, text, out)


def walk(root, text_patterns=(), exclude=()):
    root = os.path.abspath(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info") and not (d.startswith(".") and d not in (".github", ".gitlab", ".circleci", ".devcontainer")))
        for fn in sorted(filenames):
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if any(rel.startswith(e.rstrip("/") + "/") or rel == e for e in exclude):
                continue
            kind = classify(rel)
            ext = os.path.splitext(fn)[1].lower()
            if not kind and ext not in TEXT_EXT and not fn.lower().startswith("dockerfile"):
                continue
            try:
                if os.path.getsize(path) > MAX_TEXT:
                    continue
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            if kind in PARSERS:
                PARSERS[kind](rel, text, out)
            elif kind == "yaml":
                _yaml_generic(rel, text, out)
            if kind == "dockerfile":
                parse_helm_text(rel, text, out)
            if text_patterns and kind != "package.json" and ext not in (".md", ".txt", ".lock") \
                    and fn not in ("package-lock.json", "feed.json"):
                scan_text_patterns(rel, text, text_patterns, out)
    return dedupe(out)


def dedupe(deps):
    seen, res = set(), []
    for d in deps:
        k = (d.eco, d.name, d.file, d.line)
        if k in seen:
            continue
        seen.add(k)
        res.append(d)
    return res
