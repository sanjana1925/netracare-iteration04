"""The clinic knowledge base (RAG). ALL retrieval code lives here:

  build_index()        split the 3 documents -> embed -> save to Chroma (run once: python build_index.py)
  search_*()           keyword (BM25), vector (embeddings) and HYBRID search, top 4
  answer_question()    LLM chain: answer from the 4 chunks only -> answer, source_ids, answered
                       YOUR CODE (not the LLM) then builds the Sources line or the fixed "I don't know".
"""
import os
import re
import string
from functools import lru_cache
from typing import Optional

from langchain_core.documents import Document
from langchain_core.output_parsers import PydanticOutputParser, StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, Field

from . import config, llm

TOP_K = 4
CHUNK_SIZE, CHUNK_OVERLAP = 1000, 100
COLLECTION = "netracare_docs"

# Fixed words that come from OUR code, never from the LLM
IDK_MESSAGE = "I don't know. Our clinic documents don't cover that. Please ask Staff at the front desk."
OUT_OF_SCOPE_MESSAGE = "NetraCare only looks after eyes, so I can't help with that here. For other health problems, please see another doctor."
CHAT_ERROR = "Sorry, I can't answer just now. Please try again, or ask Staff at the front desk."


class IndexNotBuilt(Exception):
    pass


# ---------------------------------------------------------------------------
# R1: chunking. Split on "##" headings first (one chunk per section), and split a section again only if it is long.
# ---------------------------------------------------------------------------
def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def load_chunks(docs_dir=None) -> list[Document]:
    paths = sorted(docs_dir.glob("*.md")) if docs_dir else config.document_paths()
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    chunks: list[Document] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        # sections = text between "## " headings. The title/intro above the first heading is not a section.
        parts = re.split(r"(?m)^## +(.+?)\s*$", text)
        for heading, body in zip(parts[1::2], parts[2::2]):
            body = body.strip()
            pieces = [body] if len(body) + len(heading) + 1 <= CHUNK_SIZE else splitter.split_text(body)
            for i, piece in enumerate(pieces):
                chunk_id = f"{path.name}#{_slug(heading)}" + (f"-{i + 1}" if len(pieces) > 1 else "")
                chunks.append(Document(
                    # the heading is kept in the text too, so a search for "Dilation" can match the section title
                    page_content=f"{heading}\n{piece}",
                    metadata={"source": path.name, "section": heading, "chunk_id": chunk_id}))
    return chunks


# ---------------------------------------------------------------------------
# Embeddings and the vector store (provider read from .env; if you change the model, rebuild the index)
# ---------------------------------------------------------------------------
def get_embeddings():
    return _embeddings(os.environ.get("EMBEDDING_PROVIDER", "local").strip().lower(),
                       os.environ.get("EMBEDDING_MODEL", "").strip())


@lru_cache(maxsize=4)
def _embeddings(provider: str, model: str):
    """Loaded once per process: loading a local model takes seconds, so it must not happen per request."""
    if provider == "local":  # free, runs on your PC, no key needed
        from langchain_huggingface import HuggingFaceEmbeddings
        return HuggingFaceEmbeddings(model_name=model or "sentence-transformers/all-MiniLM-L6-v2",
                                     model_kwargs={"device": "cpu"}, encode_kwargs={"normalize_embeddings": True})
    if provider == "openai":
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(model=model or "text-embedding-3-small", api_key=os.environ.get("OPENAI_API_KEY"))
    if provider == "google":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        return GoogleGenerativeAIEmbeddings(model=model or "models/text-embedding-004", google_api_key=os.environ.get("GOOGLE_API_KEY"))
    raise ValueError(f"Unknown EMBEDDING_PROVIDER '{provider}'. Use local, openai or google.")


def _store():
    from langchain_chroma import Chroma
    return Chroma(collection_name=COLLECTION, embedding_function=get_embeddings(), persist_directory=config.index_dir(),
                  collection_metadata={"hnsw:space": "cosine"})


def build_index() -> int:
    """Rebuild the whole index from the documents. Run it again whenever a document or the embedding model changes."""
    reset()
    chunks = load_chunks()
    if not chunks:
        raise FileNotFoundError("No .md documents found in data3/documents or data4/documents")
    store = _store()
    try:
        store.delete_collection()
    except Exception:
        pass
    store = _store()
    store.add_documents(chunks, ids=[c.metadata["chunk_id"] for c in chunks])
    reset()
    return len(chunks)


# ---------------------------------------------------------------------------
# R2: BM25 + vector + hybrid
# ---------------------------------------------------------------------------
def tokenize(text: str) -> list[str]:
    """Lowercase and strip punctuation before BM25 scoring, so 'Menon?' matches 'Menon'."""
    return text.lower().translate(str.maketrans({c: " " for c in string.punctuation})).split()


