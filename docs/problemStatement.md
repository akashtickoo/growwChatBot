# Problem Statement: Mutual Fund FAQ Assistant (Facts-Only Q&A)

> **Disclaimer:** Facts-only. No investment advice.

## Overview

Build a **facts-only FAQ assistant** for mutual fund schemes, using **Groww** as the reference product context. The assistant answers objective, verifiable questions about mutual funds by retrieving information **only from official public sources** (AMC websites, AMFI, SEBI).

It must never give investment advice, opinions or recommendations. Every response carries a single, clear source link and follows the clarity, accuracy and compliance constraints below.

The guiding principle: **accuracy over intelligence**. Users should get verified, source-backed information with no advisory bias or speculation.

## Objective

Design and implement a lightweight **Retrieval-Augmented Generation (RAG)** assistant that:

- Answers factual queries about mutual fund schemes
- Uses a curated corpus of official documents
- Returns concise, source-backed responses

## Target Users

- Retail investors comparing mutual fund schemes
- Customer support and content teams handling repetitive mutual fund queries

## Scope of Work

### 1. Corpus Definition

- **AMC:** HDFC Mutual Fund
- **Schemes (7, category-diverse):**

| Scheme | Category | Reference page |
|---|---|---|
| HDFC Large Cap Fund – Direct Growth | Large-cap equity | https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth |
| HDFC Mid Cap Fund – Direct Growth | Mid-cap equity | https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth |
| HDFC Small Cap Fund – Direct Growth | Small-cap equity | https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth |
| HDFC Multi Cap Fund – Direct Growth | Multi-cap equity | https://groww.in/mutual-funds/hdfc-multi-cap-fund-direct-growth |
| HDFC ELSS Tax Saver Fund – Direct Plan Growth | ELSS (tax saving) | https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth |
| HDFC Gold ETF Fund of Fund – Direct Plan Growth | Fund of Funds (Gold) | https://groww.in/mutual-funds/hdfc-gold-etf-fund-of-fund-direct-plan-growth |
| HDFC Silver ETF Fund of Fund – Direct Growth | Fund of Funds (Silver) | https://groww.in/mutual-funds/hdfc-silver-etf-fof-direct-growth |

### 2. FAQ Assistant Requirements

**Supported factual queries (examples):**

- Expense ratio of a scheme
- Exit load details
- Minimum SIP amount
- ELSS lock-in period
- Riskometer classification
- Benchmark index
- How to download statements or capital gains reports

**Every answer must:**

- Be **at most 3 sentences**
- Include **exactly one** citation link
- End with the footer: `Last updated from sources: <date>`

### 3. Refusal Handling

The assistant must refuse non-factual or advisory queries, for example:

- "Should I invest in this fund?"
- "Which fund is better?"

**Refusals must:**

- Be polite and clearly worded
- Restate the facts-only limitation
- Include a relevant educational link (e.g. an AMFI or SEBI investor-education resource)

### 4. User Interface (Minimal)

A simple interface with:

- A welcome message
- Three example questions
- A visible disclaimer: **"Facts-only. No investment advice."**

## Constraints

### Data and Sources

- Use **only** official public sources: AMC, AMFI, SEBI
- Do **not** use third-party blogs or aggregator websites

### Privacy and Security

Do not collect, store or process:

- PAN or Aadhaar numbers
- Account numbers
- OTPs
- Email addresses or phone numbers

### Content Restrictions

- No investment advice or recommendations
- No performance comparisons or return calculations
- For performance questions, link to the **official factsheet only**

### Transparency

- Responses are short, factual and verifiable
- Every answer includes a source link and a last-updated date

## Expected Deliverables

1. **README** covering:
   - Setup instructions
   - Selected AMC and schemes
   - Architecture overview (RAG approach)
   - Known limitations
2. **Disclaimer snippet:** "Facts-only. No investment advice."

## Success Criteria

- Accurate retrieval of factual mutual fund information
- Strict adherence to facts-only responses
- Consistent inclusion of valid source citations
- Correct refusal of advisory queries
- Clean, minimal, user-friendly interface

## Open Questions

- **Source policy vs. corpus links:** The scheme list points to `groww.in` pages, but the source constraint excludes aggregator websites. Decide whether Groww pages are used only to identify the schemes, with answers and citations drawn from HDFC MF (scheme information documents, KIM, factsheets), AMFI and SEBI, or whether Groww counts as an allowed source because it is the reference product.
  **Resolved:** the 7 Groww scheme pages are the corpus and the citation for every answer ([architecture.md](architecture.md) D1).
- **"Last updated" date:** Define whether this is the crawl/ingestion date of the corpus or the publication date of the cited document.
  **Resolved:** the date the cited page was last fetched and checked, shown as the India date. A value's own as-of date is stated in the answer when the page shows one ([architecture.md](architecture.md) D2).
