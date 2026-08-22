"""drift/extract.py -- pull candidate product images and catalog/nav text out
of a mined t0/t1 render.

Uses only what mine_wayback.py already wrote to disk (dom.html, dom.txt).
No new network fetching and no JS execution here -- whatever survived the
render is what we've got. This module is pure DOM/text parsing; getting the
actual image bytes for embedding is drift/fetch.py's job.

Calibrated by hand against a few real captures (healthkart.com, caratlane.com,
libas.in, bata.in) -- see the constants below for what that showed:
  * Wayback rewrites <img src> to a fully-qualified web.archive.org replay
    URL at capture time, so no base-URL resolution is needed in the common
    case. data-src is kept as a fallback for lazy-load markup where src is
    still a placeholder.
  * Real catalogue imagery is raster (jpg/png/webp); every icon/logo/rating
    star/payment badge in spot checks was .svg. That single filter removes
    most chrome cheaply, before the name-marker and dimension checks even run.
  * dom.txt for an intact e-commerce render is dominated by short lines
    (nav categories, product titles) interleaved with price/CTA boilerplate
    ("MRP: Rs. 3900.0", "[Buy now]") that carries no category signal and is
    filtered explicitly rather than left to dilute the text centroid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Image candidate filtering
# ---------------------------------------------------------------------------
JUNK_NAME_MARKERS = (
    "logo", "icon", "favicon", "sprite", "badge", "social", "payment",
    "visa", "mastercard", "rupay", "paypal", "gpay", "phonepe",
    "whatsapp", "facebook", "instagram", "twitter", "youtube", "linkedin",
    "pixel", "spinner", "loader", "placeholder", "blank", "1x1",
    "arrow", "star", "rating", "flag", "close-icon", "search-icon",
    "cart-icon", "cartnew", "user-icon", "wishlist", "heart", "wm-",
    "wayback", "qr-code", "qrcode", "app-store", "play-store", "playstore",
    "appstore", "trust-badge", "trustbadge", "ssl", "secure-payment",
)

# Wayback rewrites <img> src to an absolute .../<ts>im_/<original> URL when
# the original response was actually an image; SVG icons/logos/decorations
# make up effectively all of the .svg hits seen in manual spot checks below.
JUNK_EXTENSIONS = (".svg",)

MIN_CANDIDATE_DIM = 60  # px; below this an explicit width/height is chrome
MAX_ASPECT_RATIO = 6.0  # very long/thin -> divider or banner strip, not a product
MAX_IMAGE_CANDIDATES = 40  # cap per side so a 400-image catalogue page doesn't
                            # blow the embedding/fetch budget; centroid quality
                            # saturates well before 40 well past this count anyway

# ---------------------------------------------------------------------------
# Catalog/nav text filtering
# ---------------------------------------------------------------------------
MIN_LINE_CHARS = 2
MAX_LINE_CHARS = 80
MAX_TEXT_LINES = 300

_PRICE_RE = re.compile(r"(?i)^(mrp|our price|price|rs\.?|inr|₹|\$)\s*[:\-]?\s*[\d,.]+\s*(/-)?$")
_BRACKET_CTA_RE = re.compile(r"^\[.+\]$")
_PURE_SYMBOLIC_RE = re.compile(r"^[\W\d_]+$")  # no letters at all -> phone numbers, dividers, dashes
_BOILERPLATE_RE = re.compile(
    r"(?i)\b(login|log in|sign in|sign up|my account|track order|customer care|"
    r"contact us|privacy polic|terms (of|&) (use|service)|copyright|all rights reserved|"
    r"subscribe|newsletter|follow us|download (the |our )?app|help ?desk|faq|"
    r"add to cart|buy now|shipping|returns? polic|cookie)\b"
)

# ---------------------------------------------------------------------------
# Thresholds for "did extraction find enough to trust the score"
# ---------------------------------------------------------------------------
MIN_IMAGES_FOR_TRUST = 3
MIN_TEXT_LINES_FOR_TRUST = 6


@dataclass
class ImageCandidate:
    url: str
    alt: str = ""


@dataclass
class ExtractResult:
    domain: str
    side: str
    image_candidates: list[ImageCandidate] = field(default_factory=list)
    catalog_text: list[str] = field(default_factory=list)
    dom_html_found: bool = False
    dom_txt_found: bool = False
    total_img_tags: int = 0
    total_text_lines: int = 0

    @property
    def images_usable(self) -> bool:
        return len(self.image_candidates) >= MIN_IMAGES_FOR_TRUST

    @property
    def text_usable(self) -> bool:
        return len(self.catalog_text) >= MIN_TEXT_LINES_FOR_TRUST


def _is_junk_image(src: str, alt: str) -> bool:
    lowered = f"{src} {alt}".lower()
    if any(marker in lowered for marker in JUNK_NAME_MARKERS):
        return True
    path = urlparse(src).path.lower()
    if path.endswith(JUNK_EXTENSIONS):
        return True
    return False


def _parse_dim(raw: str | None) -> int | None:
    if not raw:
        return None
    m = re.match(r"^\s*(\d+)", raw)
    return int(m.group(1)) if m else None


def _best_src(img, base_url: str) -> str | None:
    for attr in ("src", "data-src", "data-lazy-src", "data-original"):
        val = img.get(attr)
        if val and not val.startswith("data:"):
            return urljoin(base_url, val.strip())
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        if first and not first.startswith("data:"):
            return urljoin(base_url, first)
    return None


def extract_images(dom_html: str, base_url: str) -> tuple[list[ImageCandidate], int]:
    soup = BeautifulSoup(dom_html, "lxml")
    tags = soup.find_all("img")
    candidates: list[ImageCandidate] = []
    for img in tags:
        src = _best_src(img, base_url)
        if not src:
            continue
        alt = (img.get("alt") or "").strip()
        if _is_junk_image(src, alt):
            continue
        w = _parse_dim(img.get("width"))
        h = _parse_dim(img.get("height"))
        if w is not None and w < MIN_CANDIDATE_DIM:
            continue
        if h is not None and h < MIN_CANDIDATE_DIM:
            continue
        if w and h:
            ratio = max(w, h) / max(1, min(w, h))
            if ratio > MAX_ASPECT_RATIO:
                continue
        candidates.append(ImageCandidate(url=src, alt=alt))
        if len(candidates) >= MAX_IMAGE_CANDIDATES:
            break
    return candidates, len(tags)


def extract_catalog_text(dom_txt: str) -> tuple[list[str], int]:
    raw_lines = [ln.strip() for ln in dom_txt.splitlines()]
    raw_lines = [ln for ln in raw_lines if ln]
    kept: list[str] = []
    seen: set[str] = set()
    for ln in raw_lines:
        if not (MIN_LINE_CHARS <= len(ln) <= MAX_LINE_CHARS):
            continue
        if _PRICE_RE.match(ln):
            continue
        if _BRACKET_CTA_RE.match(ln):
            continue
        if _PURE_SYMBOLIC_RE.match(ln):
            continue
        if _BOILERPLATE_RE.search(ln):
            continue
        key = ln.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(ln)
        if len(kept) >= MAX_TEXT_LINES:
            break
    return kept, len(raw_lines)


def extract_side(domain: str, side: str, side_dir: Path) -> ExtractResult:
    result = ExtractResult(domain=domain, side=side)
    html_path = side_dir / "dom.html"
    txt_path = side_dir / "dom.txt"
    meta_path = side_dir / "meta.json"

    base_url = ""
    if meta_path.exists():
        import json

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            base_url = meta.get("this", {}).get("final_url", "") or meta.get("this", {}).get("replay_url", "")
        except (json.JSONDecodeError, OSError):
            pass

    if html_path.exists():
        result.dom_html_found = True
        html = html_path.read_text(encoding="utf-8", errors="replace")
        result.image_candidates, result.total_img_tags = extract_images(html, base_url)

    if txt_path.exists():
        result.dom_txt_found = True
        text = txt_path.read_text(encoding="utf-8", errors="replace")
        result.catalog_text, result.total_text_lines = extract_catalog_text(text)

    return result


def extract_pair(domain: str, pairs_root: Path) -> tuple[ExtractResult, ExtractResult]:
    domain_dir = pairs_root / domain
    t0 = extract_side(domain, "t0", domain_dir / "t0")
    t1 = extract_side(domain, "t1", domain_dir / "t1")
    return t0, t1


if __name__ == "__main__":
    import sys

    domain = sys.argv[1] if len(sys.argv) > 1 else "healthkart.com"
    t0, t1 = extract_pair(domain, Path("data/pairs"))
    for r in (t0, t1):
        print(
            f"{r.domain}/{r.side}: {len(r.image_candidates)}/{r.total_img_tags} image candidates "
            f"(usable={r.images_usable}), {len(r.catalog_text)}/{r.total_text_lines} text lines "
            f"(usable={r.text_usable})"
        )
        for c in r.image_candidates[:5]:
            print("   img:", c.alt[:60], "|", c.url[:100])
        for t in r.catalog_text[:10]:
            print("   txt:", t)