@lru_cache(maxsize=1)
def _state():
    from langchain_classic.retrievers import EnsembleRetriever
    from langchain_community.retrievers import BM25Retriever
    store = _store()
    got = store.get(include=["documents", "metadatas"])
    if not got["ids"]:
        raise IndexNotBuilt("The knowledge base index is empty. Run: python build_index.py")
    docs = [Document(page_content=t, metadata=m) for t, m in zip(got["documents"], got["metadatas"])]
    docs.sort(key=lambda d: d.metadata["chunk_id"])
    bm25 = BM25Retriever.from_documents(docs, preprocess_func=tokenize, k=TOP_K)
    vector = store.as_retriever(search_kwargs={"k": TOP_K})
    hybrid = EnsembleRetriever(retrievers=[bm25, vector], weights=[0.5, 0.5])  # equal weight
    return {"store": store, "docs": docs, "bm25": bm25, "vector": vector, "hybrid": hybrid}


def reset() -> None:
    _state.cache_clear()


def index_size() -> int:
    try:
        return len(_state()["docs"])
    except Exception:
        return 0


def _row(doc: Document, score: float) -> dict:
    return {"chunk_id": doc.metadata["chunk_id"], "source": doc.metadata["source"], "section": doc.metadata["section"],
            "score": round(float(score), 4), "text": doc.page_content}


def search_keyword(query: str, k: int = TOP_K) -> list[dict]:
    st = _state()
    scores = st["bm25"].vectorizer.get_scores(tokenize(query))
    ranked = sorted(range(len(st["docs"])), key=lambda i: (-scores[i], st["docs"][i].metadata["chunk_id"]))[:k]
    return [_row(st["docs"][i], scores[i]) for i in ranked if scores[i] > 0]  # no shared word = not a keyword hit


def search_vector(query: str, k: int = TOP_K) -> list[dict]:
    pairs = _state()["store"].similarity_search_with_score(query, k=k)  # cosine distance: lower is closer
    return [_row(d, 1 - dist) for d, dist in pairs]


def search_hybrid(query: str, k: int = TOP_K) -> list[dict]:
    """Keyword + vector, equal weight, merged by rank (reciprocal rank fusion). The top k chunks."""
    st = _state()
    merged = st["hybrid"].invoke(query)[:k]
    kw = {r["chunk_id"]: i for i, r in enumerate(search_keyword(query))}
    vec = {r["chunk_id"]: i for i, r in enumerate(search_vector(query))}
    rows = []
    for d in merged:  # display score = the weighted reciprocal-rank score the ensemble used (c=60)
        cid = d.metadata["chunk_id"]
        s = sum(0.5 / (60 + ranks[cid] + 1) for ranks in (kw, vec) if cid in ranks)
        rows.append(_row(d, s))
    return rows


def search_debug(query: str) -> dict:
    return {"query": query, "keyword": search_keyword(query), "vector": search_vector(query), "hybrid": search_hybrid(query)}


# ---------------------------------------------------------------------------
# R5 + R6: answer from the chunks only; the sources and "I don't know" come from code
# ---------------------------------------------------------------------------
class AnswerResult(BaseModel):
    answer: str = Field(description="the answer, at most 120 words, from the chunks only")
    source_ids: list[str] = Field(description="chunk_ids of the chunks the answer used; empty if none answered it")
    answered: bool = Field(description="true only if the chunks actually answer the question")
    not_about_eyes: bool = Field(default=False, description="true only if the patient describes a health problem that is clearly NOT about the eyes (for example a skin rash)")


_answer_parser = PydanticOutputParser(pydantic_object=AnswerResult)

