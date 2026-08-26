"""
alpha_detective.py — Alpha-Detective Hybrid Search RAG Engine
=============================================================

A Retrieval-Augmented Generation (RAG) pipeline for earnings call transcripts
built on LlamaIndex + ChromaDB.

Retrieval is HYBRID::

    Dense vector search (OpenAI embeddings, ChromaDB)   similarity_top_k=5
                          \\                            /
    Sparse lexical search (BM25, rank-bm25)    similarity_top_k=5
                          \\                            /
                   QueryFusionRetriever(mode="reciprocal_rerank")
                                     |
                        Reciprocal Rank Fusion (RRF)

The fused top-K nodes are concatenated into a strict prompt and answered by
``gpt-3.5-turbo``.  The model is instructed to answer ONLY from the provided
context ("Information not found in transcripts." otherwise).
"""

from __future__ import annotations

import os
from typing import Tuple

# --------------------------------------------------------------------------- #
# ⚠️  Set your NEW OpenAI API key here!
# --------------------------------------------------------------------------- #
os.environ["OPENAI_API_KEY"] = "OPENAI_API_KEY"  # <-- REPLACE ME

# ----------------------------- LlamaIndex imports --------------------------- #
from llama_index.core import Document, Settings, StorageContext, VectorStoreIndex
from llama_index.core.node_parser import SimpleNodeParser
from llama_index.core.retrievers import QueryFusionRetriever
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.llms.openai import OpenAI
from llama_index.retrievers.bm25 import BM25Retriever
from llama_index.vector_stores.chroma import ChromaVectorStore

import chromadb
import pandas as pd

# --------------------------------------------------------------------------- #
# Tunable settings
# --------------------------------------------------------------------------- #

EMBED_MODEL = "text-embedding-3-small"   # cheap & strong default embedding model
LLM_MODEL = "gpt-3.5-turbo"              # LLM used for final answer generation
TOP_K = 5                                # nodes retrieved per technique & after fusion
FUSION_NUM_QUERIES = 1                   # 1 = no LLM query expansion, pure RRF fusion
CHROMA_COLLECTION = "alpha_detective"    # ChromaDB collection name
DEFAULT_PERSIST_DIR = "./chroma_db"      # ChromaDB persistence folder
DEFAULT_CSV = "earnings_transcripts.csv"  # produced by combine_data.py
CHUNK_SIZE = 1024                        # token budget per node (small enough for
CHUNK_OVERLAP = 64                       #   gpt-3.5-turbo context + prompt)

