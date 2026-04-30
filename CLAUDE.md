# DataGenX Generator

Synthetic data generation and validation framework for TPC-DS and TPC-H benchmark datasets.

## Purpose

Generate synthetic database data that matches the **statistical properties** (histograms, distinct counts, cardinality) of original data. This allows testing query optimization strategies without using actual proprietary data.

## ⚠️ CRITICAL: Data Privacy Requirements

**This is a general-purpose tool, NOT specific to any benchmark schema.**

The generated data must be **completely blind to actual source data values**. This is critical for customer deployments where source databases contain sensitive/proprietary data.

### What We CAN Use (Statistical Patterns Only)
- **Distribution shapes**: Relative frequencies/weights from histograms
- **Cardinality counts**: Number of distinct values
- **Column metadata**: Data types, lengths, constraints

### What We MUST NOT Use (Data Leakage)
- ❌ **Actual data values** from histogram buckets (dates, numbers, strings)
- ❌ **MIN/MAX values** from source data
- ❌ **Copying rows** from source tables
- ❌ **Sampling actual values** from source

### Correct Approach
- Generate **synthetic values** that follow the same distribution patterns
- For dates: Use synthetic date ranges with correct distribution shape
- For numbers: Use synthetic numeric ranges with correct distribution shape
- For strings: Already correct - uses synthetic strings like `column_1`, `column_2`
- For FKs: Reference synthetically generated PK values from target schema (not source)

### Data Leak Fixes Applied
1. ✅ `_try_sparse_date_expression()`: Now uses synthetic sequential dates (2000-01-01 + N)
2. ✅ `_build_dense_date_expression()`: Now uses synthetic base date with span only
3. ✅ `histogram_to_case()`: Now uses synthetic sequential values
4. ✅ FK expressions: Use COUNT(DISTINCT) for decisions, sample from target schema (not source)

## Directory Structure

```
~/work/db/datagenx/
├── datagenx_generator/datagenx_generator/   # Main generator code (this directory)
├── tpcds/tpcds-kit/                         # TPC-DS benchmark kit
│   ├── tools/                               # dsdgen, dsqgen utilities
│   ├── query_templates/                     # TPC-DS query templates
│   └── specification/                       # TPC-DS spec documents
└── tpch/tpch-dbgen/                         # TPC-H benchmark kit
    ├── dbgen                                # TPC-H data generator binary
    ├── queries/                             # TPC-H query files
    └── *.tbl                                # Generated data files
```

## Key Files

| File | Purpose |
|------|---------|
| `MasterRun.py` | **Entry point**. Orchestrates full pipeline: generate templates → run dbgen → populate → validate |
| `GenerateDbgen.py` | Library functions for generating `.dbgen` template files from schema + histograms |
| `PopulateNewTableAndValidate.py` | Creates replay tables and compares stats (single table) |
| `ValidateTableStats.py` | Multi-table validation with enhanced categorization |
| `run_tpch_comparison.py` | Compares EXPLAIN plans between orig and replay |
| `dbgen_files/` | Generated `.dbgen` template files |
| `dbgen_tmp_out/` | Generated SQL (schema + INSERT statements) |

## Architecture - Code Flow

**IMPORTANT**: Always trace from MasterRun.py to understand actual execution paths.

```
MasterRun.py (entry point)
│
├── step_a_generate_dbgen(table)
│   ├── build_fk_appendages()          # Generates FK expressions (lines 128-333)
│   │   └── Returns dict: {col: expression} for each FK column
│   │
│   └── annotate_table_with_histogram(generated_appendages=appendages)
│       └── From GenerateDbgen.py
│       └── For FK columns: uses generated_appendages[col] if present
│       └── Otherwise: uses its own FK logic (BUT THIS IS NEVER REACHED for FKs)
│
├── step_b_run_dbgen()                  # Runs dbgen binary
│
└── step_c_create_and_validate()        # Creates table, inserts, validates
```

**Key insight**: MasterRun.py's `build_fk_appendages()` generates ALL FK expressions before calling GenerateDbgen.py. Any FK logic in GenerateDbgen.py is effectively dead code when running via MasterRun.py.

## FK Expression Generation

Three approaches for FK columns (determined by `build_single_fk_expression()` in GenerateDbgen.py):

| Approach | When Used | Expression |
|----------|-----------|------------|
| **Singleton Sparse** | Singleton histogram (low cardinality) | Weighted CASE with sampled values from target |
| **Equi-height Sparse** | Equi-height histogram with <20% coverage | Weighted CASE with ranges from target |
| **Dense** | High coverage (>10% of referenced table) | `rand.range(min, max+1)` |

**Key decision logic**: Uses `COUNT(DISTINCT col)` from source (actual count) rather than histogram estimates, because histogram `num_distinct` can be off by 10-50x due to sampling. See "Why We Use COUNT(DISTINCT)" section above.

**Important**: We only treat columns as FKs if they are declared in the DDL. We do not infer FK relationships based on naming conventions (e.g., columns ending in `_sk` or `_id`).

## Workflow