_ANSWER_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     """You are the patient assistant of NetraCare, an eye clinic. Answer the patient's question using ONLY the numbered
chunks below. Each chunk starts with its chunk_id.

Rules:
- Use only facts that are in the chunks. If the chunks do not answer the question, set answered to false and source_ids to [].
  Do not guess, and do not use outside knowledge.
- A chunk that only tells the patient to ask Staff does NOT answer a question about a specific fact (for example whether a payment
  method is accepted, or whether something is available). Then set answered to false.
- At most 120 words. Plain, friendly wording. Do NOT write a "Sources" line: the app adds the sources itself.
- List in source_ids the chunk_ids you actually used.
- NEVER name a condition the patient might have, and never say what a symptom might mean or what is causing it.
- NEVER suggest a treatment, medicine, eye drops or product. You may repeat what a clinic document says about a procedure
  (what to expect, before and after) because it is clinic information.
- Costs are indicative only; say so. Never take payments or help with insurance claims.
- You only answer questions from the clinic documents. Booking, cancelling and changing appointments is done by the booking assistant
  with its own tools, not by you: for a question about how booking works, answer only what the chunks say. You may share
  a doctor's OPD days from the doctors document.
- Politely decline anything that is not about NetraCare (the clinic, its doctors, procedures and policies).

Speciality routing (whenever the patient describes a problem, symptom or need in their own words, for example "my eyes itch",
"I need new glasses" or "I have never had my eyes checked"):
- Compare their words with the "Who it is for" lists in the doctors document and choose the speciality whose list matches best.
  A specific list wins over General: if the patient's words are close to a line of a specific speciality's list, choose that speciality
  even when the problem sounds mild. Close means the same idea in other words (for example "sandy" and "gritty", "after a day on the
  laptop" and "after screen work", "get rid of my glasses for good" and "stop wearing glasses", "seeing double" and "double vision",
  "an eye that turns inwards" and "an eye that turns in").
  Mild or unspecific symptoms (mild redness, watering or itching), routine eye tests, new glasses and "not sure" go to General, but only when
  nothing more specific matches.
  If the words fit a General line (for example "eyes that tire easily") AND ALSO a specific speciality's line (for example "sandy" and
  "after a day on the laptop" fit Dry eye's "gritty ... often worse after screen work"), the specific speciality wins. General is only the
  choice when no specific line fits.
  Patients under 16 go to Paediatric, whatever the problem. If the question says the problem is a child's, treat it as a child's.
- Reply in this form and name ALL the doctors of that speciality, from the doctors document: "A <speciality> specialist can assess
  this. At NetraCare that is <doctors>. The doctor will assess you at your visit." Start with exactly "A <speciality> specialist can assess
  this." Do not add a sentence before it, and do not repeat or quote what the document says about who the speciality is for. Put the doctors chunk you used in source_ids.
- Do NOT describe procedures, costs, timings or preparation unless the patient asked about them.
- Never say what the problem might be, or what could be causing it. A doctor will assess at the visit. Do not repeat the patient's
  symptom as a name or label either (do not write "double vision can be assessed by ..."): say "this".
- If the problem is clearly not about the eyes (for example a skin rash), set not_about_eyes to true and answered to false.
  Suggest no speciality.

{format_instructions}"""),
    ("human", "Patient age: {age}\n\nChunks:\n{chunks}\n\nPatient question: {question}"),
]).partial(format_instructions=_answer_parser.get_format_instructions())
ANSWER_SYSTEM_TEXT = _ANSWER_PROMPT.messages[0].prompt.template  # the answer prompt's rules, readable on their own (reviewed by tests)


def format_chunks(rows: list[dict]) -> str:
    return "\n\n".join(f"[chunk_id: {r['chunk_id']}]\n{r['text']}" for r in rows)


def _limit_words(text: str, n: int = 120) -> str:
    words = text.split()
    return text.strip() if len(words) <= n else " ".join(words[:n]).rstrip(",;:") + "…"


def answer_question(question: str, rows: list[dict], age: Optional[int] = None) -> dict:
    """Returns {answered, kind, answer, sources}. `sources` is built from the retrieved chunks' metadata by THIS code.

    Raises on an LLM or parser failure (the caller shows CHAT_ERROR).
    """
    res: AnswerResult = llm.run_chain(lambda m: _ANSWER_PROMPT | m | _answer_parser,
                                      {"question": question, "chunks": format_chunks(rows),
                                       "age": age if age is not None else "unknown"})
    return finalise_answer(res, rows)


def finalise_answer(res: "AnswerResult", rows: list[dict]) -> dict:
    """The part that is CODE, not the model: decide between an answer, the fixed "I don't know" and the fixed scope message,
    and build the Sources line from the retrieved chunks' metadata. The model never writes the sources."""
    if res.not_about_eyes:  # decided by the model, worded by code: no sources, no speciality
        return {"answered": False, "kind": "out_of_scope", "answer": OUT_OF_SCOPE_MESSAGE, "sources": []}
    by_id = {r["chunk_id"]: r for r in rows}
    used = list(dict.fromkeys(res.source_ids))
    # "I don't know" is decided here: not answered, no sources, or a source that was never retrieved
    if not res.answered or not used or any(s not in by_id for s in used) or not res.answer.strip():
        return {"answered": False, "kind": "idk", "answer": IDK_MESSAGE, "sources": []}
    sources = list(dict.fromkeys(f"{by_id[s]['source']} › {by_id[s]['section']}" for s in used))
    return {"answered": True, "kind": "answer", "answer": _limit_words(" ".join(res.answer.split())), "sources": sources}
