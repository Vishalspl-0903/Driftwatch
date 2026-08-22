#!/usr/bin/env python3
"""
mine_wayback.py — build a t0/t1 storefront pair dataset from the Wayback Machine.

Driftwatch measures *change over time*, so the dataset unit is a PAIR of captures
of the same merchant storefront, separated by enough time that a real business
pivot could have happened. This script mines those pairs.

Per domain:
  1. Query the archive.org CDX API for every HTML capture of the homepage.
  2. Drop captures that are not real content (non-200, non-HTML, or a WARC
     record too small to be anything but a parked/failed/placeholder grab).
  3. Pick the two most time-separated survivors, >= --min-gap-days apart.
  4. Render both through Playwright against the Wayback replay URL:
     full-page screenshot + rendered DOM text + rendered HTML.
  5. Write data/pairs/<domain>/{t0,t1}/{screenshot.png, dom.txt, dom.html, meta.json}

Design notes worth defending:
  * Replay uses the `if_` modifier so the Wayback toolbar is never injected into
    the DOM or the screenshot. Toolbar nodes are also stripped defensively.
  * Wayback silently serves the *nearest* capture to the one you asked for. We
    read the served timestamp back off the final URL and compute the gap from
    what we actually got, not from what we asked for. A pair that collapses
    below --min-gap-days after redirect is rejected and the next candidate pair
    is tried.
  * All non-archive.org subresource requests are blocked. An archived page whose
    JS phones a live CDN would otherwise render 2026 content into a 2019
    snapshot, which would poison the drift labels.
  * Failures are enumerated, not swallowed. Every domain ends with exactly one
    status code, written to status.json and to the run log.

Usage:
  python scripts/mine_wayback.py data/domains.txt
  python scripts/mine_wayback.py data/domains.txt --dry-run       # CDX only
  python scripts/mine_wayback.py data/domains.txt --only foo.in --refresh-cdx
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import hashlib
import json
import random
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("missing dependency: pip install httpx")

try:
    from playwright.async_api import Browser, BrowserContext, Error as PWError
    from playwright.async_api import TimeoutError as PWTimeout
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    sys.exit("missing dependency: pip install playwright && playwright install chromium")


SCRIPT_VERSION = "0.1.0"
CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
REPLAY_TEMPLATE = "https://web.archive.org/web/{timestamp}if_/{url}"
SERVED_TS_RE = re.compile(r"/web/(\d{14})")

# ---------------------------------------------------------------------------
# Status vocabulary. One of these lands in status.json and the run log for
# every domain; nothing is allowed to fail anonymously.
# ---------------------------------------------------------------------------
OK = "ok"
SKIPPED_DONE = "skipped_already_done"
CDX_HTTP_ERROR = "cdx_http_error"
CDX_FORBIDDEN = "cdx_forbidden"  # persistent 403 -- usually an archive.org exclusion request
CDX_EMPTY = "cdx_no_captures"
CDX_PARSE_ERROR = "cdx_parse_error"
NO_VIABLE_CAPTURES = "no_viable_captures"
GAP_TOO_SMALL = "gap_too_small"
ALL_CANDIDATES_FAILED = "all_candidate_pairs_failed"
DRY_RUN_OK = "dry_run_pair_found"

# Per-render failure reasons (recorded as detail on the domain-level status).
R_NAV_TIMEOUT = "render_nav_timeout"
R_NAV_ERROR = "render_nav_error"
R_HTTP_ERROR = "render_http_error"
R_REPLAY_ERROR_PAGE = "replay_error_page"
R_EMPTY_DOM = "empty_dom"
R_SCREENSHOT_FAILED = "screenshot_failed"
R_NO_SERVED_TS = "no_served_timestamp"
R_ASSETS_BROKEN = "broken_replay_assets"  # most images 404'd -- a gutted render, not a gutted store
R_NO_STYLES = "no_stylesheets_applied"  # CSS never loaded; the screenshot is unusable

# Text that means Wayback served us its own error/interstitial page rather than
# the merchant's storefront. Matched case-insensitively against body text.
WAYBACK_ERROR_MARKERS = (
    "wayback machine has not archived that url",
    "this page is not available",
    "got an http 30",
    "got an http 40",
    "got an http 50",
    "the wayback machine is an initiative",  # bare interstitial with no content
    "sorry, this snapshot cannot be displayed",
    "job failed",
    "redirecting to",
)

TOOLBAR_SELECTORS = ("#wm-ipp-base", "#wm-ipp", "#donato", ".wb-autocomplete-suggestions")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def slugify_domain(raw: str) -> str:
    """example.com, https://www.example.com/, WWW.Example.com -> example.com"""
    s = raw.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = s.split("/", 1)[0]
    s = re.sub(r"^www\.", "", s)
    return re.sub(r"[^a-z0-9._-]", "_", s)


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def day_gap(a: datetime, b: datetime) -> int:
    return abs((b - a).days)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def read_domains(path: Path) -> list[str]:
    out, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        slug = slugify_domain(line)
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
class RateLimiter:
    """Process-global minimum interval between archive.org navigations/queries.

    Gates CDX queries and page navigations only, not subresources -- gating every
    image would make a single page take minutes and is not what a polite browser
    does anyway. Jitter avoids lockstep bursts when concurrency > 1.
    """

    def __init__(self, min_interval: float, jitter: float = 0.35) -> None:
        self.min_interval = min_interval
        self.jitter = jitter
        self._lock = asyncio.Lock()
        self._next_free = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_free - now)
            spacing = self.min_interval + random.uniform(0, self.jitter)
            self._next_free = max(now, self._next_free) + spacing
        if wait > 0:
            await asyncio.sleep(wait)

    async def penalise(self, seconds: float) -> None:
        """Push the whole process back after a 429/503."""
        async with self._lock:
            self._next_free = max(self._next_free, time.monotonic()) + seconds


