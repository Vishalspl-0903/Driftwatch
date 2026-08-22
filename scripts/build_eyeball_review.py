#!/usr/bin/env python3
"""
build_eyeball_review.py — generate a static HTML side-by-side viewer for
manually judging t0/t1 storefront pairs mined by mine_wayback.py.

This tool does not judge anything. It lays out what a human needs to judge
quickly: both screenshots, the gap, and whatever quality-gate flags meta.json
already carries. For each pair it also renders the copy-pasteable CSV row
template (matching evals/eyeball_notes.csv's header) so filling that file in
by hand is a copy-paste, not a retype.

Usage:
  python scripts/build_eyeball_review.py
  python scripts/build_eyeball_review.py --out evals/eyeball_review.html
"""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path

FITS_AXES = ["category", "content", "trust", "structural", "none"]
CONFIDENCES = ["high", "medium", "low"]


def load_pair(domain_dir: Path) -> dict | None:
    status_path = domain_dir / "status.json"
    t0_meta_path = domain_dir / "t0" / "meta.json"
    t1_meta_path = domain_dir / "t1" / "meta.json"
    t0_shot = domain_dir / "t0" / "screenshot.png"
    t1_shot = domain_dir / "t1" / "screenshot.png"
    if not (status_path.exists() and t0_meta_path.exists() and t1_shot.exists() and t0_shot.exists()):
        return None
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        t0_meta = json.loads(t0_meta_path.read_text(encoding="utf-8"))
        t1_meta = json.loads(t1_meta_path.read_text(encoding="utf-8")) if t1_meta_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        return None
    if status.get("status") != "ok":
        return None

    return {
        "domain": domain_dir.name,
        "t0_date": (t0_meta.get("this", {}).get("iso") or "")[:10],
        "t1_date": (t1_meta.get("this", {}).get("iso") or t0_meta.get("t1_iso") or "")[:10],
        "gap_days": t0_meta.get("gap_days"),
        "needs_manual_review": t0_meta.get("needs_manual_review"),
        "text_char_ratio": t0_meta.get("text_char_ratio"),
        "usable_image_ratio": t0_meta.get("usable_image_ratio"),
        "identical_rendered_text": t0_meta.get("identical_rendered_text"),
        "t0_broken_image_ratio": t0_meta.get("this", {}).get("quality", {}).get("broken_image_ratio"),
        "t1_broken_image_ratio": t1_meta.get("this", {}).get("quality", {}).get("broken_image_ratio"),
        "t0_shot": t0_shot,
        "t1_shot": t1_shot,
    }


def fmt(v, suffix: str = "") -> str:
    if v is None:
        return "?"
    if isinstance(v, float):
        return f"{v:.3f}{suffix}"
    return f"{v}{suffix}"


