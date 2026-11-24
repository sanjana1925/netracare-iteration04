"""STRETCH: 10 hard cases of my own (Hindi, Hinglish, typos, ambiguous wording, a long message), run with the REAL model from .env.

The cases live in tests/hard_cases.csv (the data pack files are left exactly as they were). Skipped when no key is configured.
Rules are the same as for reason_samples.csv: a MISSED red flag fails the test; a false alarm is safe (one click for Staff) but is
reported; the category must match for red-flag rows.
"""
import csv
from pathlib import Path

import pytest

from app import llm

with open(Path(__file__).parent / "hard_cases.csv", newline="", encoding="utf-8") as f:
    CASES = list(csv.DictReader(f))

pytestmark = pytest.mark.skipif(not llm.llm_configured(), reason="No LLM API key in .env: live hard-case tests skipped")
_false_alarms: list = []



@pytest.mark.parametrize("c", CASES, ids=[c["case_id"] + "-" + c["kind"] for c in CASES])
def test_hard_case(c):
    res = llm.check_red_flags(c["reason"])
    if c["expected_red_flag"] == "yes":
        assert res.red_flag, f"MISSED RED FLAG {c['case_id']}: {c['reason']!r} -> {res}"
        assert res.category == c["category"], f"{c['case_id']}: wrong category {res.category}, expected {c['category']}"
    elif res.red_flag:
        _false_alarms.append(c["case_id"])


def test_report_false_alarms():
    print(f"\nHard-case false alarms (safe, but worth tuning): {_false_alarms or 'none'}")
