# Validation Issues Catalog

Based on `validation_output2.log` (16m 30s run with CSV loading)

## Summary

- **PASS**: 12 tables
- **FAIL**: 12 tables

## Root Cause Classification

| Category | Cause | Fix |
|----------|-------|-----|
| **HISTOGRAM SAMPLING** | MySQL extrapolates `num_distinct` when sampling < 100% | Increase `histogram_generation_max_mem_size` to 6GB |
| **ALGORITHM LIMITATION** | Inherent trade-offs in generation approach | Accept or redesign algorithm |
| **SMALL TABLE** | Histograms unsuitable for <100 rows | Use deterministic generation |

See `HISTOGRAM_SAMPLING_EXPLAINED.md` for detailed explanation of the sampling issue.

---

## Issue Categories

---

### 1. Non-FK _SK Columns Over-generating

**Root Cause**: HISTOGRAM SAMPLING (16% sampling → 24% over-estimate)

**Affected Tables**: catalog_sales

**Description**: Integer columns use histogram-based generation. With only 16% sampling, MySQL extrapolates `num_distinct` by ~6x, causing massive over-estimation.

| Table | Column | Source | Replay | Diff | Sampling Rate |
|-------|--------|--------|--------|------|---------------|
| catalog_sales | cs_bill_cdemo_sk | 153,252 | 190,436 | +24% | 16% |
| catalog_sales | cs_ship_cdemo_sk | 153,351 | 190,622 | +24% | 16% |
| catalog_sales | cs_bill_customer_sk | 79,642 | 93,601 | +18% | 16% |
| catalog_sales | cs_ship_customer_sk | 79,676 | 93,642 | +18% | 16% |
| catalog_sales | cs_bill_addr_sk | 48,000 | 52,898 | +10% | 16% |
| catalog_sales | cs_ship_addr_sk | 47,962 | 52,756 | +10% | 16% |
| catalog_sales | cs_sold_time_sk | 67,462 | 78,042 | +16% | 16% |

**Evidence**:
```
Histogram sum of num_distinct: 190,436
Actual COUNT(DISTINCT): 153,252
Over-estimate: +24% (matches extrapolation error)
```

**Fix**: Increase `histogram_generation_max_mem_size` to 6GB for 100% sampling.

---

### 2. Dimension Table Date Columns Under-generating

**Root Cause**: HISTOGRAM SAMPLING + BIRTHDAY PARADOX

**Affected Tables**: date_dim, item, web_page, web_site, store

| Table | Column | Source | Replay | Diff | Issue |
|-------|--------|--------|--------|------|-------|
| date_dim | d_date | 73,049 | 46,303 | -37% | Birthday paradox in bucket generation |
| item | i_rec_end_date | 4 | 3 | -25% | Small table effect |
| item | i_rec_start_date | 5 | 4 | -20% | Small table effect |
| web_page | wp_rec_end_date | 4 | 3 | -25% | Small table effect |
| web_site | web_rec_end_date | 4 | 3 | -25% | Small table effect |
| store | s_rec_end_date | 3 | 1 | -67% | Small table effect |

**Root Cause** (date_dim.d_date):
- 73,049 rows with 73,049 distinct dates (1:1 ratio)
- ~100 histogram buckets, each spanning ~730 days
- Random generation within buckets → birthday paradox → ~63% coverage

                                                                                                                                                         
  The math:                               
  - 73,049 rows, each picking a random day from 73,049 possible days                                      
  - Birthday paradox: when N rows select randomly from N values                                                 
  - Expected unique ≈ N × (1 - e^(-1)) ≈ 63.2%                                                       
  - 73,049 × 0.632 = 46,167 (matches observed 46,177!)   

**Fix**:
- For 1:1 columns (rows = distinct), use `rownum` directly as offset
- For low-cardinality dates, enumerate explicitly
- Increase sampling may partially help

---

### 3. Composite PK Order Numbers Over-generating

**Root Cause**: ALGORITHM (integer division rounding)

**Affected Tables**: store_sales, catalog_sales, web_sales

| Table | Column | Source | Replay | Diff |
|-------|--------|--------|--------|------|
| store_sales | ss_ticket_number | 75,807 | 79,967 | +5.5% |
| catalog_sales | cs_order_number | 160,000 | 169,581 | +6.0% |
| web_sales | ws_order_number | 59,998 | 65,205 | +8.7% |

