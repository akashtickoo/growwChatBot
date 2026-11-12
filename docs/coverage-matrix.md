# Coverage Matrix

> **Disclaimer:** Facts-only. No investment advice.

Where each fact for each scheme comes from in the official HDFC Mutual Fund sources (implementation plan Phase 1, [eval.md](eval.md) EV-1-06 and EV-1-07).

- **Read on:** 11 Nov 2026, from the Direct-plan scheme pages and the KIMs listed in [config/sources.yaml](../config/sources.yaml).
- **Status:** values read by script from saved copies. **A person must still check 10 random cells against the live pages (EV-1-07)** before these values are used as evaluation labels.

## 1. Fields by scheme

Values are from the scheme page unless marked **(K)**, which means the KIM dated 21 Nov 2025.

| Scheme | Expense ratio (TER) | Exit load | Min SIP | Min lumpsum | Lock-in | Riskometer | Benchmark |
|---|---|---|---|---|---|---|---|
| HDFC Large Cap Fund | 1.00% | 1.00% if redeemed within 1 year of allotment; nil after | ₹100 | ₹100, any amount thereafter (K) | NA | Very High | NIFTY 100 (Total Return Index) |
| HDFC Mid Cap Fund | 0.74% | 1.00% if redeemed within 1 year of allotment; nil after | ₹100 | ₹100 (page FAQ); ₹100, any amount thereafter (K) | NA | Very High | NIFTY Midcap 150 Index (Total Returns Index) |
| HDFC Small Cap Fund | 0.79% | 1.00% if redeemed within 1 year of allotment; nil after | ₹100 | Rs.100/- and any amount thereafter (page FAQ, K) | NA | Very High | BSE 250 SmallCap Index (Total Returns Index) |
| HDFC Multi Cap Fund | 0.95% | 1.00% if redeemed within 1 year of allotment; nil after | ₹100 | ₹100, any amount thereafter (K) | NA | Very High | NIFTY500 MultiCap 50:25:25 (TRI) |
| HDFC ELSS Tax Saver Fund | 1.19% | NIL | ₹500 | ₹500, in multiples of ₹500 (page FAQ, K) | 3 years | Very High | NIFTY 500 Total Returns Index |
| HDFC Gold ETF Fund of Fund | 0.20% | 1% if redeemed within 15 days of allotment; nil on or after 15 days | ₹100 | ₹100, any amount thereafter (K) | NA | High | Domestic Price of Physical Gold |
| HDFC Silver ETF Fund of Fund | 0.21% | 1% if redeemed within 15 days of allotment; nil on or after 15 days | ₹100 | ₹100, any amount thereafter (K) | NA | Very High | Domestic Prices of physical Silver (derived as per regulatory norms) |

All 49 cells are filled. No field is missing from the official sources.

## 2. Where each field appears

| Field | Scheme page section | Backup | Notes |
|---|---|---|---|
| `expense_ratio` | "TER" box, value after "Click here to view the Total Expense Ratio" | KIM / SID | **No as-of date** on the page, so the footer's fetch date is the only date (D2). Check that the value is the Direct plan's. |
| `exit_load` | "Exit Load" section (bullet points; `h3` tags on the Multi Cap page) | KIM | Corrected in Phase 2: the Gold FoF page does include the "nil on or after 15 days" part, which the Phase 1 script missed. |
| `min_sip` | "Min SIP" box | KIM | |
| `min_lumpsum` | FAQ "Can I invest in SIP and Lump Sum…" (Mid Cap, ELSS) or "What is the minimum investment…" (Small Cap) | **KIM "Minimum Application Amount" table** (all 7) | The KIM table is multi-column and comes out jumbled as plain text. Phase 2 must parse it as a table. |
| `lock_in` | "Lock in" box | SID | "NA" on the six non-ELSS pages; "3 years" on ELSS. Answer "NA" as "the scheme page lists no lock-in period", never as an inferred fact (EC-FLD-01). |
| `riskometer` | "Riskometer" box near the top | Product Labelling section | The page also shows a **Benchmark** Riskometer. Use the scheme's, not the benchmark's. |
| `benchmark` | "Benchmark" box (full name on the line after the shortened one) | SID | |

## 3. Other questions

| Question | Source | Covered? |
|---|---|---|
| How to download an account statement | [Request Statement](https://www.hdfcfund.com/services/additional-info/request-statement), [Consolidated Account Statement](https://www.hdfcfund.com/services/consolidated-account-statement) | Yes |
| How to get a capital gains statement | [How to Get a Capital Gain Statement](https://www.hdfcfund.com/learn/blog/how-get-capital-gain-statement-mutual-fund-schemes-india) (last updated 3 Jul 2026) | Yes |
| Performance / returns (link only, no figures) | Scheme's "Fund Facts" PDF (5 equity schemes, October 2026 edition); [Factsheets](https://www.hdfcfund.com/mutual-funds/factsheets) page for the two FoFs, which have no Fund Facts PDF | Yes |
| Fund size (AUM) | Scheme page "AUM (31/10/2026)" | Has an as-of date, so answerable under §2.1 |
| NAV | Scheme page shows "NA" | Not answered (§2.1) |

None of the 7 scheme pages explains statements or capital gains. Those questions are answered from the service pages above.

## 4. Dates found in the sources

| Source | Date shown |
|---|---|
| Scheme pages: TER, exit load, min SIP, lock-in, riskometer, benchmark | None |
| Scheme pages: AUM | 31/10/2026 |
| Fund Facts PDFs | October 2026 (in the file name) |
| KIMs and SIDs | 21 Nov 2025 (in the file name) |
| Capital gain statement guide | Last updated 3 Jul 2026 |

## 5. Page sections Phase 2 must strip

These appear on every scheme page and would break the facts-only rules if indexed:

- "Returns since inception" figure, "NAV and Historical Performance", "Benchmark Performance" and "Value of Investment of Rs 10,000" (performance, EC-PERF-07)
- "Ideal for" (e.g. "Wealth Creation, 3 Years and above") and "Product Suitability" ("This product is suitable for investors who…") (suitability, EC-INT-06)
- "INVEST NOW" and other calls to action

## 6. Human check (EV-1-07)

A second person picks 10 random cells from section 1, opens the live page or KIM, and ticks the result.

| # | Scheme | Field | Matches live source? |
|---|---|---|---|
| 1 | | | ☐ |
| 2 | | | ☐ |
| 3 | | | ☐ |
| 4 | | | ☐ |
| 5 | | | ☐ |
| 6 | | | ☐ |
| 7 | | | ☐ |
| 8 | | | ☐ |
| 9 | | | ☐ |
| 10 | | | ☐ |

When all 10 match, set `verified_by` and `verified_on` in [eval/fact_labels.yaml](../eval/fact_labels.yaml).
