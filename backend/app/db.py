"""SQLite schema, connection helper and CSV loaders (R1 of iterations 1 and 2)."""
import csv
import os
import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS doctors (
  doctor_id TEXT PRIMARY KEY, name TEXT NOT NULL, specialities TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS weekly_schedule (
  doctor_id TEXT NOT NULL REFERENCES doctors(doctor_id),
  day TEXT NOT NULL, session TEXT NOT NULL, status TEXT NOT NULL,
  PRIMARY KEY (doctor_id, day, session));
CREATE TABLE IF NOT EXISTS holidays (date TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS patients (
  patient_id TEXT PRIMARY KEY, name TEXT NOT NULL,
  mobile TEXT NOT NULL UNIQUE, year_of_birth INTEGER,
  general_health TEXT, allergies TEXT);              -- iteration 2: profile
CREATE TABLE IF NOT EXISTS appointments (
  appointment_id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(patient_id),
  doctor_id TEXT NOT NULL REFERENCES doctors(doctor_id),
  speciality TEXT NOT NULL, visit_type TEXT NOT NULL,
  date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('booked','cancelled','completed','proposed')),  -- iteration 4: 'proposed' holds a slot after doctor leave
  
  reason TEXT,                                        -- iteration 2: why the patient is coming
  doctor_note TEXT,                                   -- iteration 2: written when completed
  urgent INTEGER NOT NULL DEFAULT 0,                  -- iteration 2: booked after a red-flag reason
  moved_note TEXT);                                   -- iteration 4: 'Moved because Dr. X is on leave'
CREATE INDEX IF NOT EXISTS idx_appt_doctor_date ON appointments(doctor_id, date);
CREATE INDEX IF NOT EXISTS idx_appt_patient ON appointments(patient_id);
CREATE TABLE IF NOT EXISTS approvals (              -- stretch goal: Staff approval queue for a patient's late cancel / reschedule
  approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
  appointment_id TEXT NOT NULL REFERENCES appointments(appointment_id),
  patient_id TEXT NOT NULL REFERENCES patients(patient_id),
  action TEXT NOT NULL CHECK (action IN ('cancel','reschedule')),
  new_doctor_id TEXT, new_speciality TEXT, new_visit_type TEXT, new_date TEXT, new_start_time TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  created_at TEXT NOT NULL, decided_at TEXT, decided_by TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, role TEXT NOT NULL, patient_id TEXT, doctor_id TEXT, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS urgent_cases (
  urgent_id INTEGER PRIMARY KEY AUTOINCREMENT,
  patient_id TEXT NOT NULL REFERENCES patients(patient_id),
  reason TEXT NOT NULL, category TEXT NOT NULL, why TEXT NOT NULL,
  created_at TEXT NOT NULL,
  seen INTEGER NOT NULL DEFAULT 0, seen_at TEXT, seen_by TEXT,
  source TEXT NOT NULL DEFAULT 'booking');            -- iteration 3: 'booking' or 'chat'
CREATE TABLE IF NOT EXISTS leaves (                 -- iteration 4 (R7): a doctor's leave
  leave_id INTEGER PRIMARY KEY AUTOINCREMENT,
  doctor_id TEXT NOT NULL REFERENCES doctors(doctor_id),
  date TEXT NOT NULL, session TEXT NOT NULL CHECK (session IN ('AM','PM','FULL')),
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rebookings (             -- iteration 4 (R7): one proposal per appointment hit by leave
  rebooking_id INTEGER PRIMARY KEY AUTOINCREMENT,
  leave_id INTEGER NOT NULL REFERENCES leaves(leave_id),
  original_appointment_id TEXT NOT NULL REFERENCES appointments(appointment_id),
  proposed_appointment_id TEXT REFERENCES appointments(appointment_id),
  patient_id TEXT NOT NULL REFERENCES patients(patient_id),
  status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','no_slot')),
  created_at TEXT NOT NULL, decided_at TEXT, decided_by TEXT);
CREATE TABLE IF NOT EXISTS patient_facts (            -- stretch: the Fact of the Day saved for each patient, so login is instant
  patient_id TEXT PRIMARY KEY REFERENCES patients(patient_id),
  fact TEXT NOT NULL, created_at TEXT NOT NULL);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.db_path(), isolation_level=None, timeout=10,
                           check_same_thread=False)  # manual transactions. FastAPI may create the per-request connection in one worker thread and use it in another; each request has its OWN connection and uses it one step at a time, so this is safe
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _rows(path: Path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def init_db(with_seed_appointments: bool = False, with_past_visits: bool | None = None) -> None:
    """Create tables and load the data packs if the database is empty. Safe to call on every startup.

    Data pack 1 is always loaded. Data pack 2 (profiles + 27 past visits) is loaded too, unless
    with_past_visits=False (the iteration 1 rule tests use the stretch seed instead, so they stay unchanged).
    NETRACARE_SKIP_PACK2=1 does the same from the environment.
    """
    if with_past_visits is None:
        with_past_visits = os.environ.get("NETRACARE_SKIP_PACK2") != "1"
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        if "source" not in [r[1] for r in conn.execute("PRAGMA table_info(urgent_cases)")]:  # a database made by iteration 2: add the new column
            conn.execute("ALTER TABLE urgent_cases ADD COLUMN source TEXT NOT NULL DEFAULT 'booking'")
        if "moved_note" not in [r[1] for r in conn.execute("PRAGMA table_info(appointments)")]:  # a database made by iteration 3
            conn.execute("ALTER TABLE appointments ADD COLUMN moved_note TEXT")
        d1, d2 = config.DATA_DIR, config.DATA2_DIR
        if conn.execute("SELECT COUNT(*) FROM doctors").fetchone()[0] == 0:
            conn.execute("BEGIN")
            conn.executemany("INSERT INTO doctors VALUES (:doctor_id,:name,:specialities)", _rows(d1 / "doctors.csv"))
            conn.executemany("INSERT INTO weekly_schedule VALUES (:doctor_id,:day,:session,:status)", _rows(d1 / "weekly_schedule.csv"))
            conn.executemany("INSERT INTO holidays VALUES (:date,:name)", _rows(d1 / "holidays.csv"))
            conn.executemany("INSERT INTO patients (patient_id,name,mobile,year_of_birth) VALUES (:patient_id,:name,:mobile,:year_of_birth)",
                             _rows(d1 / "patients.csv"))
            if with_past_visits:
                conn.executemany("UPDATE patients SET general_health=:general_health, allergies=:allergies WHERE patient_id=:patient_id",
                                 _rows(d2 / "patient_profiles.csv"))
                conn.executemany(
                    "INSERT INTO appointments (appointment_id,patient_id,doctor_id,speciality,visit_type,date,start_time,end_time,"
                    "status,reason,doctor_note,urgent) VALUES (:visit_id,:patient_id,:doctor_id,:speciality,:visit_type,:date,"
                    ":start_time,:end_time,'completed',:reason,:doctor_note,0)", _rows(d2 / "past_visits.csv"))
            conn.execute("COMMIT")
        if with_seed_appointments and conn.execute("SELECT COUNT(*) FROM appointments WHERE appointment_id LIKE 'A%'").fetchone()[0] == 0:
            conn.execute("BEGIN")
            conn.executemany(
                "INSERT INTO appointments (appointment_id,patient_id,doctor_id,speciality,visit_type,date,start_time,end_time,status) "
                "VALUES (:appointment_id,:patient_id,:doctor_id,:speciality,:visit_type,:date,:start_time,:end_time,:status)",
                _rows(d1 / "stretch" / "appointments_seed.csv"))
            conn.execute("COMMIT")
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("Database ready at", config.db_path())
