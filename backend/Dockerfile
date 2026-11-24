# NetraCare Iteration 4 backend: FastAPI + SQLite + LangChain + LangGraph (the booking agent) + the clinic knowledge base (Chroma + BM25). Data packs 1 to 4 are copied in;
# the database lives on a volume at /data. The vector index is built ONCE, here at image build time (not on every request).
# The API key is NOT in this image: docker-compose passes backend/.env to the container at run time (see .dockerignore).
FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
# CPU-only PyTorch (the local embedding model needs no GPU); keeps the image small
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt

COPY app app
COPY data data
COPY data2 data2
COPY data3 data3
COPY data4 data4
COPY build_index.py .
# download the embedding model and build the index from data3/documents. Re-run  python build_index.py  (docker compose exec backend ...) after a document changes
RUN python build_index.py

ENV NETRACARE_DB=/data/netracare.db \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=40s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