1. **Extract metadata** from source schema (MySQL `information_schema.column_statistics`)
2. **Generate `.dbgen` templates** with expressions for each column type:
   - Primary Keys: `rownum` or `rownum-1`
   - Foreign Keys: Dense or sparse approach based on histogram type
   - Date columns: `TIMESTAMP 'min' + INTERVAL rand.range(0, span) DAY`
   - Numeric: Weighted CASE expressions from histogram buckets
3. **Run dbgen binary** to produce INSERT statements
4. **Populate replay schema** and run `ANALYZE TABLE`
5. **Compare statistics** between orig and replay

## Terminology

- **orig**: Original/source data with real distributions
- **replay**: Generated synthetic data that should match orig's statistics
- **diverged**: When replay statistics differ from orig beyond threshold (typically 5%)

## Common Commands

```bash
# Full pipeline
python3 MasterRun.py

# Compare TPC-H query plans
./run_tpch_comparison.sh

# Single table validation
python3 PopulateNewTableAndValidate.py --source-schema=tpcds --table=web_site
```

## Why We Use COUNT(DISTINCT) Instead of Histogram Estimates

### The Problem

MySQL equi-height histograms contain a `num_distinct` field in each bucket that estimates the number of distinct values in that bucket's range. Summing these gives an estimated total distinct count. However, **this estimate can be wildly inaccurate**.

Example observed in practice:
```
Column: ss_item_sk (FK to item table)
Histogram estimated_distinct: ~500
Actual distinct count: 18,000
Error: 36x underestimate
```

### Why Histogram Estimates Are Unreliable

1. **Sampling During ANALYZE TABLE**: MySQL doesn't scan the entire table when building histograms. It samples a subset of pages/rows. For a column with 18,000 distinct values spread across millions of rows, the sample may only encounter a fraction of those values.

