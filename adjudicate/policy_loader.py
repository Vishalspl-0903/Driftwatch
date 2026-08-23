"""adjudicate/policy_loader.py -- shared YAML policy loading.

Split out so structure_output.py (node 4, needs parse_failure_action) and
policy_adjudicate.py (node 5, needs rules/default_action) both read
adjudicate/policy.yaml without importing each other's internals. Node 4's
use of this is narrow and specific: it never rule-matches against evidence
(there is no evidence on a parse failure) -- it only reads the one fixed
fallback action, so that action name stays config-driven rather than a
hardcoded string buried in Python, consistent with every other action this
project hands out.
"""

from __future__ import annotations

from pathlib import Path

import yaml

POLICY_PATH = Path("adjudicate/policy.yaml")


def load_policy(path: Path = POLICY_PATH) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)
