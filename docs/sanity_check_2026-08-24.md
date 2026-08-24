# Driftwatch end-to-end sanity check — 2026-08-24

A full pipeline verification run: environment, mining, ground-truth/eval
reproduction, detector, agent (including a live Groq VLM call), console,
and repo hygiene (including a real fresh-clone test in a temp directory
and a fresh virtualenv). Screenshots from this run are in
[`verification_screenshots/`](verification_screenshots/).

| # | Section | Status | Notes |
|---|---|---|---|
| 1 | Environment | **PASS** | Python 3.12.0, Node v24.19.0/npm 11.17.0, git 2.55.0.3 all resolve cleanly on PATH in fresh shells (bash and PowerShell both checked). All required packages present (playwright, torch, transformers, sentence-transformers, bs4, lxml, langgraph, groq, python-dotenv, pyyaml, pydantic, numpy, pillow, httpx). Groq key valid, `qwen/qwen3.6-27b` still active — checked via free `models.list()`, no generation cost spent. |
| 2 | Mining pipeline | **PASS** | Re-mined healthkart.com with `--force`; `status=ok`, all 6 expected output files present and non-empty. |
| 3 | Ground truth & eval | **PASS** | `eyeball_notes.csv` has all 23 rows, correct axis distribution. `run_eval.py` reproduces the documented numbers **exactly**: precision 1.00, recall 0.67, n=10. |
| 4 | Detector | **PASS**, one flag | kreditbee.in and boat-lifestyle.com scores match exactly. healthkart.com shifted 0.3145→0.3029 — caused by the Section 2 re-mine itself (fresh screenshots, a different subset of images successfully fetched). Doesn't change routing (still well above the auto_flag threshold) but confirms image-drift scores aren't bit-exact across re-mines. |
| 5 | Agent | **PASS** | Routing correct for all 3 (`insufficient_data` / `auto_flag`-no-LLM / `escalated`). Live VLM reliability this session: **2 successes / 3 real attempts**, consistent with the documented ~60% pipeline-level rate (see `FAILURES.md`). One real (not simulated) failure was caught correctly by the safe-failure path — logged, routed to `needs_manual_review`, nothing lost. Latency ranged 5.25s–98.6s depending on whether the retry fired — genuinely variable, worth knowing for a live demo. Simulated-failure unit check also re-verified clean. |
| 6 | Console | **PASS** | Builds, installs, boots, renders 3 rows, sort/filter work, boat-lifestyle.com detail shows both evidence packets + disagreement callout + ground truth comparison, zero console errors. |
| 7 | Repo hygiene | **PASS**, one bug found+fixed | `.gitignore` verified correct via an actual fresh clone (none of the excluded dirs present). Base setup (`pip install -r requirements.txt`, `playwright install chromium`, a real `--dry-run` mine) verified working in a genuinely fresh venv. **Found and fixed**: `build_console_data.py` crashed with `TypeError` on a truly fresh clone (no `run_log.csv` yet → `None` formatted with a width spec). One-line fix, verified, committed, pushed (`d24761b`). |

## The one thing worth knowing before the pitch

If someone clones the repo fresh and jumps straight to the Console section
without first running the mining pipeline and the agent, `build_console_data.py`
now runs without crashing, but produces a console with incomplete data,
because that data only exists after real Wayback mining (minutes) and real
Groq API calls (real cost) have actually happened. That's not a bug — it's
inherent to "real data, no backend, no shortcuts" — but a from-scratch
reproduction attempted live, cold, would look broken even though the code
isn't. See README.md's "Reproducing the demo" section for how this is now
documented up front, rather than being a silent trap.

All work from this check is committed and pushed — `origin/main` was at
`bb884e3` when this report was written.
