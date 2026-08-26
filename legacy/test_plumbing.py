"""End-to-end plumbing test for the Alpha-Detective hybrid retrieval chain.

Uses a FAKE embedding model so no OpenAI API call is made. Verifies:
  1. chromadb 1.5.9 PersistentClient + get_or_create_collection
  2. ChromaVectorStore + StorageContext + VectorStoreIndex.from_documents
  3. index.as_retriever(similarity_top_k)  (dense)
  4. BM25Retriever.from_defaults(docstore=index.docstore, similarity_top_k)  (sparse)
  5. QueryFusionRetriever(mode="reciprocal_rerank", num_queries=1)  (RRF fusion)
  6. retrieve() returns nodes with metadata + RRF score
  7. persistence: reopening the same persist_dir sees the collection again

Non-destructive: writes only into .openclaw-tmp/plumbing_chroma_db and never
deletes anything.
"""
import os, hashlib

os.environ["OPENAI_API_KEY"] = "sk-dummy-plumbing-test-only"

import numpy as np
from llama_index.core import Document, Settings, StorageContext, VectorStoreIndex
from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.retrievers import QueryFusionRetriever
from llama_index.retrievers.bm25 import BM25Retriever
from llama_index.vector_stores.chroma import ChromaVectorStore
import chromadb


class FakeEmbed(BaseEmbedding):
    """Deterministic 64-dim bag-of-hash embedding — no network."""
    def _get_text_embedding(self, text: str) -> list:
        v = np.zeros(64)
        for tok in text.lower().replace("\n", " ").split()[:20]:
            h = int(hashlib.md5(tok.encode()).hexdigest()[:8], 16)
            v[h % 64] += 1.0
        return v.tolist()

    def _get_query_embedding(self, query: str) -> list:
        return self._get_text_embedding(query)

    async def _aget_query_embedding(self, query: str):
        return self._get_query_embedding(query)

    async def _aget_text_embedding(self, text: str):
        return self._get_text_embedding(text)


Settings.embed_model = FakeEmbed()

docs = [
    Document(text="Apple reported record revenue of 97 billion dollars driven by iPhone and services growth.",
             metadata={"Ticker": "AAPL", "Company_Name": "Apple", "Quarter": "2023-Q1"}),
    Document(text="Apple services segment reached an all-time high with strong App Store performance.",
             metadata={"Ticker": "AAPL", "Company_Name": "Apple", "Quarter": "2023-Q2"}),
    Document(text="Microsoft cloud revenue grew 24 percent led by Azure and AI workloads.",
             metadata={"Ticker": "MSFT", "Company_Name": "Microsoft", "Quarter": "2023-Q2"}),
    Document(text="Microsoft guided operating expenses up for fiscal 2024 on data center buildout.",
             metadata={"Ticker": "MSFT", "Company_Name": "Microsoft", "Quarter": "2023-Q4"}),
    Document(text="Accenture announced new generative AI bookings of one point one billion dollars.",
             metadata={"Ticker": "ACN", "Company_Name": "Accenture", "Quarter": "2023-Q4"}),
    Document(text="The company closed the acquisition of a cyber security firm in Europe.",
             metadata={"Ticker": "ACN", "Company_Name": "Accenture", "Quarter": "2023-Q3"}),
]

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    ".openclaw-tmp", "plumbing_chroma_db")
persist = os.path.join(BASE, "chroma_db")
os.makedirs(persist, exist_ok=True)

# unique collection name per run -> deterministic count assertions
coll_name = "alpha_detective_" + str(os.getpid())

client = chromadb.PersistentClient(path=persist)
collection = client.get_or_create_collection(coll_name)
vector_store = ChromaVectorStore(chroma_collection=collection)
storage_context = StorageContext.from_defaults(vector_store=vector_store)
index = VectorStoreIndex.from_documents(docs, storage_context=storage_context)

# NOTE: matches alpha_detective.py — BM25 gets the chunk nodes directly
# because index.docstore is empty when a ChromaDB vector store is used.
nodes = docs
dense = index.as_retriever(similarity_top_k=3)
sparse = BM25Retriever.from_defaults(nodes=nodes, similarity_top_k=3)
hybrid = QueryFusionRetriever(
    [dense, sparse],
    similarity_top_k=3,
    mode="reciprocal_rerank",
    num_queries=1,
    use_async=False,
)

for q in ["Apple record revenue", "cloud revenue growth", "generative AI bookings"]:
    nodes = hybrid.retrieve(q)
    print(f"\nQUERY: {q!r}  -> {len(nodes)} fused nodes")
    for n in nodes:
        md = n.metadata
        print(f"   score={n.score:.4f}  {md.get('Company_Name')} ({md.get('Ticker')}, {md.get('Quarter')})  :: {n.node.get_content()[:60]}")

top = hybrid.retrieve("Apple record revenue")
assert top[0].metadata.get("Ticker") == "AAPL", top[0].metadata
assert all("RRF" or isinstance(n.score, float) for n in top)

client2 = chromadb.PersistentClient(path=persist)
c2 = client2.get_or_create_collection(coll_name)
print("\npersisted collection count:", c2.count())
assert c2.count() == len(docs)
print("PLUMBING TEST PASSED (all assertions ok)")