def render_pair(p: dict, out_dir: Path, idx: int, total: int) -> str:
    domain = html.escape(p["domain"])
    dom_id = html.escape(p["domain"].replace(".", "_").replace("-", "_"))
    t0_rel = html.escape(Path(os.path.relpath(p["t0_shot"], out_dir)).as_posix())
    t1_rel = html.escape(Path(os.path.relpath(p["t1_shot"], out_dir)).as_posix())

    flags = []
    if p["needs_manual_review"]:
        flags.append(
            f'<span class="flag review">QUALITY GATE: needs_manual_review '
            f'(text_ratio={fmt(p["text_char_ratio"])}, image_ratio={fmt(p["usable_image_ratio"])})</span>'
        )
    if p["identical_rendered_text"]:
        flags.append('<span class="flag control">IDENTICAL TEXT (control candidate)</span>')
    if isinstance(p["t0_broken_image_ratio"], (int, float)) and p["t0_broken_image_ratio"] > 0.3:
        flags.append(f'<span class="flag warn">t0 broken images {fmt(p["t0_broken_image_ratio"], "")}</span>')
    if isinstance(p["t1_broken_image_ratio"], (int, float)) and p["t1_broken_image_ratio"] > 0.3:
        flags.append(f'<span class="flag warn">t1 broken images {fmt(p["t1_broken_image_ratio"], "")}</span>')
    flags_html = "\n      ".join(flags) if flags else '<span class="flag ok">no quality-gate flags</span>'

    axis_options = "\n".join(f'<option value="{a}">{a}</option>' for a in FITS_AXES)
    conf_options = "\n".join(f'<option value="{c}">{c}</option>' for c in CONFIDENCES)

    return f"""
  <section class="pair" id="p-{dom_id}">
    <div class="pair-head">
      <h2>{idx}/{total} &nbsp; {domain}</h2>
      <div class="meta-row">
        <span>t0 {html.escape(p["t0_date"])} &rarr; t1 {html.escape(p["t1_date"])}</span>
        <span>gap {fmt(p["gap_days"], "d")}</span>
      </div>
      <div class="flags">
      {flags_html}
      </div>
    </div>
    <div class="screenshots">
      <div class="shot">
        <h3>t0 &mdash; {html.escape(p["t0_date"])} <a href="{t0_rel}" target="_blank">open full size</a></h3>
        <div class="shot-frame"><img loading="lazy" src="{t0_rel}" alt="{domain} t0"></div>
      </div>
      <div class="shot">
        <h3>t1 &mdash; {html.escape(p["t1_date"])} <a href="{t1_rel}" target="_blank">open full size</a></h3>
        <div class="shot-frame"><img loading="lazy" src="{t1_rel}" alt="{domain} t1"></div>
      </div>
    </div>
    <div class="review-form">
      <label>what_changed
        <textarea id="wc-{dom_id}" rows="2" placeholder="what actually changed between t0 and t1"
          oninput="updateRow('{dom_id}')"></textarea>
      </label>
      <div class="form-row">
        <label>fits_axis
          <select id="axis-{dom_id}" onchange="updateRow('{dom_id}')">
            <option value="">--</option>
            {axis_options}
          </select>
        </label>
        <label>confidence
          <select id="conf-{dom_id}" onchange="updateRow('{dom_id}')">
            <option value="">--</option>
            {conf_options}
          </select>
        </label>
      </div>
      <label>notes
        <textarea id="notes-{dom_id}" rows="2" placeholder="anything else worth remembering"
          oninput="updateRow('{dom_id}')"></textarea>
      </label>
      <div class="csv-row">
        <code id="csv-{dom_id}" data-domain="{domain}" data-t0="{html.escape(p["t0_date"])}"
          data-t1="{html.escape(p["t1_date"])}" data-gap="{fmt(p["gap_days"])}">{domain},{html.escape(p["t0_date"])},{html.escape(p["t1_date"])},{fmt(p["gap_days"])},,,,</code>
        <button onclick="copyRow('{dom_id}')">Copy row</button>
      </div>
    </div>
  </section>
"""


PAGE_CSS = """
:root {
  --bg: #0f1115; --panel: #171a21; --border: #2a2f3a; --text: #e6e8ec;
  --muted: #8b93a3; --accent: #5b9dff; --ok: #3ecf8e; --warn: #f0a84e; --review: #ef6a6a;
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, Segoe UI, Roboto, sans-serif;
  margin: 0; padding: 0 0 4rem; }
header.topbar { position: sticky; top: 0; z-index: 10; background: var(--panel); border-bottom: 1px solid var(--border);
  padding: 0.75rem 1.5rem; display: flex; justify-content: space-between; align-items: center; gap: 1rem; flex-wrap: wrap; }
header.topbar h1 { font-size: 1rem; margin: 0; font-weight: 600; }
header.topbar p { margin: 0; color: var(--muted); font-size: 0.85rem; }
button { background: var(--accent); color: #fff; border: none; border-radius: 6px; padding: 0.4rem 0.8rem;
  font-size: 0.85rem; cursor: pointer; }
button:hover { filter: brightness(1.1); }
main { max-width: 1400px; margin: 0 auto; padding: 1.5rem; }
section.pair { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 1.25rem; margin-bottom: 2rem; }
.pair-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 1rem; margin-bottom: 0.5rem; }
.pair-head h2 { font-size: 1.1rem; margin: 0; }
.meta-row { color: var(--muted); font-size: 0.85rem; display: flex; gap: 1rem; }
.flags { margin: 0.4rem 0 1rem; display: flex; gap: 0.5rem; flex-wrap: wrap; }
.flag { font-size: 0.75rem; padding: 0.2rem 0.5rem; border-radius: 999px; border: 1px solid var(--border); }
.flag.review { color: var(--review); border-color: var(--review); }
.flag.warn { color: var(--warn); border-color: var(--warn); }
.flag.ok { color: var(--ok); border-color: var(--ok); }
.flag.control { color: var(--accent); border-color: var(--accent); }
.screenshots { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.shot h3 { font-size: 0.8rem; font-weight: 500; color: var(--muted); margin: 0 0 0.4rem; }
.shot h3 a { color: var(--accent); margin-left: 0.5rem; font-size: 0.75rem; }
.shot-frame { max-height: 640px; overflow-y: auto; border: 1px solid var(--border); border-radius: 6px;
  background: #000; }
.shot-frame img { width: 100%; display: block; }
.review-form { margin-top: 1rem; border-top: 1px dashed var(--border); padding-top: 1rem; }
.review-form label { display: block; font-size: 0.8rem; color: var(--muted); margin-bottom: 0.6rem; }
.review-form textarea, .review-form select { width: 100%; margin-top: 0.25rem; background: #0d0f13;
  color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 0.4rem; font-family: inherit;
  font-size: 0.85rem; }
.form-row { display: flex; gap: 1rem; }
.form-row label { flex: 1; }
.csv-row { display: flex; gap: 0.6rem; align-items: center; margin-top: 0.5rem; }
.csv-row code { flex: 1; background: #0d0f13; border: 1px solid var(--border); border-radius: 6px;
  padding: 0.5rem 0.7rem; font-size: 0.8rem; overflow-x: auto; white-space: pre; }
@media (max-width: 900px) { .screenshots { grid-template-columns: 1fr; } .form-row { flex-direction: column; } }
"""

