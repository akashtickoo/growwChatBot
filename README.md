# HDFC Mutual Fund FAQ Assistant

> **Facts-only. No investment advice.**

A small retrieval-augmented (RAG) assistant that answers factual questions about 7 HDFC Mutual Fund schemes, such as expense ratio, exit load, minimum SIP, lock-in, riskometer, benchmark and how to get statements. Every answer is at most 3 sentences, cites exactly one official source, and ends with the date that source was last checked. Questions asking for advice, opinions, returns or comparisons are refused politely, and messages containing personal details (PAN, Aadhaar, account numbers, OTPs, phone numbers, emails) are blocked before any processing.

The UI is a Streamlit app, deployed privately on Streamlit Community Cloud.

## Schemes covered

AMC: **HDFC Mutual Fund**, Direct Growth plans only (the FoFs, not the underlying ETFs).

| Scheme | Category |
|---|---|
| HDFC Large Cap Fund | Large-cap equity |
| HDFC Mid Cap Fund | Mid-cap equity |
| HDFC Small Cap Fund | Small-cap equity |
| HDFC Multi Cap Fund | Multi-cap equity |
| HDFC ELSS Tax Saver Fund | ELSS (tax saving) |
| HDFC Gold ETF Fund of Fund | Fund of Funds (gold) |
| HDFC Silver ETF Fund of Fund | Fund of Funds (silver) |