# ---------------------------------------------------------------------------
# CDX
# ---------------------------------------------------------------------------
@dataclass
class Capture:
    timestamp: str
    original: str
    statuscode: str
    mimetype: str
    digest: str
    length: int

    @property
    def dt(self) -> datetime:
        return parse_ts(self.timestamp)

    @property
    def replay_url(self) -> str:
        return REPLAY_TEMPLATE.format(timestamp=self.timestamp, url=self.original)

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "iso": self.dt.isoformat(),
            "original": self.original,
            "statuscode": self.statuscode,
            "mimetype": self.mimetype,
            "digest": self.digest,
            "length": self.length,
        }


@dataclass
class CdxResult:
    rows: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    detail: str = ""


async def fetch_cdx(
    client: httpx.AsyncClient,
    domain: str,
    limiter: RateLimiter,
    args: argparse.Namespace,
    cache_dir: Path,
) -> CdxResult:
    cache_path = cache_dir / f"{domain}.json"
    if cache_path.exists() and not args.refresh_cdx:
        try:
            return CdxResult(rows=json.loads(cache_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            cache_path.unlink(missing_ok=True)  # corrupt cache, refetch

    params = {
        "url": domain,
        "matchType": "exact",
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,digest,length",
        "filter": ["statuscode:200", "mimetype:text/html"],
        # timestamp:6 = one capture per calendar month. Heavily-archived merchants
        # have tens of thousands of daily captures; `limit` truncates from the
        # OLDEST end, so daily granularity silently amputates the recent captures
        # that t1 needs. Monthly is the right resolution for pair mining anyway.
        "collapse": f"timestamp:{args.collapse_digits}",
        "limit": str(args.cdx_limit),
    }
    if args.from_year:
        params["from"] = str(args.from_year)
    if args.to_year:
        params["to"] = str(args.to_year)

    last_detail = ""
    for attempt in range(1, args.cdx_retries + 1):
        await limiter.acquire()
        try:
            resp = await client.get(CDX_ENDPOINT, params=params, timeout=args.cdx_timeout)
        except httpx.TimeoutException:
            last_detail = f"timeout after {args.cdx_timeout}s (attempt {attempt})"
            await asyncio.sleep(min(30, 2**attempt))
            continue
        except httpx.HTTPError as exc:
            last_detail = f"{type(exc).__name__}: {exc} (attempt {attempt})"
            await asyncio.sleep(min(30, 2**attempt))
            continue

        # 403 is ambiguous on archive.org: it is both the throttle response and
        # the permanent "excluded on request" response. Retry it; if it survives
        # every retry, call it an exclusion and move on.
        if resp.status_code in (403, 429, 503, 504):
            retry_after = resp.headers.get("Retry-After")
            backoff = float(retry_after) if (retry_after or "").isdigit() else min(60.0, 5.0 * 2**attempt)
            last_detail = f"HTTP {resp.status_code}, backing off {backoff:.0f}s (attempt {attempt})"
            await limiter.penalise(backoff)
            await asyncio.sleep(backoff)
            if attempt == args.cdx_retries and resp.status_code == 403:
                return CdxResult(
                    error=CDX_FORBIDDEN,
                    detail=f"HTTP 403 after {attempt} attempts (likely archive.org exclusion)",
                )
            continue
        if resp.status_code != 200:
            return CdxResult(error=CDX_HTTP_ERROR, detail=f"HTTP {resp.status_code}")

        body = resp.text.strip()
        if not body:
            cache_path.write_text("[]", encoding="utf-8")
            return CdxResult(rows=[])
        try:
            raw = json.loads(body)
        except json.JSONDecodeError as exc:
            return CdxResult(error=CDX_PARSE_ERROR, detail=f"{exc} :: {body[:120]!r}")
        if not raw:
            cache_path.write_text("[]", encoding="utf-8")
            return CdxResult(rows=[])

        header, *data = raw
        rows = [dict(zip(header, r)) for r in data]
        cache_path.write_text(json.dumps(rows), encoding="utf-8")
        return CdxResult(rows=rows)

    return CdxResult(error=CDX_HTTP_ERROR, detail=last_detail or "exhausted retries")


def viable_captures(rows: Sequence[dict[str, str]], min_length: int) -> tuple[list[Capture], dict[str, int]]:
    """Filter CDX rows to captures that plausibly contain a real storefront.

    CDX `length` is the compressed WARC record size. Parked domains, registrar
    holding pages and truncated grabs come in around 200-1500 bytes; a rendered
    Indian SMB storefront is comfortably above 3KB compressed. This is the
    cheapest possible junk filter and it runs before we spend any browser time.
    """
    rejects = {"bad_timestamp": 0, "not_200": 0, "not_html": 0, "too_small": 0, "no_length": 0}
    out: list[Capture] = []
    for r in rows:
        if r.get("statuscode") != "200":
            rejects["not_200"] += 1
            continue
        if "html" not in (r.get("mimetype") or ""):
            rejects["not_html"] += 1
            continue
        raw_len = (r.get("length") or "").strip()
        if not raw_len.isdigit():
            rejects["no_length"] += 1
            continue
        length = int(raw_len)
        if length < min_length:
            rejects["too_small"] += 1
            continue
        try:
            parse_ts(r["timestamp"])
        except (KeyError, ValueError):
            rejects["bad_timestamp"] += 1
            continue
        out.append(
            Capture(
                timestamp=r["timestamp"],
                original=r["original"],
                statuscode=r["statuscode"],
                mimetype=r["mimetype"],
                digest=r.get("digest", ""),
                length=length,
            )
        )
    out.sort(key=lambda c: c.timestamp)
    return out, rejects


def candidate_pairs(
    captures: Sequence[Capture],
    min_gap_days: int,
    max_gap_days: int | None,
    max_alternates: int,
) -> list[tuple[Capture, Capture]]:
    """Widest-gap-first candidate pairs, optionally capped to a gap band.

    Unbounded, the primary pair is (earliest, latest). Alternates walk inward
    from each end so that one dead capture -- Wayback replay failures are common
    and not predictable from CDX metadata -- does not cost us the whole domain.

    With --max-gap-days set, t1 anchors on the newest capture and t0 walks back
    to the OLDEST capture still inside the band, which is the pair that most
    resembles a production rescan interval.
    """
    if len(captures) < 2:
        return []
    if max_gap_days is not None:
        anchors = list(reversed(captures[-max_alternates:]))
        pairs: list[tuple[Capture, Capture]] = []
        seen: set[tuple[str, str]] = set()
        for t in anchors:
            in_band = [
                c for c in captures if min_gap_days <= day_gap(c.dt, t.dt) <= max_gap_days and c.timestamp < t.timestamp
            ]
            # widest gap inside the band first, then progressively tighter
            for h in sorted(in_band, key=lambda c: c.timestamp)[:max_alternates]:
                key = (h.timestamp, t.timestamp)
                if key not in seen:
                    seen.add(key)
                    pairs.append((h, t))
        pairs.sort(key=lambda p: day_gap(p[0].dt, p[1].dt), reverse=True)
        return pairs

    heads = captures[:max_alternates]
    tails = list(reversed(captures[-max_alternates:]))
    pairs = []
    seen = set()
    for h in heads:
        for t in tails:
            if t.timestamp <= h.timestamp:
                continue
            key = (h.timestamp, t.timestamp)
            if key in seen:
                continue
            if day_gap(h.dt, t.dt) < min_gap_days:
                continue
            seen.add(key)
            pairs.append((h, t))
    pairs.sort(key=lambda p: day_gap(p[0].dt, p[1].dt), reverse=True)
    return pairs


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
@dataclass
class Render:
    ok: bool
    reason: str = ""
    detail: str = ""
    served_timestamp: str | None = None
    final_url: str = ""
    http_status: int | None = None
    text_chars: int = 0
    text_sha256: str = ""
    html_bytes: int = 0
    screenshot_bytes: int = 0
    screenshot_full_page: bool = True
    render_seconds: float = 0.0
    dir: Path | None = None
    quality: dict[str, Any] = field(default_factory=dict)

    @property
    def served_dt(self) -> datetime:
        assert self.served_timestamp
        return parse_ts(self.served_timestamp)


# Measured in-page after load. These separate "the merchant gutted their store"
# from "Wayback could not serve me the store", which look identical to every
# downstream drift signal and would otherwise become false positives.
QUALITY_PROBE = """() => {
    const imgs = Array.from(document.images)
        .filter(i => i.currentSrc || i.getAttribute('src'));
    let rules = 0;
    for (const ss of Array.from(document.styleSheets)) {
        try { rules += (ss.cssRules || []).length; } catch (e) { rules += 1; }
    }
    return {
        images_total: imgs.length,
        images_broken: imgs.filter(i => i.naturalWidth === 0).length,
        stylesheets: document.styleSheets.length,
        css_rules: rules,
        inline_style_blocks: document.querySelectorAll('style').length,
        dom_nodes: document.getElementsByTagName('*').length,
        links: document.querySelectorAll('a[href]').length,
        forms: document.querySelectorAll('form').length,
    };
}"""


async def _make_context(
    browser: Browser, args: argparse.Namespace
) -> tuple[BrowserContext, dict[str, int]]:
    ctx = await browser.new_context(
        viewport={"width": args.viewport_width, "height": args.viewport_height},
        user_agent=args.user_agent,
        java_script_enabled=True,
        ignore_https_errors=True,
    )
    ctx.set_default_timeout(args.nav_timeout * 1000)
    counters = {"blocked_offsite": 0, "request_failed": 0, "http_4xx_5xx": 0}

    async def gate(route, request):
        # Keep the snapshot honest: only archive.org may serve bytes into it.
        parsed = urlparse(request.url)
        if parsed.scheme in ("data", "blob", "about"):
            await route.continue_()
            return
        host = parsed.hostname or ""
        if host == "archive.org" or host.endswith(".archive.org"):
            await route.continue_()
        else:
            counters["blocked_offsite"] += 1
            with contextlib.suppress(PWError):
                await route.abort()

    await ctx.route("**/*", gate)
    ctx.on("requestfailed", lambda _r: counters.__setitem__("request_failed", counters["request_failed"] + 1))
    ctx.on(
        "response",
        lambda r: counters.__setitem__("http_4xx_5xx", counters["http_4xx_5xx"] + 1) if r.status >= 400 else None,
    )
    return ctx, counters


async def render_capture(
    ctx: BrowserContext,
    counters: dict[str, int],
    capture: Capture,
    out_dir: Path,
    limiter: RateLimiter,
    args: argparse.Namespace,
) -> Render:
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    for k in counters:
        counters[k] = 0
    page = await ctx.new_page()
    try:
        await limiter.acquire()
        try:
            resp = await page.goto(
                capture.replay_url,
                wait_until="domcontentloaded",
                timeout=args.nav_timeout * 1000,
            )
        except PWTimeout:
            return Render(False, R_NAV_TIMEOUT, f"domcontentloaded > {args.nav_timeout}s")
        except PWError as exc:
            return Render(False, R_NAV_ERROR, f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}")

        http_status = resp.status if resp else None
        if http_status is not None and http_status >= 400:
            return Render(False, R_HTTP_ERROR, f"HTTP {http_status}", http_status=http_status)

        # Archived pages routinely never reach networkidle: rewritten assets 404
        # slowly and some never resolve. Treat it as best-effort, not required.
        with contextlib.suppress(PWTimeout, PWError):
            await page.wait_for_load_state("networkidle", timeout=args.settle_timeout * 1000)
        await page.wait_for_timeout(args.settle_ms)

        # Lazy-loaded product grids are the whole point of the dataset; scroll
        # them into existence before screenshotting.
        with contextlib.suppress(PWError, PWTimeout):
            await page.evaluate(
                """async (step) => {
                    const sleep = ms => new Promise(r => setTimeout(r, ms));
                    let y = 0;
                    const limit = Math.min(document.body.scrollHeight, 40000);
                    while (y < limit) { y += step; window.scrollTo(0, y); await sleep(120); }
                    window.scrollTo(0, 0);
                    await sleep(300);
                }""",
                args.viewport_height,
            )

        with contextlib.suppress(PWError, PWTimeout):
            await page.evaluate(
                """(sels) => sels.forEach(s =>
                    document.querySelectorAll(s).forEach(n => n.remove()))""",
                list(TOOLBAR_SELECTORS),
            )

        served = None
        m = SERVED_TS_RE.search(page.url)
        if m:
            served = m.group(1)
        if not served:
            return Render(False, R_NO_SERVED_TS, f"final url {page.url[:200]}", http_status=http_status)

        try:
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            html = await page.content()
        except PWError as exc:
            return Render(False, R_NAV_ERROR, f"dom read failed: {exc}", http_status=http_status)

        text = (text or "").strip()
        lowered = text[:4000].lower()
        hit = next((mk for mk in WAYBACK_ERROR_MARKERS if mk in lowered), None)
        if hit and len(text) < args.error_page_max_chars:
            return Render(False, R_REPLAY_ERROR_PAGE, f"marker={hit!r} chars={len(text)}", http_status=http_status)
        if len(text) < args.min_text_chars:
            return Render(False, R_EMPTY_DOM, f"{len(text)} chars < {args.min_text_chars}", http_status=http_status)

        try:
            quality = await page.evaluate(QUALITY_PROBE)
        except PWError:
            quality = {}
        quality.update(counters)
        total_imgs = quality.get("images_total", 0)
        broken = quality.get("images_broken", 0)
        quality["broken_image_ratio"] = round(broken / total_imgs, 3) if total_imgs else None

        if not args.no_quality_gate:
            # A replay that lost its images or its CSS produces exactly the same
            # downstream signature as a merchant who deleted their catalogue.
            # Refuse it here; the caller falls back to another capture.
            if total_imgs >= args.min_images_for_check and broken / total_imgs > args.max_broken_image_ratio:
                return Render(
                    False,
                    R_ASSETS_BROKEN,
                    f"{broken}/{total_imgs} images failed to load "
                    f"(> {args.max_broken_image_ratio:.0%}); blocked_offsite="
                    f"{counters['blocked_offsite']} http_4xx_5xx={counters['http_4xx_5xx']}",
                    http_status=http_status,
                )
            if (
                quality.get("css_rules", 0) == 0
                and quality.get("inline_style_blocks", 0) == 0
                and quality.get("dom_nodes", 0) > 50
            ):
                return Render(
                    False,
                    R_NO_STYLES,
                    f"0 css rules over {quality.get('dom_nodes')} nodes -- unstyled render",
                    http_status=http_status,
                )

        shot_path = out_dir / "screenshot.png"
        full_page = True
        try:
            await page.screenshot(path=str(shot_path), full_page=True, timeout=args.screenshot_timeout * 1000)
        except (PWError, PWTimeout) as exc:
            # Chromium refuses screenshots past ~16k px. A viewport shot is worth
            # more than nothing, and meta records which we got.
            full_page = False
            try:
                await page.screenshot(path=str(shot_path), full_page=False, timeout=args.screenshot_timeout * 1000)
            except (PWError, PWTimeout) as exc2:
                return Render(
                    False,
                    R_SCREENSHOT_FAILED,
                    f"full={str(exc).splitlines()[0][:120]} | viewport={str(exc2).splitlines()[0][:120]}",
                    http_status=http_status,
                )

        (out_dir / "dom.txt").write_text(text, encoding="utf-8")
        (out_dir / "dom.html").write_text(html, encoding="utf-8")

        return Render(
            ok=True,
            served_timestamp=served,
            final_url=page.url,
            http_status=http_status,
            text_chars=len(text),
            text_sha256=sha256_text(text),
            html_bytes=len(html.encode("utf-8", "replace")),
            screenshot_bytes=shot_path.stat().st_size,
            screenshot_full_page=full_page,
            render_seconds=round(time.monotonic() - started, 2),
            dir=out_dir,
            quality=quality,
        )
    finally:
        with contextlib.suppress(PWError):
            await page.close()


# ---------------------------------------------------------------------------
# Per-domain orchestration
# ---------------------------------------------------------------------------
@dataclass
class DomainOutcome:
    domain: str
    status: str
    detail: str = ""
    t0: str = "-"
    t1: str = "-"
    gap_days: int | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    cdx_total: int = 0
    cdx_viable: int = 0
    cdx_rejects: dict[str, int] = field(default_factory=dict)

    def log_line(self, idx: int, total: int) -> str:
        gap = f"{self.gap_days}d" if self.gap_days is not None else "-"
        tail = f"{self.status}" + (f": {self.detail}" if self.detail else "")
        return (
            f"[{idx:>4}/{total}] {self.domain:<34} "
            f"t0={self.t0:<14} t1={self.t1:<14} gap={gap:<7} {tail}"
        )


def is_complete(domain_dir: Path) -> bool:
    status_path = domain_dir / "status.json"
    if not status_path.exists():
        return False
    try:
        status = json.loads(status_path.read_text(encoding="utf-8")).get("status")
    except (json.JSONDecodeError, OSError):
        return False
    if status != OK:
        return False
    required = ["screenshot.png", "dom.txt", "meta.json"]
    return all((domain_dir / side / f).exists() for side in ("t0", "t1") for f in required)


def write_pair_meta(
    domain: str,
    domain_dir: Path,
    c0: Capture,
    c1: Capture,
    r0: Render,
    r1: Render,
    gap: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    identical = r0.text_sha256 == r1.text_sha256
    lo, hi = sorted((r0.text_chars, r1.text_chars))
    text_ratio = round(lo / hi, 3) if hi else 0.0

    # Category drift is a cosine distance between t0 and t1 catalogue image
    # centroids, so a lopsided count of *usable* images is not a footnote -- it
    # changes what that distance means. Recorded, not gated: a merchant really
    # shrinking their catalogue must stay in the dataset.
    def usable(r: Render) -> int:
        return max(0, r.quality.get("images_total", 0) - r.quality.get("images_broken", 0))

    ulo, uhi = sorted((usable(r0), usable(r1)))
    image_ratio = round(ulo / uhi, 3) if uhi else 0.0
    review = text_ratio < args.review_text_ratio or image_ratio < args.review_text_ratio
    for side, cap, ren in (("t0", c0, r0), ("t1", c1, r1)):
        side_dir = domain_dir / side
        side_dir.mkdir(parents=True, exist_ok=True)
        for name in ("screenshot.png", "dom.txt", "dom.html"):
            src = ren.dir / name
            if src.exists():
                shutil.copy2(src, side_dir / name)
        meta = {
            "schema": "driftwatch/pair-side/1",
            "script_version": SCRIPT_VERSION,
            "domain": domain,
            "side": side,
            # Both timestamps live in both metas so either file alone is enough
            # to reconstruct the pair.
            "t0_timestamp": r0.served_timestamp,
            "t1_timestamp": r1.served_timestamp,
            "t0_iso": r0.served_dt.isoformat(),
            "t1_iso": r1.served_dt.isoformat(),
            "gap_days": gap,
            "gap_days_requested": day_gap(c0.dt, c1.dt),
            "this": {
                "requested_timestamp": cap.timestamp,
                "served_timestamp": ren.served_timestamp,
                "redirected": cap.timestamp != ren.served_timestamp,
                "iso": ren.served_dt.isoformat(),
                "replay_url": cap.replay_url,
                "final_url": ren.final_url,
                "original_url": cap.original,
                "cdx": cap.as_dict(),
                "http_status": ren.http_status,
                "text_chars": ren.text_chars,
                "text_sha256": ren.text_sha256,
                "html_bytes": ren.html_bytes,
                "screenshot_bytes": ren.screenshot_bytes,
                "screenshot_full_page": ren.screenshot_full_page,
                "render_seconds": ren.render_seconds,
                "quality": ren.quality,
            },
            # True when both sides rendered byte-identical text. Not a failure:
            # these are the natural seed for the stable control group.
            "identical_rendered_text": identical,
            "cdx_same_digest": bool(c0.digest) and c0.digest == c1.digest,
            # Ratio of the shorter side's text to the longer. A low value is
            # NOT auto-rejected -- a merchant really gutting their catalogue
            # looks the same -- but it is the first thing to eyeball, because a
            # half-loaded replay looks the same too.
            "text_char_ratio": text_ratio,
            "usable_image_ratio": image_ratio,
            "usable_images": {"t0": usable(r0), "t1": usable(r1)},
            "needs_manual_review": review,
            "capture": {
                "viewport": [args.viewport_width, args.viewport_height],
                "offsite_requests_blocked": True,
                "toolbar_stripped": True,
                "mined_at": datetime.now(timezone.utc).isoformat(),
            },
            "label": {"drift_class": None, "notes": ""},  # filled by hand during inspection
        }
        (side_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"text_ratio": text_ratio, "image_ratio": image_ratio, "review": review}


async def process_domain(
    domain: str,
    client: httpx.AsyncClient,
    browser: Browser | None,
    limiter: RateLimiter,
    args: argparse.Namespace,
    pairs_root: Path,
    cache_root: Path,
) -> DomainOutcome:
    domain_dir = pairs_root / domain
    if is_complete(domain_dir) and not args.force:
        try:
            prev = json.loads((domain_dir / "status.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prev = {}
        return DomainOutcome(
            domain,
            SKIPPED_DONE,
            t0=prev.get("t0") or "-",
            t1=prev.get("t1") or "-",
            gap_days=prev.get("gap_days"),
        )

    cdx = await fetch_cdx(client, domain, limiter, args, cache_root)
    if cdx.error:
        return DomainOutcome(domain, cdx.error, cdx.detail)
    if not cdx.rows:
        return DomainOutcome(domain, CDX_EMPTY, "cdx returned 0 rows")

    captures, rejects = viable_captures(cdx.rows, args.min_capture_length)
    reject_summary = ", ".join(f"{k}={v}" for k, v in rejects.items() if v)
    base = DomainOutcome(
        domain,
        OK,
        cdx_total=len(cdx.rows),
        cdx_viable=len(captures),
        cdx_rejects=rejects,
    )
    if len(captures) < 2:
        base.status = NO_VIABLE_CAPTURES
        base.detail = f"{len(captures)} of {len(cdx.rows)} captures viable ({reject_summary or 'none rejected'})"
        return base

    if len(cdx.rows) >= args.cdx_limit:
        # CDX truncates from the oldest end, so a hit limit means the NEWEST
        # captures are missing and t1 is not really t1.
        base.detail = f"WARNING cdx truncated at limit {args.cdx_limit}; raise --cdx-limit. "

    pairs = candidate_pairs(captures, args.min_gap_days, args.max_gap_days, args.max_alternates)
    if not pairs:
        widest = day_gap(captures[0].dt, captures[-1].dt)
        band = f"[{args.min_gap_days}, {args.max_gap_days or 'inf'}]d"
        base.status = GAP_TOO_SMALL
        base.detail += f"no viable pair in band {band}; widest gap {widest}d ({len(captures)} viable captures)"
        base.t0, base.t1, base.gap_days = captures[0].timestamp, captures[-1].timestamp, widest
        return base

    if args.dry_run or browser is None:
        c0, c1 = pairs[0]
        base.status = DRY_RUN_OK
        base.t0, base.t1 = c0.timestamp, c1.timestamp
        base.gap_days = day_gap(c0.dt, c1.dt)
        base.detail += f"{len(captures)}/{len(cdx.rows)} viable, {len(pairs)} candidate pairs"
        return base

    scratch = domain_dir / ".renders"
    ctx, counters = await _make_context(browser, args)
    cache: dict[str, Render] = {}

    async def get_render(cap: Capture) -> Render:
        if cap.timestamp not in cache:
            cache[cap.timestamp] = await render_capture(
                ctx, counters, cap, scratch / cap.timestamp, limiter, args
            )
        return cache[cap.timestamp]

    try:
        for attempt_no, (c0, c1) in enumerate(pairs[: args.max_attempts], start=1):
            attempt: dict[str, Any] = {
                "attempt": attempt_no,
                "t0_requested": c0.timestamp,
                "t1_requested": c1.timestamp,
                "requested_gap_days": day_gap(c0.dt, c1.dt),
            }
            r0 = await get_render(c0)
            if not r0.ok:
                attempt.update(result="t0_failed", reason=r0.reason, detail=r0.detail)
                base.attempts.append(attempt)
                continue
            r1 = await get_render(c1)
            if not r1.ok:
                attempt.update(result="t1_failed", reason=r1.reason, detail=r1.detail)
                base.attempts.append(attempt)
                continue

            gap = day_gap(r0.served_dt, r1.served_dt)
            attempt["served_gap_days"] = gap
            if gap < args.min_gap_days:
                # Wayback redirected one side to a nearby capture and collapsed
                # the gap. Reject rather than quietly shipping a 12-day "pair".
                attempt.update(
                    result="gap_collapsed_after_redirect",
                    reason=GAP_TOO_SMALL,
                    detail=f"served {r0.served_timestamp}->{r1.served_timestamp} = {gap}d",
                )
                base.attempts.append(attempt)
                continue

            attempt["result"] = "ok"
            base.attempts.append(attempt)
            skew = write_pair_meta(domain, domain_dir, c0, c1, r0, r1, gap, args)
            base.status = OK
            base.t0, base.t1 = r0.served_timestamp or "-", r1.served_timestamp or "-"
            base.gap_days = gap
            bits = [f"{len(captures)}/{len(cdx.rows)} viable"]
            if attempt_no > 1:
                bits.append(f"pair #{attempt_no}")
            if r0.text_sha256 == r1.text_sha256:
                bits.append("IDENTICAL TEXT (control candidate)")
            if skew["review"]:
                bits.append(f"REVIEW text_ratio={skew['text_ratio']} img_ratio={skew['image_ratio']}")
            base.detail += ", ".join(bits)
            if not args.keep_renders:
                shutil.rmtree(scratch, ignore_errors=True)
            return base

        reasons = [f"{a.get('result')}({a.get('reason')})" for a in base.attempts]
        base.status = ALL_CANDIDATES_FAILED
        base.detail += f"{len(base.attempts)} attempted: " + "; ".join(reasons[:4])
        return base
    finally:
        with contextlib.suppress(PWError):
            await ctx.close()


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------
LOG_FIELDS = ["run_started", "domain", "t0", "t1", "gap_days", "status", "detail", "cdx_total", "cdx_viable"]


def append_log(path: Path, run_started: str, outcome: DomainOutcome) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow(
            {
                "run_started": run_started,
                "domain": outcome.domain,
                "t0": outcome.t0,
                "t1": outcome.t1,
                "gap_days": outcome.gap_days if outcome.gap_days is not None else "",
                "status": outcome.status,
                "detail": outcome.detail,
                "cdx_total": outcome.cdx_total,
                "cdx_viable": outcome.cdx_viable,
            }
        )


def write_status(pairs_root: Path, outcome: DomainOutcome, run_started: str) -> None:
    if outcome.status == SKIPPED_DONE:
        return
    d = pairs_root / outcome.domain
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(
        json.dumps(
            {
                "schema": "driftwatch/mine-status/1",
                "script_version": SCRIPT_VERSION,
                "domain": outcome.domain,
                "status": outcome.status,
                "detail": outcome.detail,
                "t0": outcome.t0,
                "t1": outcome.t1,
                "gap_days": outcome.gap_days,
                "cdx_total": outcome.cdx_total,
                "cdx_viable": outcome.cdx_viable,
                "cdx_rejects": outcome.cdx_rejects,
                "attempts": outcome.attempts,
                "run_started": run_started,
                "finished": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Mine t0/t1 storefront snapshot pairs from the Wayback Machine.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("domains", type=Path, help="text file of merchant domains, one per line")
    p.add_argument("--out", type=Path, default=Path("data/pairs"), help="output root for pairs")
    p.add_argument("--cdx-cache", type=Path, default=Path("data/cdx_cache"), help="CDX response cache")
    p.add_argument("--log", type=Path, default=Path("data/mine_wayback_log.csv"), help="append-only run log")

    sel = p.add_argument_group("selection")
    sel.add_argument("--min-gap-days", type=int, default=90, help="minimum days between t0 and t1")
    sel.add_argument("--max-gap-days", type=int, default=None,
                     help="cap the gap. Unset = widest possible pair, which on a 2011-2026 archive "
                          "measures a decade of web-design fashion, not merchant drift. Set this to "
                          "your intended rescan interval (e.g. 540) for a dataset that transfers.")
    sel.add_argument("--min-capture-length", type=int, default=3000,
                     help="drop CDX captures whose WARC record is smaller than this (parked/failed grabs)")
    sel.add_argument("--max-alternates", type=int, default=4,
                     help="fallback captures to consider from each end when a render fails")
    sel.add_argument("--max-attempts", type=int, default=6, help="candidate pairs to try per domain")
    sel.add_argument("--collapse-digits", type=int, default=6, choices=[4, 6, 8, 10],
                     help="CDX timestamp collapse: 4=yearly 6=monthly 8=daily 10=hourly")
    sel.add_argument("--cdx-limit", type=int, default=10000, help="max CDX rows per domain")
    sel.add_argument("--from-year", type=int, default=None, help="earliest capture year, e.g. 2016")
    sel.add_argument("--to-year", type=int, default=None, help="latest capture year")

    net = p.add_argument_group("politeness / network")
    net.add_argument("--rate", type=float, default=3.0, help="min seconds between archive.org navigations")
    net.add_argument("--concurrency", type=int, default=2, help="domains in flight (rate limit is global)")
    net.add_argument("--cdx-timeout", type=float, default=60.0)
    net.add_argument("--cdx-retries", type=int, default=4)
    net.add_argument("--nav-timeout", type=float, default=60.0, help="seconds for domcontentloaded")
    net.add_argument("--settle-timeout", type=float, default=15.0, help="best-effort networkidle budget")
    net.add_argument("--settle-ms", type=int, default=1500, help="fixed post-load settle")
    net.add_argument("--screenshot-timeout", type=float, default=60.0)
    net.add_argument("--user-agent", default=f"Driftwatch-dataset-miner/{SCRIPT_VERSION} (research; contact via repo)")

    qual = p.add_argument_group("render quality gates")
    qual.add_argument("--viewport-width", type=int, default=1440)
    qual.add_argument("--viewport-height", type=int, default=900)
    qual.add_argument("--min-text-chars", type=int, default=250, help="below this the render counts as empty")
    qual.add_argument("--error-page-max-chars", type=int, default=2500,
                      help="wayback error markers only count as fatal below this length")
    qual.add_argument("--max-broken-image-ratio", type=float, default=0.5,
                      help="reject a render when more than this share of <img> failed to load")
    qual.add_argument("--min-images-for-check", type=int, default=5,
                      help="skip the broken-image gate on pages with fewer images than this")
    qual.add_argument("--review-text-ratio", type=float, default=0.35,
                      help="flag (do not reject) pairs where the shorter side has less than this "
                           "share of the longer side's text")
    qual.add_argument("--no-quality-gate", action="store_true",
                      help="accept broken/unstyled renders (diagnostics only)")

    run = p.add_argument_group("run control")
    run.add_argument("--limit", type=int, default=None, help="process at most N domains")
    run.add_argument("--only", action="append", default=None, help="restrict to this domain (repeatable)")
    run.add_argument("--force", action="store_true", help="re-mine domains already marked ok")
    run.add_argument("--refresh-cdx", action="store_true", help="ignore the CDX cache")
    run.add_argument("--dry-run", action="store_true", help="CDX selection only, no browser")
    run.add_argument("--keep-renders", action="store_true", help="keep .renders scratch dir on success")
    run.add_argument("--headed", action="store_true", help="run Chromium headed (debugging)")
    return p


async def run(args: argparse.Namespace) -> int:
    if not args.domains.exists():
        print(f"domains file not found: {args.domains}", file=sys.stderr)
        return 2
    domains = read_domains(args.domains)
    if args.only:
        wanted = {slugify_domain(d) for d in args.only}
        domains = [d for d in domains if d in wanted]
    if args.limit:
        domains = domains[: args.limit]
    if not domains:
        print("no domains to process", file=sys.stderr)
        return 2

    pairs_root, cache_root = args.out, args.cdx_cache
    pairs_root.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)

    run_started = datetime.now(timezone.utc).isoformat()
    limiter = RateLimiter(args.rate)
    counts: dict[str, int] = {}
    total = len(domains)
    done = 0
    started_at = time.monotonic()

    print(
        f"driftwatch mine_wayback v{SCRIPT_VERSION} | {total} domains | "
        f"gap band [{args.min_gap_days}, {args.max_gap_days or 'inf'}]d | "
        f"rate {args.rate}s | concurrency {args.concurrency}"
        + (" | DRY RUN" if args.dry_run else ""),
        flush=True,
    )

    async with httpx.AsyncClient(
        headers={"User-Agent": args.user_agent}, follow_redirects=True
    ) as client:
        pw = browser = None
        try:
            if not args.dry_run:
                pw = await async_playwright().start()
                browser = await pw.chromium.launch(headless=not args.headed)

            sem = asyncio.Semaphore(max(1, args.concurrency))
            lock = asyncio.Lock()

            async def worker(domain: str) -> None:
                nonlocal done
                async with sem:
                    try:
                        outcome = await process_domain(
                            domain, client, browser, limiter, args, pairs_root, cache_root
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # last resort: never lose a domain
                        outcome = DomainOutcome(
                            domain, "unhandled_exception", f"{type(exc).__name__}: {str(exc)[:200]}"
                        )
                    async with lock:
                        done += 1
                        counts[outcome.status] = counts.get(outcome.status, 0) + 1
                        print(outcome.log_line(done, total), flush=True)
                        write_status(pairs_root, outcome, run_started)
                        append_log(args.log, run_started, outcome)

            try:
                await asyncio.gather(*(worker(d) for d in domains))
            except KeyboardInterrupt:
                print("\ninterrupted -- completed domains are on disk, rerun to resume", flush=True)
        finally:
            if browser:
                with contextlib.suppress(Exception):
                    await browser.close()
            if pw:
                with contextlib.suppress(Exception):
                    await pw.stop()

    elapsed = time.monotonic() - started_at
    usable = counts.get(OK, 0) + counts.get(SKIPPED_DONE, 0)
    print(f"\n--- {done}/{total} domains in {elapsed/60:.1f} min ---")
    for status, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {status}")
    print(f"\nusable pairs on disk: {usable}   log: {args.log}")
    if counts.get(OK) or counts.get(DRY_RUN_OK):
        print("next: eyeball 20 pairs before fixing the drift taxonomy.")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
