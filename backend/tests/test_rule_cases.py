"""Runs ALL 38 cases of stretch/rule_tests.csv through the real HTTP API (fresh seed database each time)."""
import csv

import pytest

from helpers import DATA, login_patient, login_staff

with open(DATA / "stretch" / "rule_tests.csv", newline="", encoding="utf-8") as f:
    CASES = list(csv.DictReader(f))


def _status(client, appt_id):
    rows = client.get("/appointments", headers=login_staff(client)).json()
    return next(r["status"] for r in rows if r["appointment_id"] == appt_id)


def test_all_38_cases_are_loaded():
    assert len(CASES) == 38


@pytest.mark.parametrize("c", CASES, ids=[c["test_id"] for c in CASES])
def test_case(client, patients, c):
    headers = login_staff(client) if c["role"] == "staff" else login_patient(client, patients[c["acting_patient_id"]])
    body = {"doctor_id": c["doctor_id"], "speciality": c["speciality"], "visit_type": c["visit_type"],
            "date": c["date"], "start_time": c["start_time"], "reason": "Routine eye check"}

    if c["action"] == "book":
        if c["role"] == "staff":
            body["patient_id"] = c["acting_patient_id"]  # staff books for the walk-in
        r = client.post("/appointments", json=body, headers=headers)
    elif c["action"] == "cancel":
        r = client.post(f"/appointments/{c['appointment_id']}/cancel", headers=headers)
    else:
        r = client.post(f"/appointments/{c['appointment_id']}/reschedule", json=body, headers=headers)

    if c["expected"] == "allowed":
        assert r.status_code in (200, 201), f"{c['test_id']} {c['note']}: {r.text}"
        if c["action"] == "book":
            assert r.json()["status"] == "booked"
            assert r.json()["end_time"] > r.json()["start_time"]
        elif c["action"] == "cancel":
            assert _status(client, c["appointment_id"]) == "cancelled"
        else:
            assert _status(client, c["appointment_id"]) == "cancelled"
            assert r.json()["status"] == "booked"
    else:
        detail = r.json()["detail"]
        assert r.status_code >= 400, c["note"]
        assert detail["code"] == c["reason_code"], f"{c['test_id']} {c['note']}: got {detail}"
        assert detail["to_staff"] == (c["expected"] == "to_staff")
        assert detail["message"]  # a plain-words message always comes back
        if c["action"] in ("cancel", "reschedule") and c["reason_code"] != "NOT_ACTIVE":
            assert _status(client, c["appointment_id"]) == "booked"  # a refusal leaves the original untouched
