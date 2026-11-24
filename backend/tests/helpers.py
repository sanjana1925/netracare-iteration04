"""Shared test helpers: where the data pack is, the fixed clock, and the three login helpers."""
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"     # data pack 1
DATA2 = Path(__file__).resolve().parent.parent / "data2"   # data pack 2 (past visits, profiles, reason samples)
DATA3 = Path(__file__).resolve().parent.parent / "data3"   # data pack 3 (clinic documents, questions, routing samples)
DATA4 = Path(__file__).resolve().parent.parent / "data4"   # data pack 4 (updated policy document, agent and leave scenarios)
CLINIC_NOW = "2026-11-02T10:00"                          # a Monday; the fixed "today" given in the data pack 1 README


def login_patient(client, p):
    r = client.post("/login", json={"role": "patient", "mobile": p["mobile"], "name": p["name"]})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def login_staff(client):
    r = client.post("/login", json={"role": "staff", "name": "Front Desk"})  # Staff log in with any name (no staff list in the pack)
    return {"Authorization": "Bearer " + r.json()["token"]}


def login_doctor(client, doctor_id):
    r = client.post("/login", json={"role": "doctor", "doctor_id": doctor_id})
    return {"Authorization": "Bearer " + r.json()["token"]}