2. **Bucket Granularity**: With ~100 buckets (MySQL's default), each bucket's `num_distinct` is estimated from whatever values the sampler encountered within that bucket's range—not computed exactly.

3. **Sample Size Math**: If MySQL samples 10,000 rows from a 2.8M row table (0.35% sample rate), and 18,000 distinct items exist, the sample might only see 500-1,000 of them due to probability distribution.

### Why This Matters for Generation

When deciding between sparse vs dense FK approaches, we need to know: "Does this FK column use a small subset of the referenced table, or most of it?"

| Decision Input | Sparse Triggers | Dense Triggers |
|----------------|-----------------|----------------|
| Histogram estimate (500/18000 = 2.8%) | ✓ Low coverage | |
| Actual count (18000/18000 = 100%) | | ✓ High coverage |

Using the wrong input leads to wrong decisions:
- **Item FKs**: Histogram said 500 → triggered sparse → generated 500 values → validation showed 97% divergence
- **Date FKs**: Should trigger sparse (1800 out of 73000) but didn't due to other threshold issues

### Why Validation Uses Actual Counts

Validation compares **index cardinality** (from `SHOW INDEX`) which reflects the true distinct count, not histogram estimates. This is why validation correctly reports `orig=18000, replay=500`.

### The Solution

For FK generation decisions, we query `COUNT(DISTINCT column)` from the source table. This:

1. **Gives accurate counts**: No sampling artifacts
2. **Maintains privacy**: A count doesn't expose actual values—it's purely statistical metadata
3. **Aligns with validation**: We generate based on what validation will measure
4. **Ensures query plan equivalence**: The optimizer uses both histogram distributions AND index cardinality for planning

### Privacy Consideration

`COUNT(DISTINCT)` returns only an integer count. It does not reveal:
- Actual data values
- MIN/MAX values
- Any row-level information

This is consistent with our privacy requirement: we use **statistical patterns** (counts, distributions) but never actual data values.

## Histogram Sampling Requirement

MySQL histograms use **sampling** when data exceeds `histogram_generation_max_mem_size` (default 20MB). This causes `bucket[3]` (num_distinct) values to be inaccurate estimates.

**MasterRun.py automatically regenerates histograms** at startup with:
- `histogram_generation_max_mem_size = 500MB`
- Verifies `sampling_rate = 1.0` for all tables

To check sampling rates manually:
```sql
SELECT TABLE_NAME, COLUMN_NAME, HISTOGRAM->>'$."sampling-rate"' AS sampling_rate
FROM information_schema.COLUMN_STATISTICS
WHERE SCHEMA_NAME = 'tpcds'
ORDER BY sampling_rate;
```

If `sampling_rate < 1.0`, the histogram data is unreliable.

## Understanding histogram_to_case()

The `histogram_to_case()` function in `GenerateDbgen.py` generates CASE expressions for numeric columns based on MySQL histograms.

### MySQL Equi-height Histogram Bucket Format

Each bucket is an array: `[lo, hi, cumulative_freq, num_distinct]`
- `lo`: Lower bound of the bucket range
- `hi`: Upper bound of the bucket range
- `cumulative_freq`: Cumulative frequency (0.0 to 1.0)
- `num_distinct`: **Estimated distinct values in this bucket** (bucket[3])

### Critical Distinction: Span vs Distinct Count

For sparse data (e.g., brand IDs scattered across a large numeric range):
- **span = hi - lo** → The VALUE RANGE (can be very large)
- **num_distinct = bucket[3]** → Actual distinct count (much smaller)

Example: Brand IDs 1001000-9001000 with only 949 distinct values
- span = 8,000,000 (value range)
- num_distinct = 949 (actual count)

Using span instead of num_distinct causes massive over-generation of distinct values.

### Bugs Fixed

1. **Overlapping ranges**: When generating synthetic ranges, consecutive buckets overlapped
   - Fix: Changed `synthetic_start = synthetic_hi` to `synthetic_start = synthetic_hi + 1`

2. **span=0 generating 2 values**: For single-value buckets, `max(1, 0) = 1` caused `rand.range(0, 2)` to generate {0, 1}
   - Fix: Special case for span=0 to generate exactly 1 value (no rand.range)

3. **Using span instead of num_distinct** (PENDING): For equi-height histograms with sparse data, must use `bucket[3]` (num_distinct) not `hi - lo`

### Validation Alignment

Validation compares **index cardinality** (from `SHOW INDEX`) which reflects true distinct counts. The generation logic must use the same source of truth (bucket[3] or COUNT(DISTINCT)) to match validation results.

## Known Issues

### FK Column Divergence (e.g., `web_close_date_sk`)

Foreign key columns use `rand.range(min, max+1)` which generates **uniform distribution** across the entire FK range. If the source data only uses a subset of FK values (e.g., 10 specific dates out of thousands), the replay will have more distinct values.

**Fix approach**: Use the histogram of the FK column itself (sparse approach) for low-cardinality FKs.

### Composite PK Columns

Columns in composite PKs (but not FKs) like `ss_ticket_number` and `cs_order_number` now use:
```
div(rownum-1, rows_per_value) + min_val
```
where `rows_per_value = total_rows / distinct_count`.

This generates **grouped** values (not cycling) to avoid duplicate PK combinations:
- `div()` groups: 1,1,1,1,1,2,2,2,2,2,3,3,3,... (consecutive rows share same value)
- `mod()` cycles: 1,2,3,4,5,1,2,3,4,5,1,2,... (would cause duplicates with random FK column)

For example, with 75,807 distinct tickets across 799,666 rows (10 items/ticket):
`div(rownum-1, 10) + 1` → rows 1-10 get ticket 1, rows 11-20 get ticket 2, etc.

### Composite FK+PK Cardinality (FIXED)

When ALL columns of a composite PK are also FKs (e.g., `inventory` table), we use an odometer pattern.
The key fix: divisors must be based on **source distinct counts**, not reference table sizes.

See `COMPOSITE_FK_PK_CARDINALITY.md` for full explanation.

**Example**: `inventory.inv_date_sk` (PK + FK to date_dim)
- Old approach: divisor = 90,000 (product of other ref tables) → only 131 dates generated
- Fixed approach: divisor = 11,745,000 / 261 ≈ 45,000 → all 261 dates generated

## Code Maintenance Guidelines

### Generic Code Only

**All fixes must be schema-agnostic.** This tool is NOT specific to TPC-DS or TPC-H - it must work for any MySQL schema with any table/column names.

- ❌ **No hardcoded table names** (e.g., `if table == 'web_returns'`)
- ❌ **No hardcoded column names** (e.g., `if col.endswith('_sk')`)
- ❌ **No schema-specific thresholds** (e.g., `if distinct_count == 18000`)
- ✅ **Use metadata-driven logic** (DDL constraints, histogram statistics, COUNT queries)

### Before Making Changes

1. **Trace execution from MasterRun.py** - Don't assume code in GenerateDbgen.py is used
2. **Search for duplicate logic** - Use `grep` to find similar patterns across all files
3. **Check generated_appendages** - MasterRun.py generates appendages that override GenerateDbgen.py logic

### Where to Make Changes

| Change Type | Location |
|-------------|----------|
| FK expression logic | `MasterRun.py:build_fk_appendages()` (primary) |
| Non-FK column logic | `GenerateDbgen.py:annotate_table_with_histogram()` |
| Validation logic | `ValidateTableStats.py` or `PopulateNewTableAndValidate.py` |

### Consolidation Needed

The FK handling logic exists in two places:
- `MasterRun.py:build_fk_appendages()` - **Actually used**
- `GenerateDbgen.py:get_fk_range_expression()` - Dead code when running via MasterRun.py

Future refactoring should consolidate FK logic into GenerateDbgen.py and have MasterRun.py call those functions.

## Validation Thresholds

- Histogram difference: 5% for indexed columns
- Distinct count difference: 5% for critical columns
- Index cardinality: 20% difference allowed
- Unindexed string columns: More lenient (marked as NOTE, not DIVERGED)

## Database Connection

Default MySQL connection:
- Host: localhost
- User: root
- Source schemas: `tpcds`, `tpch`
- Replay schemas: `tpcds_harsha`, `tpch_harsha`