**Sources:** only official HDFC MF pages and documents on `www.hdfcfund.com` and `files.hdfcfund.com` (each scheme's Direct-plan page, its KIM, and three statement pages), plus fixed SEBI and AMFI investor-education links in refusals. The Groww pages in the problem statement only identify the schemes; they are never fetched or cited.

## How it works

```text
question → PII guard → intent rules → scheme resolver → hybrid search → LLM (strict JSON) → validator → composer
             │             │                │                  │                                    │
          blocked    refusal / redirect  not covered /    nothing relevant                  one citation + footer
                     / scope / greeting  clarify          → "not found"                     from the source registry
```

- **Ingestion (offline):** fetches the official sources (allowlisted hosts, sanity-checked), strips returns, NAV and suitability sections, chunks the text and builds a versioned ChromaDB + BM25 index with `BAAI/bge-small-en-v1.5` embeddings.
- **Fact cards:** the 7 key fields of each scheme are extracted with a verbatim-quote check, reviewed by a person, and indexed as high-priority passages. A card is withdrawn automatically if its source page changes.
- **Answering:** rules handle PII, advice, performance, scope and greetings without the LLM. Factual questions get hybrid retrieval filtered to one scheme, one Groq call (`openai/gpt-oss-120b`, strict JSON schema) and a validator. The validator checks length, links, grounding of every number and date, and advice phrases, with one retry. The citation and date always come from the source registry, never from the model.

Full design: [docs/architecture.md](docs/architecture.md). Build phases and status: [docs/implementation-plan.md](docs/implementation-plan.md). Evaluation: [docs/eval.md](docs/eval.md) and [docs/eval-log.md](docs/eval-log.md).

## Run it locally

Prerequisites: Python 3.12 and [uv](https://docs.astral.sh/uv/). A [Groq](https://console.groq.com/) API key for answers.

```bash
uv sync                                   # install dependencies
cp .env.example .env                      # then set GROQ_API_KEY in .env

uv run python -m ingestion.run            # fetch the official sources and build the index
uv run streamlit run streamlit_app.py     # open http://localhost:8501
```

If `data/` has no index, the app builds one at first start. It also loads the reviewed fact cards from `config/reviewed_fact_cards.yaml`.

Other commands:

| Command | What it does |
|---|---|
| `uv run python -m app.cli ask "What is the exit load of HDFC Mid Cap?"` | One question through the full pipeline, with debug details |
| `uv run python -m ingestion.fact_cards` | Extract fact cards (Groq) for schemes whose sources changed |
| `uv run python -m ingestion.review_facts` | Approve or reject fact cards, then `python -m ingestion.run --index-only` |
| `uv run python -m ingestion.card_export export` | Save the approved cards to `config/reviewed_fact_cards.yaml` for deployments |
| `uv run python -m ingestion.inspect --scheme hdfc-mid-cap` | Look at the indexed passages of a scheme |
| `uv run uvicorn app.main:app` | Optional JSON API: `POST /api/chat`, `GET /api/meta`, `GET /healthz` |
| `uv run pytest` | Tests (no network, no API calls) |
| `uv run python -m eval.run_guards` / `run_retrieval` / `run_validator` | Deterministic evaluation gates |
| `uv run python -m eval.run_generation` | Generation eval with the real model (uses Groq quota) |

## Deploy on Streamlit Community Cloud

1. **Keep the app private.** HDFC MF's website terms allow its content to be used for personal, non-commercial purposes only. Public use needs HDFC AMC's written permission. After deploying, set **Share → "Only specific people can view this app"**.
2. In [share.streamlit.io](https://share.streamlit.io), create an app from this repository. Pick the branch, set the main file to `streamlit_app.py`, and set **Advanced settings → Python 3.12**.
3. In **Advanced settings → Secrets**, paste the contents of [.streamlit/secrets.toml.example](.streamlit/secrets.toml.example) with your real `GROQ_API_KEY`.
4. Deploy. Community Cloud installs from `uv.lock`; on Linux `torch` comes from the CPU-only PyTorch index, so no CUDA libraries are installed.

   The first start fetches the official sources and builds the index, which takes a minute or more. Until then a spinner shows. The sidebar shows the index version once it is loaded.

5. Smoke test: the three example questions, one advisory question ("Should I invest in HDFC Mid Cap?"), one message with a fake PAN.

Data is never committed: `data/` (raw pages, PDFs, index, registry) is gitignored. A deployment builds its own index from the live sources. A reviewed card whose source page has changed since review is withdrawn until it is reviewed again.

## Refreshing the data (monthly)

1. `uv run python -m ingestion.run` and read the run report in `data/reports/`.
2. `uv run python -m ingestion.fact_cards`, then `uv run python -m ingestion.review_facts` for the changed cards.
3. `uv run python -m ingestion.run --index-only` and `uv run python -m ingestion.card_export export`.
4. Commit `config/reviewed_fact_cards.yaml`, then reboot the Streamlit app so it rebuilds from the current sources.

## Configuration

All settings are environment variables (`.env` locally, Secrets on Streamlit Cloud); see [.env.example](.env.example). The main ones:

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | — | Needed for factual answers; templates work without it |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Answer and extraction model |
| `MIN_SIMILARITY` | `0.63` | Below this, "not found" without an LLM call |
| `RATE_LIMIT` / `GLOBAL_RATE_LIMIT` | `20/minute` / `60/minute` | Per session / per process |
| `APP_ENV` | `development` | `production` blocks question logging |

## Known limitations

- **Personal, non-commercial use only** under HDFC MF's terms, so the app stays private until HDFC AMC gives written permission.
- **Groq quota:** the free key allows about 8,000 tokens per minute, roughly 4 answers per minute. Beyond that, answers wait for the quota; a paid tier or `GROQ_FALLBACK_MODEL` helps.
- **Data freshness:** values are as fresh as the last fetch. Most values on the pages carry no date, so the footer shows when the source was last checked. NAV and returns are never quoted; performance questions link the official factsheet.
- **Source changes:** a changed page withdraws its reviewed cards until they are reviewed again, and a page that fails its sanity check is left out. Since 13 Nov 2026 the live Silver FoF page shows no benchmark, so a fresh build leaves it out and Silver FoF questions get "not found" with a link to its page until HDFC MF fixes the page.
- **One scheme per answer:** questions about several schemes get a request to ask one at a time (one citation per answer).
- **Rule-based checks:** advice detection and PII patterns are rules. They can refuse some harmless questions and miss disguised personal details ("name at gmail dot com").
- **English only.** Single-turn: follow-ups like "what about its lock-in?" need the scheme named again.

More in [docs/architecture.md §18](docs/architecture.md) and [docs/edge-case.md](docs/edge-case.md).

## Repository layout

```text
streamlit_app.py      Streamlit entry point
app/                  pipeline, guards, retrieval, LLM step, composer, Streamlit UI, optional API
ingestion/            fetch, parse, chunk, embed, index, fact cards, review, card export
config/               schemes, sources, templates, UI text, reviewed fact cards, education links
eval/                 evaluation sets and runners
tests/                pytest (synthetic fixtures only)
docs/                 problem statement, architecture, plan, edge cases, evaluation
```

---

**Disclaimer:** Facts-only. No investment advice. This assistant shares objective information from official HDFC Mutual Fund documents. It does not recommend, compare or predict, and nothing it says is investment advice. Mutual fund investments are subject to market risks; read all scheme-related documents carefully.
