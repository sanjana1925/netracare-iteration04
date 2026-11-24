"""Iteration 3 checks that stay valid in Iteration 4: the clinic knowledge base (chunking, Chroma, BM25, hybrid search, the Staff
search debug view) and the code that builds sources and "I don't know". Nothing here is faked: the real local embeddings and the real
documents are used. The CHAT itself is now the booking agent: it is tested in test_iteration4_*.py and test_agent_scenarios_live.py.
"""
import pytest

from app import rag
from helpers import login_doctor, login_patient, login_staff
from test_iteration2 import break_model

EMERGENCY = ("What you describe needs urgent attention. Please come to the clinic now or go to the nearest emergency "
             "department. Our staff have been notified.")
IDK = "I don't know. Our clinic documents don't cover that. Please ask Staff at the front desk."


def ids(rows):
    return [(r["source"], r["section"]) for r in rows]


# ---------------- R1 chunking and metadata ----------------
def test_r1_chunks_one_per_section_with_metadata():
    chunks = rag.load_chunks()
    sections = {(c.metadata["source"], c.metadata["section"]) for c in chunks}
    assert len(sections) == 40  # every "##" heading is a section: 38 in data pack 3 + 2 new ones in data pack 4's policy document
    assert 40 <= len(chunks) <= 47  # one per section, a few more where a section is longer than the chunk size
    assert len({c.metadata["chunk_id"] for c in chunks}) == len(chunks)  # unique ids
    for c in chunks:
        assert c.metadata["source"].endswith(".md") and c.metadata["section"] and c.metadata["chunk_id"]
        assert len(c.page_content) < 1300  # nothing grew past the chunk size (plus the heading)
    assert {c.metadata["source"] for c in chunks} == {"doctors.md", "procedures.md", "clinic_policy_faq.md"}


def test_r1_index_built_once_with_metadata(rag_index):
    assert rag_index.index_size() == len(rag_index.load_chunks())
    row = rag_index.search_vector("when are you open on saturday")[0]
    assert row["source"] and row["section"] and row["chunk_id"]


# ---------------- R2 BM25 / vector / hybrid ----------------
def test_r2_bm25_strips_punctuation_and_lowercases():
    assert rag.tokenize("Is Dr. Menon, here?") == ["is", "dr", "menon", "here"]


def test_r2_keyword_matches_a_name_despite_punctuation(rag_index):
    top = ids(rag_index.search_keyword("Menon?"))
    assert ("doctors.md", "Dr. Vikram Menon") in top[:2]


def test_r2_row1_hba1c_keyword_first_and_hybrid_top4(rag_index):
    d = rag_index.search_debug("Should I bring my HbA1c report?")
    assert ids(d["keyword"])[0] == ("procedures.md", "Diabetic eye check")
    assert ("procedures.md", "Diabetic eye check") in ids(d["hybrid"])
    assert len(d["hybrid"]) <= 4 and len(d["keyword"]) <= 4 and len(d["vector"]) <= 4


def test_r2_row2_squint_found_by_meaning_not_by_words(rag_index):
    d = rag_index.search_debug("Can my kid have breakfast on the morning of the operation to straighten his eye?")
    squint = ("procedures.md", "Squint surgery")
    assert squint in ids(d["vector"])  # the embedding understands "operation to straighten his eye"
    # keyword search shares no distinctive word with the Squint surgery chunk (only everyday words such as "the" and "to"), so it can
    # only list it by chance when the document set is small; the meaning-based search is what really finds it
    stop = {"can", "my", "have", "on", "the", "of", "to", "his", "his", "eye", "a", "an", "and", "is", "in", "for"}
    chunk = next(x for x in rag_index._state()["docs"] if x.metadata["chunk_id"] == "procedures.md#squint-surgery")
    shared = (set(rag_index.tokenize(d["query"])) & set(rag_index.tokenize(chunk.page_content))) - stop
    assert not shared, f"keyword search should share no distinctive word, but shares {shared}"
    assert squint in ids(d["hybrid"])  # but hybrid still finds it
    for r in d["hybrid"]:
        assert set(r) >= {"source", "section", "chunk_id", "score"}


def test_r2_search_debug_is_staff_only(client2, rag_index, patients):
    url = "/staff/search-debug?q=dilation"
    r = client2.get(url, headers=login_staff(client2))
    assert r.status_code == 200 and set(r.json()) == {"query", "keyword", "vector", "hybrid"}
    assert client2.get(url, headers=login_patient(client2, patients["P01"])).status_code == 403
    assert client2.get(url, headers=login_doctor(client2, "D1")).status_code == 403
    assert client2.get(url).status_code == 401


