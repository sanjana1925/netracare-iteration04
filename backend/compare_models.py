"""Compare LLM providers / models on the SAME red-flag cases (stretch: switch provider through .env, then compare).

The app itself never changes: the provider and model are read from the environment (LLM_PROVIDER, LLM_MODEL and the key
for that provider, normally from .env). This script just runs the real check_red_flags() once per candidate.

    python compare_models.py nvidia:nvidia/nemotron-3.5-lightning-30b-a3b nvidia:meta/llama-3.1-70b-instruct
    python compare_models.py            # compares the single model currently set in .env

Each candidate is  provider:model  (the model may be empty to use the provider default). The keys must be in .env.
Cases: the 35 rows of data2/reason_samples.csv plus the 10 hard cases of tests/hard_cases.csv (45 in all).
Add --pause 2 to wait between calls (free tiers rate-limit bursts). Nothing is written to the database.
"""
import argparse
import csv
import os
import statistics
import time
from pathlib import Path

from app import config, llm

HERE = Path(__file__).parent


def load_cases():
    cases = []
    for src, path in (("pack", config.DATA2_DIR / "reason_samples.csv"), ("hard", HERE / "tests" / "hard_cases.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                cases.append({"id": r.get("sample_id") or r["case_id"], "set": src, "reason": r["reason"],
                              "red": r["expected_red_flag"] == "yes", "category": r["category"]})
    return cases


def run(candidate: str, cases, pause: float):
    provider, _, model = candidate.partition(":")
    os.environ["LLM_PROVIDER"] = provider
    os.environ["LLM_MODEL"] = model
    out = {"candidate": candidate, "missed": [], "false_alarms": [], "wrong_category": [], "errors": [], "seconds": []}
    for c in cases:
        t0 = time.time()
        try:
            res = llm.check_red_flags(c["reason"])
        except llm.RedFlagCheckError as e:  # the app would refuse the booking: counted separately, never as a correct answer
            out["errors"].append(f"{c['id']}: {str(e)[:60]}")
            time.sleep(pause)
            continue
        out["seconds"].append(time.time() - t0)
        if c["red"] and not res.red_flag:
            out["missed"].append(c["id"])
        elif c["red"] and res.category != c["category"]:
            out["wrong_category"].append(f"{c['id']} (said {res.category})")
        elif not c["red"] and res.red_flag:
            out["false_alarms"].append(c["id"])
        time.sleep(pause)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates", nargs="*", help="provider:model ... (default: the one in .env)")
    ap.add_argument("--pause", type=float, default=1.0)
    args = ap.parse_args()
    candidates = args.candidates or [f"{os.environ.get('LLM_PROVIDER', 'openai')}:{os.environ.get('LLM_MODEL', '')}"]
    cases = load_cases()
    n_red = sum(c["red"] for c in cases)
    print(f"{len(cases)} cases: {n_red} red flags, {len(cases) - n_red} not red flags "
          f"({sum(c['set'] == 'pack' for c in cases)} from the data pack, {sum(c['set'] == 'hard' for c in cases)} hard cases)\n")
    rows = [run(c, cases, args.pause) for c in candidates]
    print(f"{'model':58} {'red flags caught':>16} {'category ok':>12} {'false alarms':>13} {'errors':>7} {'median s':>9}")
    for r in rows:
        answered_red = n_red - sum(1 for e in r["errors"] if any(e.startswith(c["id"] + ":") and c["red"] for c in cases))
        caught = answered_red - len(r["missed"])
        cat_ok = caught - len(r["wrong_category"])
        med = f"{statistics.median(r['seconds']):.1f}" if r["seconds"] else "-"
        print(f"{r['candidate'][:58]:58} {f'{caught}/{n_red}':>16} {f'{cat_ok}/{n_red}':>12} {len(r['false_alarms']):>13} {len(r['errors']):>7} {med:>9}")
    print()
    for r in rows:
        print(r["candidate"])
        for k in ("missed", "wrong_category", "false_alarms", "errors"):
            print(f"   {k:15}", r[k] or "none")


if __name__ == "__main__":
    main()