STRICT_PROMPT = (
    "Given the context information and not prior knowledge, answer the query. "
    "If the context doesn't contain the answer, say "
    "'Information not found in transcripts.'\n"
    "Context:\n{context}\n\nQuery: {query}\nAnswer:"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _ensure_api_key() -> None:
    """Refuse to run with the placeholder key and give a clear error."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key or key == "YOUR_NEW_OPENAI_API_KEY_HERE":
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Edit alpha_detective.py and replace "
            "'YOUR_NEW_OPENAI_API_KEY_HERE', or set the OPENAI_API_KEY environment "
            "variable, then restart the app."
        )


# --------------------------------------------------------------------------- #
# Step 1 — Load data
# --------------------------------------------------------------------------- #

def load_data(csv_path: str = DEFAULT_CSV) -> list:
    """Read ``earnings_transcripts.csv`` into chunked LlamaIndex nodes.

    Each CSV row becomes one Document; ``Ticker``, ``Company_Name`` and
    ``Quarter`` are stored in the metadata so retrieved nodes can show
    *which* transcript a fact came from.  Every document is then split into
    chunks — the returned list is those chunk nodes, which are used BOTH for
    dense indexing and for the BM25 corpus (guaranteeing both retrievers
    search the exact same chunks).
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"'{csv_path}' missing. Run `python combine_data.py` first "
            "to build the CSV from your transcripts folder."
        )

    df = pd.read_csv(csv_path)

    required = {"Ticker", "Company_Name", "Quarter", "Text"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV is missing required column(s): {sorted(missing)}")

    # Keep only rows with actual transcript text.
    df = df[df["Text"].notna() & (df["Text"].str.strip() != "")].copy()
    if df.empty:
        raise ValueError("No non-empty transcript rows found in the CSV.")

    parser = SimpleNodeParser.from_defaults(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
    )

    nodes: list = []
    for _, row in df.iterrows():
        metadata = {
            # ChromaDB requires string metadata values.
            "Ticker": str(row["Ticker"]),
            "Company_Name": str(row["Company_Name"]),
            "Quarter": str(row["Quarter"]),
        }
        doc = Document(text=str(row["Text"]), metadata=metadata)
        # Split each (large) transcript into chunks; every chunk inherits the
        # metadata, so BM25 + dense retrieval both work at chunk granularity.
        nodes.extend(parser.get_nodes_from_documents([doc]))
    return nodes


# --------------------------------------------------------------------------- #
# Step 2 — Hybrid retriever (Dense + BM25, fused with RRF)
# --------------------------------------------------------------------------- #

def build_hybrid_retriever(
    nodes: list,
    persist_dir: str = DEFAULT_PERSIST_DIR,
    reset_collection: bool = False,
) -> QueryFusionRetriever:
    """Build the hybrid dense+sparse retriever backed by ChromaDB.

    Args:
        nodes: chunk nodes produced by :func:`load_data`.
        persist_dir: ChromaDB persistence folder.
        reset_collection: when True, drop and recreate the ChromaDB collection
            before indexing (avoids duplicate chunks when the user rebuilds
            the index by clicking the button again).

    Returns:
        A ``QueryFusionRetriever`` whose ``mode="reciprocal_rerank"``
        implements Reciprocal Rank Fusion over:

        1. a **dense** retriever — OpenAI embeddings stored in a persistent
           ChromaDB collection, ``similarity_top_k=TOP_K``, and
        2. a **sparse** retriever — BM25 over the same chunks
           (``bm25s`` backend, ``rank-bm25`` API), ``similarity_top_k=TOP_K``.
    """
    _ensure_api_key()

    # --- LLM + embedding models are set globally via Settings ---
    Settings.llm = OpenAI(model=LLM_MODEL)
    Settings.embed_model = OpenAIEmbedding(model=EMBED_MODEL)

    # --- 1. ChromaDB persistent client + collection -------------------------
    chroma_client = chromadb.PersistentClient(path=persist_dir)
    if reset_collection:
        try:
            chroma_client.delete_collection(name=CHROMA_COLLECTION)
        except Exception:
            pass  # collection does not exist yet — fine
    chroma_collection = chroma_client.get_or_create_collection(
        name=CHROMA_COLLECTION,
        # metadata={"hnsw:space": "cosine"}  # uncomment to switch distance metric
    )
    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)

    # --- 2. Index documents into the vector store (dense search) ------------
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    index = VectorStoreIndex(
        nodes=nodes,
        storage_context=storage_context,
    )

    # --- 3. Dense retriever --------------------------------------------------
    dense_retriever = index.as_retriever(similarity_top_k=TOP_K)

    # --- 4. Sparse BM25 retriever over the SAME chunks -----------------------
    # NOTE (llama-index >= 0.12 / bm25s-backed retriever): when the index uses
    # an external vector store (ChromaDB), ``index.docstore`` stays empty, so
    # constructing BM25 from ``docstore=index.docstore`` yields an empty
    # corpus and crashes.  Passing the chunk nodes directly fixes this and
    # guarantees the BM25 corpus == the dense index.
    sparse_retriever = BM25Retriever.from_defaults(
        nodes=nodes,
        similarity_top_k=TOP_K,
    )

    # --- 5. Fuse both with Reciprocal Rank Fusion (RRF) ----------------------
    hybrid_retriever = QueryFusionRetriever(
        [dense_retriever, sparse_retriever],
        similarity_top_k=TOP_K,
        mode="reciprocal_rerank",  # type: ignore # RRF: score = sum over ranks of 1/(k + rank)
        num_queries=FUSION_NUM_QUERIES,
    )
    return hybrid_retriever


# --------------------------------------------------------------------------- #
# Step 3 — Query system (retrieve -> strict prompt -> LLM answer)
# --------------------------------------------------------------------------- #

def query_system(
    query: str,
    hybrid_retriever: QueryFusionRetriever,
) -> Tuple[str, list]:
    """Run a user query through the hybrid retriever and the LLM.

    Returns ``(answer, retrieved_nodes)`` where ``retrieved_nodes`` is the
    fused top-K list of ``NodeWithScore`` — each carries ``.metadata``
    (Company_Name, Ticker, Quarter) and ``.score`` (the RRF score), which the
    Streamlit UI displays to prove hybrid search is working.
    """
    _ensure_api_key()

    # --- Hybrid retrieval (RRF fusion of dense + BM25) -----------------------
    retrieved_nodes = hybrid_retriever.retrieve(query)
    retrieved_nodes = retrieved_nodes[:TOP_K]

    # --- Assemble context and call the LLM with the strict prompt ------------
    context = "\n\n".join(
        f"[Source: {n.metadata.get('Company_Name', '?')} "
        f"({n.metadata.get('Ticker', '?')}, {n.metadata.get('Quarter', '?')})]\n"
        f"{n.node.get_content()}"
        for n in retrieved_nodes
    )
    prompt = STRICT_PROMPT.format(context=context, query=query)

    response = Settings.llm.complete(prompt)
    return str(response), retrieved_nodes


# --------------------------------------------------------------------------- #
# Convenience entry point (CLI smoke test)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Alpha-Detective CLI smoke test")
    parser.add_argument("--csv", default=DEFAULT_CSV, help="path to earnings_transcripts.csv")
    parser.add_argument("-q", "--query", default="What was Accenture's revenue growth?")
    args = parser.parse_args()

    docs = load_data(args.csv)
    print(f"[load_data] {len(docs):,} chunks from {args.csv}")
    hybrid = build_hybrid_retriever(docs, persist_dir=DEFAULT_PERSIST_DIR)
    print("[build] Hybrid retriever (dense + BM25, RRF fusion) ready.")
    answer, nodes = query_system(args.query, hybrid)
    print("\n--- ANSWER ---\n", answer)
    print("\n--- TOP SOURCES ---")
    for n in nodes:
        print(
            f"{n.metadata.get('Company_Name')} | {n.metadata.get('Ticker')} | "
            f"RRF {n.score:.4f}"
        )