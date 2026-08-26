"""
app.py — Alpha-Detective Streamlit Web App
==========================================

A hybrid-search RAG chatbot for earnings call transcripts.

Run with::

    streamlit run app.py
"""

from __future__ import annotations

import time

import streamlit as st

# Importing the module does not call OpenAI — the API key is only used when a
# retriever is actually built (the "Load & Index Earnings Data" button).
from alpha_detective import (
    DEFAULT_CSV,
    build_hybrid_retriever,
    load_data,
    query_system,
)

# --------------------------------------------------------------------------- #
# Page configuration (wide layout, spy emoji 🕵️)
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="Alpha-Detective 🕵️",
    page_icon="🕵️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --------------------------------------------------------------------------- #
# Session state: cache the retriever + flag so we never rebuild the index per
# query (building the index takes minutes for 50+ real transcripts).
# --------------------------------------------------------------------------- #
if "data_loaded" not in st.session_state:
    st.session_state.data_loaded = False
if "retriever" not in st.session_state:
    st.session_state.retriever = None


# --------------------------------------------------------------------------- #
# Sidebar — load & index
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.title("🕵️ Alpha-Detective")
    st.caption("Hybrid Search RAG — Earnings Call Analysis")

    col_a, col_b = st.columns(2)
    col_a.metric("Docs loaded", "✅" if st.session_state.data_loaded else "⏳")
    col_b.metric("Retriever", "RRF 📡" if st.session_state.retriever else "—")

    if st.button(
        "Load & Index Earnings Data",
        type="primary",
        use_container_width=True,
        help="Reads earnings_transcripts.csv, embeds with OpenAI, builds "
        "ChromaDB dense index + BM25 sparse index, fuses with RRF.",
    ):
        with st.spinner(
            "⏳ Loading 50 earnings transcripts and building the hybrid "
            "ChromaDB + BM25 index — this can take a few minutes..."
        ):
            try:
                t0 = time.time()
                docs = load_data(DEFAULT_CSV)
                st.session_state.retriever = build_hybrid_retriever(
                    docs, reset_collection=True
                )
                st.session_state.data_loaded = True
                st.success(
                    f"✅ Indexed {len(docs):,} chunks "
                    f"in {time.time() - t0:.1f}s. "
                    "Hybrid retriever (dense + BM25, RRF) is live."
                )
            except FileNotFoundError as exc:
                st.error(f"❌ {exc}")
                st.info("Run `python combine_data.py` first to build the CSV.")
            except RuntimeError as exc:
                st.error(f"❌ {exc}")
            except Exception as exc:  # pragma: no cover - defensive catch-all
                st.error(f"❌ Unexpected error while indexing: {exc}")

    if st.session_state.data_loaded:
        st.info(
            "Hybrid search = **dense** (OpenAI embeddings in ChromaDB) + "
            "**sparse** (BM25), fused with **Reciprocal Rank Fusion**."
        )


# --------------------------------------------------------------------------- #
# Main area — query
# --------------------------------------------------------------------------- #
st.subheader("🔍 Question an earnings call")
st.caption(
    "Examples: “What did Accenture say about AI bookings?” · "
    "“Revenue growth for Apple?” · “Any mention of supply chain issues?”"
)

query = st.text_input(
    "Your financial query",
    placeholder="Ask anything about the 50 companies' earnings calls...",
    label_visibility="collapsed",
)

if query:
    if not st.session_state.data_loaded:
        st.warning(
            "⚠️ No index yet. Click **“Load & Index Earnings Data”** in the "
            "sidebar first."
        )
    else:
        try:
            with st.spinner("🕵️ Investigating..."):
                t0 = time.time()
                answer, nodes = query_system(query, st.session_state.retriever)
            elapsed = time.time() - t0

            # --- The LLM's answer in a styled block -------------------------
            st.markdown("### 🧠 Answer")
            st.markdown(
                f"<div style='background:#0e1117;border:1px solid #2a6df4;"
                f"border-left:6px solid #2a6df4;border-radius:10px;padding:18px;"
                f"font-size:1.05rem;line-height:1.6'>{answer}</div>",
                unsafe_allow_html=True,
            )
            st.caption(f"Generated in {elapsed:.1f}s · top-{len(nodes)} fused sources")

            # --- Retrieved context sources (proof of hybrid search) ----------
            with st.expander("📚 Retrieved Context Sources", expanded=False):
                st.caption(
                    "Score = **RRF score** (Reciprocal Rank Fusion of dense "
                    "vector + BM25 ranks). Higher is better."
                )
                if nodes:
                    rows = [
                        {
                            "Company Name": n.metadata.get("Company_Name", "?"),
                            "Ticker": n.metadata.get("Ticker", "?"),
                            "Quarter": n.metadata.get("Quarter", "?"),
                            "RRF Score": f"{n.score:.4f}",
                        }
                        for n in nodes
                    ]
                    st.dataframe(rows, use_container_width=True, hide_index=True)
                    with st.expander("Show source snippets"):
                        for i, n in enumerate(nodes, 1):
                            st.markdown(
                                f"**{i}.** "
                                f"{n.metadata.get('Company_Name', '?')} · "
                                f"{n.metadata.get('Ticker', '?')} · "
                                f"{n.metadata.get('Quarter', '?')}"
                            )
                            snippet = n.node.get_content()[:400].replace("\n", " ")
                            st.markdown(f"> {snippet}…")
                else:
                    st.markdown("*No sources retrieved.*")
        except RuntimeError as exc:
            st.error(f"❌ {exc}")
        except Exception as exc:  # pragma: no cover - defensive catch-all
            st.error(f"❌ Unexpected error while querying: {exc}")
else:
    st.markdown("---")
    st.markdown(
        "**How it works:** the app loads `earnings_transcripts.csv` "
        "(built by `combine_data.py`), embeds every chunk with "
        "`text-embedding-3-small`, stores vectors in a persistent **ChromaDB**, "
        "runs **BM25** over the same chunks, and fuses both rank lists with "
        "**Reciprocal Rank Fusion** before answering via `gpt-3.5-turbo`."
    )