PAGE_JS = """
function csvField(v) {
  v = v == null ? '' : String(v);
  if (/[",\\n]/.test(v)) { return '"' + v.replace(/"/g, '""') + '"'; }
  return v;
}
function updateRow(id) {
  const code = document.getElementById('csv-' + id);
  const wc = document.getElementById('wc-' + id).value;
  const axis = document.getElementById('axis-' + id).value;
  const conf = document.getElementById('conf-' + id).value;
  const notes = document.getElementById('notes-' + id).value;
  const row = [code.dataset.domain, code.dataset.t0, code.dataset.t1, code.dataset.gap, wc, axis, conf, notes]
    .map(csvField).join(',');
  code.textContent = row;
}
function copyText(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand('copy'); } catch (e) {}
  document.body.removeChild(ta);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => {});
  }
}
function copyRow(id) {
  copyText(document.getElementById('csv-' + id).textContent);
}
function copyAllRows() {
  const rows = Array.from(document.querySelectorAll('.csv-row code')).map(c => c.textContent);
  copyText(rows.join('\\n'));
  const btn = document.getElementById('copy-all-btn');
  const orig = btn.textContent;
  btn.textContent = 'Copied ' + rows.length + ' rows!';
  setTimeout(() => { btn.textContent = orig; }, 1500);
}
"""


def build(pairs_root: Path, out_path: Path) -> int:
    pairs = []
    for d in sorted(pairs_root.iterdir()):
        if not d.is_dir():
            continue
        p = load_pair(d)
        if p:
            pairs.append(p)

    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    body_sections = "\n".join(
        render_pair(p, out_dir, i, len(pairs)) for i, p in enumerate(pairs, start=1)
    )

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Driftwatch eyeball review</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<header class="topbar">
  <div>
    <h1>Driftwatch eyeball review &mdash; {len(pairs)} pairs</h1>
    <p>Fill a row, hit Copy row (or Copy all at the end), paste into evals/eyeball_notes.csv.</p>
  </div>
  <button id="copy-all-btn" onclick="copyAllRows()">Copy all filled rows</button>
</header>
<main>
{body_sections}
</main>
<script>{PAGE_JS}</script>
</body>
</html>
"""
    out_path.write_text(html_doc, encoding="utf-8")
    return len(pairs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", type=Path, default=Path("data/pairs"), help="root of mined pairs")
    ap.add_argument("--out", type=Path, default=Path("evals/eyeball_review.html"), help="output HTML file")
    args = ap.parse_args()

    if not args.pairs.exists():
        print(f"pairs root not found: {args.pairs}")
        return 2
    n = build(args.pairs, args.out)
    print(f"wrote {args.out} with {n} pair(s)")
    if n == 0:
        print("no completed (status=ok) pairs found under", args.pairs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
