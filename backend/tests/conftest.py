import os

# The tests make hundreds of AI calls: they must never be traced by accident because a key happens to be in .env.
# (To trace on purpose, run test_iteration4_tracing_live.py with NETRACARE_TEST_TRACING=1.)
if os.environ.get("NETRACARE_TEST_TRACING") != "1":
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"

import csv
import os
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from helpers import CLINIC_NOW, DATA


@pytest.fixture(scope="session", autouse=True)
def real_model_required():
    """These tests call the REAL language model (there is no stand-in model anywhere). Stop at once, with a clear
    message, if no key is configured, instead of letting every booking fail with a confusing 503."""
    from app import llm
    if not llm.llm_configured():
        pytest.exit("No LLM key found. These tests call the real model: put your key in backend/.env (see .env.example).", returncode=2)


@pytest.fixture(autouse=True)
def pace_the_real_model():
    """The tests call the REAL model, and a free-tier key allows only so many requests per minute. A short pause after every test keeps
    a long run under that limit (otherwise one burst of HTTP 429 makes every later call fail, which says nothing about the app).
    Change it with TEST_PACE_SECONDS (0 switches it off)."""
    yield
    time.sleep(float(os.environ.get("TEST_PACE_SECONDS", "3")))


def _build_template(tmp_path_factory, name, seed, pack2):
    path = tmp_path_factory.mktemp(name) / "template.db"
    old = os.environ.get("NETRACARE_DB")
    os.environ["NETRACARE_DB"] = str(path)
    os.environ["CLINIC_NOW"] = CLINIC_NOW
    from app import db
    db.init_db(with_seed_appointments=seed, with_past_visits=pack2)
    if old is not None:
        os.environ["NETRACARE_DB"] = old
    return path


@pytest.fixture(scope="session")
def template_db(tmp_path_factory):
    """Iteration 1 database: data pack 1 + stretch seed appointments (data pack 2 left out so those tests are unchanged)."""
    return _build_template(tmp_path_factory, "tpl1", seed=True, pack2=False)


@pytest.fixture(scope="session")
def template_db2(tmp_path_factory):
    """Iteration 2 database: data pack 1 + data pack 2 (27 past visits, profiles), no seed appointments."""
    return _build_template(tmp_path_factory, "tpl2", seed=False, pack2=True)


def _client(template, tmp_path, monkeypatch):
    fresh = tmp_path / "test.db"
    shutil.copy(template, fresh)
    monkeypatch.setenv("NETRACARE_DB", str(fresh))
    monkeypatch.setenv("CLINIC_NOW", CLINIC_NOW)
    from app.main import app
    return TestClient(app)  # no `with`: lifespan is skipped, the DB is already loaded


@pytest.fixture()
def client(template_db, tmp_path, monkeypatch):
    return _client(template_db, tmp_path, monkeypatch)


@pytest.fixture()
def client2(template_db2, tmp_path, monkeypatch):
    return _client(template_db2, tmp_path, monkeypatch)


@pytest.fixture(scope="session")
def template_db4(tmp_path_factory):
    """The database the data pack 4 README starts every scenario from: data pack 1 + stretch/appointments_seed.csv + data pack 2 past visits."""
    return _build_template(tmp_path_factory, "tpl4", seed=True, pack2=True)


@pytest.fixture()
def client4(template_db4, tmp_path, monkeypatch):
    from app import agent
    agent.reset()  # no conversation survives from an earlier test
    yield _client(template_db4, tmp_path, monkeypatch)
    agent.reset()


@pytest.fixture(scope="session")
def _index_path(tmp_path_factory):
    """Build the REAL Chroma index once per test run (real local embeddings, no key needed)."""
    path = tmp_path_factory.mktemp("chroma")
    os.environ["NETRACARE_INDEX_DIR"] = str(path)
    from app import rag
    rag.build_index()
    return path


@pytest.fixture()
def rag_index(_index_path, monkeypatch):
    from app import rag
    monkeypatch.setenv("NETRACARE_INDEX_DIR", str(_index_path))
    rag.reset()
    yield rag
    rag.reset()


@pytest.fixture(scope="session")
def patients():
    with open(DATA / "patients.csv", newline="", encoding="utf-8") as f:
        return {r["patient_id"]: r for r in csv.DictReader(f)}
