"""Tiny HTTP layer with an on-disk cache. Tests swap `fetch_json` out."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from . import __version__

UA = f"exitradar/{__version__} (+https://github.com/hamarshehmhmd/exitradar)"
CACHE_TTL = 24 * 3600


class NetError(Exception):
    pass


def cache_dir():
    base = os.environ.get("EXITRADAR_CACHE") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"), "exitradar")
    os.makedirs(base, exist_ok=True)
    return base


def _cache_path(key):
    return os.path.join(cache_dir(), hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")


def http_json(url, data=None, headers=None, use_cache=True, timeout=20):
    key = url + (json.dumps(data, sort_keys=True) if data is not None else "")
    path = _cache_path(key)
    if use_cache and os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    hdrs.update(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            payload = {"__status__": 404}
        else:
            raise NetError(f"{url}: HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        raise NetError(f"{url}: {e}") from None
    if use_cache:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    return payload


fetch_json = http_json


def github_token():
    for k in ("EXITRADAR_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    if shutil.which("gh"):
        try:
            p = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return ""
