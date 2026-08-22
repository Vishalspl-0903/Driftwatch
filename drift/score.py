"""drift/score.py -- t0 vs t1 catalog drift score for one domain pair.

Two independent signals, reported separately and never fused into one number:
  * image_drift_score: cosine distance between the t0 and t1 CLIP embedding
    centroids of extracted, successfully-fetched product images.
  * text_drift_score: cosine distance between the t0 and t1 BGE embedding
    centroids of extracted catalog/nav text.

Higher = more drift. Each score is None when its side didn't clear the
usability bar -- a None is a refusal to guess, not a zero.

Usability has two layers, both refusals rather than best-effort guesses:
  * extract.py's own gate rejects a side outright when mine_wayback.py's
    render-quality probe (broken_image_ratio) came back too high, regardless
    of how many candidates were extracted -- a broken render can still yield
    a handful of technically-fetchable images or technically-long-enough
    text (see extract.py's docstring for the real examples that motivated
    this).
  * On top of that, image usability here additionally requires enough
    candidates to actually resolve to bytes post-fetch: extract.py finds
    URLs, but a meaningful share routinely fail to fetch even when the
    render wasn't broken enough to trip the first gate. A score built on 2
    out of 3 requested images that happened to load is not trustworthy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from drift.embed import centroid, cosine_distance, embed_images, embed_texts
from drift.extract import ExtractResult, extract_pair
from drift.fetch import fetch_images

MIN_FETCHED_IMAGES_FOR_TRUST = 3


@dataclass
class SideDiagnostics:
    side: str
    dom_image_candidates: int = 0
    fetched_images: int = 0
    fetch_failures: int = 0
    text_lines: int = 0
    render_broken_image_ratio: float | None = None  # from mine_wayback.py's meta.json, for cross-reference
    images_usable: bool = False
    text_usable: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class PairScore:
    domain: str
    image_drift_score: float | None
    text_drift_score: float | None
    t0: SideDiagnostics
    t1: SideDiagnostics

    @property
    def image_pair_usable(self) -> bool:
        return self.t0.images_usable and self.t1.images_usable

    @property
    def text_pair_usable(self) -> bool:
        return self.t0.text_usable and self.t1.text_usable

    @property
    def any_usable(self) -> bool:
        return self.image_pair_usable or self.text_pair_usable


def _side_images(extract_result: ExtractResult, cache_dir: Path) -> tuple[np.ndarray, SideDiagnostics]:
    diag = SideDiagnostics(side=extract_result.side, dom_image_candidates=len(extract_result.image_candidates))
    diag.text_lines = len(extract_result.catalog_text)
    # text_usable already folds in the broken_image_ratio gate (drift/extract.py) --
    # not recomputed here, so the two modules can't silently disagree on it.
    diag.text_usable = extract_result.text_usable
    diag.render_broken_image_ratio = extract_result.render_broken_image_ratio
    diag.notes.extend(extract_result.notes)

    if extract_result.render_too_broken:
        # Already logged via extract_result.notes above; refused outright per
        # the broken_image_ratio gate, regardless of candidate count -- no
        # point spending a fetch on a side we're not going to score.
        return np.zeros((0, 512), dtype=np.float32), diag

    urls = [c.url for c in extract_result.image_candidates]
    if not urls:
        diag.notes.append("no image candidates survived DOM extraction")
        return np.zeros((0, 512), dtype=np.float32), diag

    fetched = fetch_images(urls, cache_dir=cache_dir)
    ok_bytes = [r.content for r in fetched if r.ok and r.content is not None]
    diag.fetched_images = len(ok_bytes)
    diag.fetch_failures = len(fetched) - len(ok_bytes)
    diag.images_usable = diag.fetched_images >= MIN_FETCHED_IMAGES_FOR_TRUST
    if not diag.images_usable:
        diag.notes.append(
            f"only {diag.fetched_images}/{len(urls)} candidates fetched successfully "
            f"(< {MIN_FETCHED_IMAGES_FOR_TRUST} needed)"
        )
    vecs = embed_images(ok_bytes) if ok_bytes else np.zeros((0, 512), dtype=np.float32)
    return vecs, diag


def score_pair(
    domain: str,
    pairs_root: Path = Path("data/pairs"),
    cache_dir: Path = Path("data/drift_image_cache"),
) -> PairScore:
    t0_extract, t1_extract = extract_pair(domain, pairs_root)

    t0_img_vecs, t0_diag = _side_images(t0_extract, cache_dir)
    t1_img_vecs, t1_diag = _side_images(t1_extract, cache_dir)

    image_score: float | None = None
    if t0_diag.images_usable and t1_diag.images_usable:
        c0, c1 = centroid(t0_img_vecs), centroid(t1_img_vecs)
        if c0 is not None and c1 is not None:
            image_score = round(cosine_distance(c0, c1), 4)

    text_score: float | None = None
    if t0_diag.text_usable and t1_diag.text_usable:
        t0_txt_vecs = embed_texts(t0_extract.catalog_text)
        t1_txt_vecs = embed_texts(t1_extract.catalog_text)
        c0, c1 = centroid(t0_txt_vecs), centroid(t1_txt_vecs)
        if c0 is not None and c1 is not None:
            text_score = round(cosine_distance(c0, c1), 4)

    return PairScore(
        domain=domain,
        image_drift_score=image_score,
        text_drift_score=text_score,
        t0=t0_diag,
        t1=t1_diag,
    )


if __name__ == "__main__":
    import sys

    domain = sys.argv[1] if len(sys.argv) > 1 else "healthkart.com"
    result = score_pair(domain)
    print(f"{domain}: image_drift={result.image_drift_score} text_drift={result.text_drift_score}")
    for side in (result.t0, result.t1):
        print(
            f"  {side.side}: images {side.fetched_images} fetched / {side.dom_image_candidates} candidates "
            f"(usable={side.images_usable}), text {side.text_lines} lines (usable={side.text_usable}), "
            f"render_broken_image_ratio={side.render_broken_image_ratio}"
        )
        for n in side.notes:
            print("    note:", n)