def test_r2_search_debug_without_index_says_how_to_build_it(client2, tmp_path, monkeypatch):
    monkeypatch.setenv("NETRACARE_INDEX_DIR", str(tmp_path / "empty_index"))
    rag.reset()
    r = client2.get("/staff/search-debug?q=hello", headers=login_staff(client2))
    assert r.status_code == 503 and "build_index.py" in r.json()["detail"]["message"]
    rag.reset()




# ---------------- R3 chat window rules ----------------
def chat(client, headers, message, history=None):
    return client.post("/chat", headers=headers, json={"message": message, "history": history or []})


# ---------------- R4 red flag before every answer ----------------
# ---------------- R5 sources from code, or "I don't know" ----------------
# ---- the part that is CODE, called directly with hand-written model replies ----
ROWS = [{"chunk_id": "doctors.md#general", "source": "doctors.md", "section": "General", "score": 1, "text": "General"},
        {"chunk_id": "procedures.md#cataract-surgery-1", "source": "procedures.md", "section": "Cataract surgery", "score": 1, "text": "x"}]


def _res(**kw):
    base = {"answer": "An answer.", "source_ids": ["doctors.md#general"], "answered": True, "not_about_eyes": False}
    return rag.AnswerResult(**{**base, **kw})


def test_code_builds_the_sources_from_metadata():
    out = rag.finalise_answer(_res(source_ids=["doctors.md#general", "procedures.md#cataract-surgery-1", "doctors.md#general"]), ROWS)
    assert out["kind"] == "answer" and out["sources"] == ["doctors.md › General", "procedures.md › Cataract surgery"]  # in order, no duplicates


@pytest.mark.parametrize("bad", [
    {"answered": True, "source_ids": []},                       # claims an answer but cites nothing
    {"answered": True, "source_ids": ["nonexistent.md#x"]},     # cites a chunk that was never retrieved
    {"answered": False},                                        # says it could not answer
    {"answer": "   "},                                          # an empty answer
])
def test_code_overrides_an_unreliable_model_answer_with_the_fixed_idk(bad):
    assert rag.finalise_answer(_res(**bad), ROWS) == {"answered": False, "kind": "idk", "answer": IDK, "sources": []}


def test_code_shows_the_fixed_scope_message_for_a_problem_that_is_not_about_the_eyes():
    out = rag.finalise_answer(_res(not_about_eyes=True, answered=False, source_ids=[]), ROWS)
    assert out["kind"] == "out_of_scope" and out["sources"] == [] and "only looks after eyes" in out["answer"]
    assert "Dr." not in out["answer"] and "speciality" not in out["answer"].lower()


def test_code_caps_the_answer_at_120_words():
    out = rag.finalise_answer(_res(answer=" ".join(["word"] * 300)), ROWS)
    assert len(out["answer"].split()) <= 121


def test_the_answer_prompt_has_the_safety_and_routing_rules_and_shows_the_model_only_4_chunks_and_the_age():
    rendered = rag._ANSWER_PROMPT.format_messages(question="q", age=11, chunks=rag.format_chunks(ROWS * 2))
    s = rendered[0].content
    for phrase in ("NEVER name a condition", "NEVER suggest a treatment", "indicative only", "Politely decline anything", "ONLY the numbered",
                   'Do NOT write a "Sources" line', "120 words", "only answer questions from the clinic documents", "Patients under 16 go to Paediatric",
                   "go to General", "not_about_eyes", "A doctor will assess", "ALL the doctors of that speciality",
                   "only tells the patient to ask Staff does NOT answer a question about a specific fact",
                   "Do NOT describe procedures, costs, timings or preparation unless the patient asked about them"):
        assert phrase in s, phrase
    human = rendered[1].content
    assert human.count("[chunk_id:") == 4 and "Patient age: 11" in human  # the age only: no name, no mobile number


# ---------------- R6 routing (real) ----------------
def test_r6_routing_question_retrieves_the_doctors_document(rag_index):
    for q, doctor in (("My optician said the pressure in my eyes is high", "Dr. Meera Iyer"),
                      ("My 5 year old's left eye turns inwards", "Dr. Vikram Menon")):
        rows = rag_index.search_hybrid(q)
        assert any(r["source"] == "doctors.md" for r in rows), q
        assert any(doctor in r["text"] for r in rag_index.search_hybrid(q + " doctor")), q


# ---------------- failures ----------------
