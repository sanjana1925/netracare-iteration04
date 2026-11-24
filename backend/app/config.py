"""Configuration. Everything that tests or demos may need to change comes from environment variables."""
import os
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("NETRACARE_DATA_DIR", BASE_DIR / "data"))      # data pack 1
DATA2_DIR = Path(os.environ.get("NETRACARE_DATA2_DIR", BASE_DIR / "data2"))   # data pack 2
DATA3_DIR = Path(os.environ.get("NETRACARE_DATA3_DIR", BASE_DIR / "data3"))   # data pack 3 (clinic documents + samples)
DATA4_DIR = Path(os.environ.get("NETRACARE_DATA4_DIR", BASE_DIR / "data4"))   # data pack 4 (updated policy document + scenarios)


def document_paths() -> list[Path]:
    """Every clinic document the chat answers from. Data pack 4's updated policy document REPLACES the data pack 3 copy of the
    same name, as its README says; the packs themselves are never edited. NETRACARE_DOCS_DIR (tests) overrides both."""
    override = os.environ.get("NETRACARE_DOCS_DIR")
    if override:
        return sorted(Path(override).glob("*.md"))
    found = {p.name: p for p in (DATA3_DIR / "documents").glob("*.md")}
    found.update({p.name: p for p in (DATA4_DIR / "documents").glob("*.md")})
    return [found[name] for name in sorted(found)]


def docs_dir() -> Path:
    """The clinic documents the chat answers from."""
    return Path(os.environ.get("NETRACARE_DOCS_DIR", DATA3_DIR / "documents"))


def index_dir() -> str:
    """Where the Chroma vector index is saved (built once by build_index.py, not on every request)."""
    return os.environ.get("NETRACARE_INDEX_DIR", str(BASE_DIR / "chroma_db"))


def db_path() -> str:
    return os.environ.get("NETRACARE_DB", str(BASE_DIR / "netracare.db"))


def clinic_now() -> datetime:
    """The clinic clock (naive, clinic-local time).

    Set CLINIC_NOW=2026-11-02T10:00 to freeze it (used by the automated rule tests).
    Every rule uses this function, never datetime.now() directly.
    """
    fixed = os.environ.get("CLINIC_NOW")
    if fixed:
        return datetime.fromisoformat(fixed.replace(" ", "T"))
    return datetime.now().replace(second=0, microsecond=0)


# Clinic constants (brief section 03 and 04)
SLOT_MINUTES = 10
DURATION = {"new": 20, "follow_up": 10}
SESSIONS = {"AM": ("09:00", "13:00"), "PM": ("14:00", "18:00")}
BOOKING_WINDOW_DAYS = 30
CANCEL_CUTOFF_HOURS = 2
