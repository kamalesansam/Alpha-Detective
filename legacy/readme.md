# 🕵️ Alpha-Detective — Hybrid Search RAG Pipeline for Earnings Call Analysis

A production-ready **RAG (Retrieval-Augmented Generation)** web app that
answers questions about earnings call transcripts using **Hybrid Search**:

> **Dense vector search** (OpenAI embeddings → ChromaDB)
> **+** **Sparse BM25** (lexical, `rank-bm25`)
> **→** **Reciprocal Rank Fusion (RRF)** via LlamaIndex `QueryFusionRetriever`
> **→** strict-prompt answer from `gpt-3.5-turbo`

Every answer shows its **Retrieved Context Sources** (Company Name, Ticker,
RRF Score to 4 decimals) so you can verify *why* the model answered the way
it did.

---

## 1. Project layout

```
alpha-detective/
├── NLP_Dataset/              # 51 company folders, 1,185 quarterly transcripts
├── combine_data.py           # Step 2 — builds earnings_transcripts.csv
├── alpha_detective.py        # Step 3 — RAG engine (LlamaIndex + ChromaDB)
├── app.py                    # Step 4 — Streamlit UI
├── requirements.txt          # pinned dependency list
├── setup_env.bat             # (Windows) venv creation + install, one shot
└── README.md
```

## 2. Environment setup (Python 3.12)

```bash
cd alpha-detective

# 1) Create the virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 2) Upgrade the packaging tools FIRST (avoids 3.12 resolution issues)
python -m pip install --upgrade pip setuptools wheel

# 3) Install dependencies
pip install -r requirements.txt
```

> **Python 3.12 note:** `chromadb` needs recent wheels (`chroma-hnswlib`).
> If a build error appears on Windows, upgrade pip first (step 2) and install
> the latest `chromadb` — wheels for 3.12/3.13 are available since 0.5.x.

## 3. Prepare the data

```bash
python combine_data.py
# -> earnings_transcripts.csv  (Ticker, Company_Name, Quarter, Text)
```

`combine_data.py` **auto-detects** the dataset layout:

| Layout | Detected when | Company_Name | Ticker | Quarter |
|---|---|---|---|---|
| Nested (`NLP_Dataset/<Company>/<YYYY_QN_ticker>.txt`) — the attached dataset | subfolders exist | from folder name | from filename (`acn`) | from filename (`2018-Q1`) |
| Flat (`cleaned_ECTs_dataset/*.txt`, one file per company) | files directly in root | from filename | slug fallback | `N/A` |

It also survives **Windows-1252 smart quotes** inside transcripts (UTF-8
decode falls back to cp1252).

## 4. Set your OpenAI API key

Edit `alpha_detective.py` (top of file) — or better, use an environment
variable so the key never lands in code:

```bash
# Windows PowerShell
$env:OPENAI_API_KEY="sk-..."
# Windows cmd
set OPENAI_API_KEY=sk-...
# macOS / Linux
export OPENAI_API_KEY="sk-..."
```

## 5. Launch the app

```bash
streamlit run app.py
```

1. Click **“Load & Index Earnings Data”** in the sidebar (first run embeds
   ~1185 transcripts — allow a few minutes).
2. Type a question, e.g. *“What did Accenture say about generative AI?”*
3. Inspect **📚 Retrieved Context Sources** — RRF scores prove the hybrid
   retriever ranked both dense (semantic) and sparse (keyword) hits.

## CLI smoke test (optional)

```bash
python alpha_detective.py --query "What was Accenture's revenue growth?"
```

## Notes & caveats

- Embeddings/indexing calls the OpenAI API and incurs **cost** on your key.
  `QueryFusionRetriever` is configured with `num_queries=1` so no extra LLM
  query-expansion calls are made — pure dense+sparse RRF.
- The ChromaDB index persists in `./chroma_db`; delete that folder to rebuild.
- If a file/folder is missing the app raises a clear error instead of
  crashing (missing CSV → “run combine_data.py first”; missing API key →
  explicit message).