"""STRETCH: runs check_red_flags on all 35 rows of data2/reason_samples.csv with the REAL model from .env.

Skipped automatically when no API key is configured. Run it with a key to measure your prompt:
    python -m pytest tests/test_reason_samples_live.py -v

Rules: a MISSED red flag fails the test (never acceptable). A false alarm is allowed (it costs Staff one click) but is
reported at the end so you can tune the prompt. The category must match for red-flag rows.
"""
import csv

import pytest

from app import llm
from helpers import DATA2

with open(DATA2 / "reason_samples.csv", newline="", encoding="utf-8") as f:
    SAMPLES = list(csv.DictReader(f))

pytestmark = pytest.mark.skipif(not llm.llm_configured(), reason="No LLM API key in .env: live red-flag tests skipped")
_false_alarms: list = []



def test_there_are_35_samples():
    assert len(SAMPLES) == 35 and sum(s["expected_red_flag"] == "yes" for s in SAMPLES) == 20


@pytest.mark.parametrize("s", SAMPLES, ids=[s["sample_id"] for s in SAMPLES])
def test_sample(s):
    res = llm.check_red_flags(s["reason"])
    if s["expected_red_flag"] == "yes":
        assert res.red_flag, f"MISSED RED FLAG {s['sample_id']}: {s['reason']!r} -> {res}"
        assert res.category == s["category"], f"{s['sample_id']}: wrong category {res.category}, expected {s['category']}"
    elif res.red_flag:
        _false_alarms.append(s["sample_id"])  # safe, but worth tuning


def test_report_false_alarms():
    print(f"\nFalse alarms (safe, but tune the prompt): {_false_alarms or 'none'}")
