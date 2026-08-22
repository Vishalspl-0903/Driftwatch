"""drift/fetch.py -- resolve extracted image candidates to actual bytes.

extract.py only parses the DOM; it never proves an image URL still resolves
to real pixel data. mine_wayback.py's own quality probe already found that a
meaningful share of <img> elements fail to load during the render (see
broken_image_ratio in meta.json) -- the same URLs will often fail here too,
for the same reason (archive.org never captured that asset, or the capture
is gone). That is expected and handled by returning only what succeeded;
callers decide whether what's left is enough to trust.

All fetches stay on web.archive.org (the candidate URLs are already Wayback
replay URLs produced by extract.py's resolution against the page's replay
base), so this does not reintroduce the "live CDN into an old snapshot"
contamination mine_wayback.py's render step was designed to avoid.

Bytes are cached to disk keyed by URL hash, since the same fetch is repeated
across dev iterations of embed.py/score.py and archive.org is a shared,
rate-limited resource -- no reason to hit it twice for the same URL.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

DEFAULT_CACHE_DIR = Path("data/drift_image_cache")
FETCH_TIMEOUT = 15.0
FETCH_DELAY = 0.15  # polite spacing; small dataset, not the CDX endpoint
MIN_BYTES = 512  # smaller than this is a 1x1 gif / error stub, not a real image

USER_AGENT = "Driftwatch-drift-detector/0.1 (research; contact via repo)"


@dataclass
class FetchResult:
    url: str
    ok: bool
    content: bytes | None = None
    reason: str = ""


def _cache_path(url: str, cache_dir: Path) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.bin"


def fetch_images(
    urls: list[str],
    cache_dir: Path = DEFAULT_CACHE_DIR,
    client: httpx.Client | None = None,
) -> list[FetchResult]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    own_client = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    results: list[FetchResult] = []
    try:
        for url in urls:
            cpath = _cache_path(url, cache_dir)
            if cpath.exists():
                data = cpath.read_bytes()
                if len(data) >= MIN_BYTES:
                    results.append(FetchResult(url, True, data))
                else:
                    results.append(FetchResult(url, False, reason=f"cached-too-small ({len(data)}B)"))
                continue
            try:
                time.sleep(FETCH_DELAY)
                resp = client.get(url, timeout=FETCH_TIMEOUT)
            except httpx.HTTPError as exc:
                results.append(FetchResult(url, False, reason=f"{type(exc).__name__}: {exc}"))
                continue
            if resp.status_code != 200:
                results.append(FetchResult(url, False, reason=f"HTTP {resp.status_code}"))
                continue
            content = resp.content
            cpath.write_bytes(content)  # cache negatives too (as empty-ish) so reruns don't refetch dead links
            if len(content) < MIN_BYTES:
                results.append(FetchResult(url, False, reason=f"too small ({len(content)}B)"))
                continue
            results.append(FetchResult(url, True, content))
    finally:
        if own_client:
            client.close()
    return results
