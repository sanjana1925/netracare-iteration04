"""Build (or rebuild) the knowledge-base index:   python build_index.py

Run it once after setup, and again whenever a document or EMBEDDING_MODEL changes.
It is NOT run on every request."""
import sys

from app import config, rag


def main() -> int:
    chunks = rag.load_chunks()
    print("Documents:", ", ".join(str(p.parent.parent.name + "/" + p.name) for p in config.document_paths()))
    for name in sorted({c.metadata['source'] for c in chunks}):
        n = sum(1 for c in chunks if c.metadata["source"] == name)
        print(f"  {name}: {n} chunks")
    print(f"Embedding {len(chunks)} chunks ...")
    count = rag.build_index()
    print(f"Done: {count} chunks saved to {config.index_dir()}")
    if "--show" in sys.argv:
        for c in chunks[:5]:
            print("-" * 60, "\n", c.metadata, "\n", c.page_content[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