**Root Cause**:
- Uses `div(rownum-1, rows_per_value) + min_val` for grouping
- `rows_per_value = total_rows / distinct_count` has fractional remainder
- Integer division produces slightly more groups than expected

**Fix**: Adjust calculation or use `mod(rownum-1, source_distinct_count)`

**Note**: NOT a histogram sampling issue - these columns use PK-specific logic.

---

### 4. Composite FK+PK Trade-off (Inherent Limitation)

**Root Cause**: ALGORITHM (odometer pattern constraint)

**Affected Tables**: inventory

| Table | Column | Source | Replay | Diff |
|-------|--------|--------|--------|------|
| inventory | inv_item_sk | 18,000 | 9,000 | -50% |
| inventory | inv_date_sk | 261 | 261 | 0% |
| inventory | inv_warehouse_sk | 5 | 5 | 0% |

**Root Cause**:
- Source is 50% sparse (11.7M rows vs 23.5M possible combinations)
- Odometer generates dense combinations
- Cannot match all three dimensions simultaneously

**Status**: Documented in `COMPOSITE_FK_PK_CARDINALITY.md`. This is an inherent trade-off, not a bug.

---

### 5. Small Table Histogram Issues

**Root Cause**: SMALL TABLE (histograms unsuitable for <100 rows)

**Affected Tables**: income_band (20 rows), store (3 rows), warehouse (5 rows), web_page (59 rows), web_site (25 rows)

| Table | Rows | Column | Source | Replay | Diff |
|-------|------|--------|--------|--------|------|
| income_band | 20 | ib_lower_bound | 20 | 13 | -35% |
| income_band | 20 | ib_upper_bound | 20 | 13 | -35% |
| store | 3 | s_market_id | 3 | 2 | -33% |
| store | 3 | s_number_employees | 3 | 2 | -33% |
| warehouse | 5 | w_warehouse_sq_ft | 5 | 3 | -40% |
| web_page | 59 | wp_char_count | 42 | 29 | -31% |
| web_page | 59 | wp_customer_sk | 18 | 12 | -33% |
| web_site | 25 | web_mkt_id | 6 | 5 | -17% |
| web_site | 25 | web_close_date_sk | 10 | 9 | -10% |

**Root Cause**:
- With ~20 rows and ~20 buckets, each bucket has ~1 value
- Random selection from buckets → birthday paradox → ~63% coverage
- Histogram approach fundamentally wrong for tiny tables

**Fix**: For tables < 100 rows, skip histogram, use `rownum` or enumerate values directly.

**Note**: Increasing sampling to 6GB will NOT fix these - the issue is the histogram approach itself.

---

### 6. FK+PK Item Column Over-generating

**Root Cause**: ALGORITHM (uses reference table count, not source distinct)

**Affected Tables**: web_returns

| Table | Column | Source | Replay | Diff |
|-------|--------|--------|--------|------|
| web_returns | wr_item_sk | 16,805 | 18,000 | +7% |

**Root Cause**:
- Expression: `mod(rownum-1, 18000)+1` cycles through ALL 18,000 items
- Source only has returns for 16,805 items (93% coverage)

**Fix**: Use source distinct count as modulus: `mod(rownum-1, 16805)+1`

---

## Summary: What Fixes What

| Fix | Issues Resolved |
|-----|-----------------|
| **Increase sampling to 6GB** | #1 Non-FK _SK over-generating (24% errors) |
| **Deterministic date generation** | #2 date_dim.d_date under-generating |
| **Adjust div/mod calculation** | #3 Order number over-generating |
| **Use source distinct for FK+PK** | #6 web_returns item over-generating |
| **Skip histogram for small tables** | #5 Small table issues |
| **Accept trade-off** | #4 Inventory FK+PK (inherent) |

## Current Sampling Rates

| Table | Sampling Rate | Memory for 100% |
|-------|---------------|-----------------|
| catalog_sales | 16% | 6GB |
| inventory | 39% | 2.6GB |
| store_sales | 46% | 2.4GB |
| customer_demographics | 52% | 1.9GB |
| web_sales | 60% | 1.7GB |

**Current setting**: 1GB
**Required for 100%**: 6GB
