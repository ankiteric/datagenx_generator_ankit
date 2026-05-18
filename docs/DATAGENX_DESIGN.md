# DataGenX: Design

This document describes the high-level design of how DataGenX transforms database metadata into annotated DDL files (.dbgen) for synthetic data generation.

For implementation details, see [DATAGENX_IMPLEMENTATION.md](DATAGENX_IMPLEMENTATION.md).

---

## 1. Overview

```
┌─────────────────────┐      ┌─────────────────────┐      ┌─────────────────────┐
│   INPUT: Metadata   │ ──▶  │  PROCESS: Annotate  │ ──▶  │  OUTPUT: .dbgen     │
│                     │      │                     │      │                     │
│ • Schema DDL        │      │ • Column classify   │      │ • Valid SQL DDL     │
│ • Histograms        │      │ • Expression gen    │      │ • Inline expressions│
│ • Row counts        │      │ • Privacy filter    │      │ • Ready for dbgen   │
│ • Distinct counts   │      │                     │      │                     │
└─────────────────────┘      └─────────────────────┘      └─────────────────────┘
```

## 2. Input: Metadata

| Metadata Type | Source | Purpose |
|---------------|--------|---------|
| Schema DDL | `SHOW CREATE TABLE` | Column types, constraints, PKs, FKs |
| Histograms | `information_schema.column_statistics` | Distribution shapes and weights |
| Row counts | `SELECT COUNT(*)` | Scaling, small-table detection |
| Distinct counts | `SELECT COUNT(DISTINCT col)` | FK coverage, cardinality matching |
| FK relationships | `information_schema.KEY_COLUMN_USAGE` | Reference graph, topological sort |

**Histogram Structure (MySQL):**
```json
{
  "histogram-type": "singleton" | "equi-height",
  "buckets": [
    [value, cumulative_freq],                    // singleton
    [min, max, cumulative_freq, num_distinct]    // equi-height
  ]
}
```

## 3. Output: Annotated DDL (.dbgen)

The output is valid SQL DDL with embedded generation expressions:

```sql
CREATE TABLE `table_name` (
  `pk_col` int NOT NULL /*{{ @pk_col := rownum }}*/,
  `fk_col` int /*{{ @fk_col := mod(rownum-1, 1000) + 1 }}*/,
  `str_col` varchar(25) /*{{ @str_col := case rand.weighted(array[0.3, 0.7])
    when 1 then 'str_col_1________________'
    when 2 then 'str_col_2________________'
  end }}*/,
  `date_col` date /*{{ @date_col := TIMESTAMP '2000-01-01' + INTERVAL rand.range(0, 365) DAY }}*/,
  `num_col` decimal(10,2) /*{{ @num_col := rand.range(100, 10000) / 100 }}*/,
  PRIMARY KEY (`pk_col`),
  FOREIGN KEY (`fk_col`) REFERENCES `ref_table` (`ref_pk`)
)
```

**Key Properties:**
- Valid SQL syntax (annotations are comments)
- Expressions use `@column := value` syntax
- `rownum` is the row counter (1, 2, 3, ...)
- `rand.range()`, `rand.weighted()` for randomness
- `mod()`, `div()` for deterministic cycling

## 4. Expression Strategy by Column Type

```
┌─────────────────────────────────────────────────────────────────┐
│                    Column Classification                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Column                                                         │
│    │                                                            │
│    ├── PRIMARY KEY?                                             │
│    │     ├── Single PK ──────────▶ rownum                       │
│    │     └── Composite PK ───────▶ div/mod cycling              │
│    │                                                            │
│    ├── FOREIGN KEY?                                             │
│    │     ├── High coverage (>80%) ▶ rand.range(min, max)        │
│    │     ├── Singleton histogram ─▶ weighted CASE               │
│    │     └── Composite FK ────────▶ N-cycling (div + mod)       │
│    │                                                            │
│    ├── DATE/DATETIME?                                           │
│    │     ├── Low cardinality ─────▶ weighted CASE (synthetic)   │
│    │     └── High cardinality ────▶ base + INTERVAL rand.range  │
│    │                                                            │
│    ├── NUMERIC?                                                 │
│    │     ├── Singleton histogram ─▶ weighted CASE               │
│    │     └── Equi-height histogram▶ bucket cycling              │
│    │                                                            │
│    └── STRING?                                                  │
│          ├── Low cardinality ─────▶ weighted CASE (synthetic)   │
│          └── High cardinality ────▶ rand.regex('[a-z]{len}')    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## 5. Privacy Guarantees

**Used (Statistical Patterns):**
- Distribution shapes (histogram bucket weights)
- Cardinality counts
- Row counts
- Column metadata (types, lengths)

**Never Used (Data Values):**
- Actual values from histograms (dates, numbers, strings)
- MIN/MAX from source data
- Row samples from source tables

**Example - Date Column:**
```
Source data:     1995-01-01 to 1998-12-31 (TPC-H dates)
Histogram:       span = 1461 days
Generated expr:  TIMESTAMP '2000-01-01' + INTERVAL rand.range(0, 1461) DAY
Output:          2000-01-01 to 2004-01-01 (synthetic range, same span)
```

## 6. Validation Dimensions

After generation, DataGenX validates synthetic data across 5 dimensions:

| Check | What It Measures | Pass Criteria |
|-------|------------------|---------------|
| **Row Counts** | Total rows per table | Exact match |
| **Histograms** | Distribution shape (sorted bucket weights) | <5% total variation distance |
| **Distinct Counts** | Cardinality per column | <5% difference |
| **FK Integrity** | Orphan rows in child tables | 0 orphans |
| **Privacy** | Exact row overlap (MD5 hash) | <1% overlap |

## 7. Supported Databases

| Database | Extractor | Histogram Source |
|----------|-----------|------------------|
| MySQL | `MySQLExtractor` | `information_schema.column_statistics` |
| SingleStore | `SingleStoreExtractor` | `information_schema.column_statistics` |
| TiDB | `TiDBExtractor` | `SHOW STATS_HISTOGRAMS` / `SHOW STATS_BUCKETS` |
