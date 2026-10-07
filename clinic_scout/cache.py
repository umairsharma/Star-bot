"""Tiny JSON file cache so repeat runs don't re-query OpenStreetMap services."""

import hashlib
import json
import time
from pathlib import Path

CACHE_DIR = Path(".cache")
TTL_SECONDS = 24 * 3600


def _path(namespace, key):
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / namespace / f"{digest}.json"


def load(namespace, key):
    path = _path(namespace, key)
    try:
        if time.time() - path.stat().st_mtime > TTL_SECONDS:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(namespace, key, data):
    path = _path(namespace, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass  # caching is best-effort
