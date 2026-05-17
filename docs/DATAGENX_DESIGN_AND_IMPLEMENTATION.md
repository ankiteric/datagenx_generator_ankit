# DataGenX: Design and Implementation

This document describes how DataGenX transforms database metadata into annotated DDL files (.dbgen) for synthetic data generation.

---

## Part 1: Design (High-Level)

### 1.1 Overview

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

### 1.2 Input: Metadata

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

### 1.3 Output: Annotated DDL (.dbgen)

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

### 1.4 Expression Strategy by Column Type

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

### 1.5 Privacy Guarantees

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

---

## Part 2: Implementation

### 2.1 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Entry Point                             │
│                        MasterRun.py                             │
└─────────────────────────────────────────────────────────────────┘
                              │
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
┌───────────────────┐ ┌───────────────┐ ┌───────────────────────┐
│ lib/              │ │ datagenx/     │ │ datagenx/             │
│ schema_extractor  │ │ generation/   │ │ validation/           │
│                   │ │ GenerateDbgen │ │                       │
│ • MySQLExtractor  │ │               │ │ • ValidateTableStats  │
│ • SingleStore...  │ │ • annotate()  │ │ • compare_plans       │
└───────────────────┘ │ • histogram() │ └───────────────────────┘
                      └───────────────┘
```

### 2.2 Generic Layer

**Abstract Interface:** `lib/schema_extractor.py`

```python
class SchemaExtractor(ABC):
    @abstractmethod
    def get_tables(self, schema: str) -> List[str]

    @abstractmethod
    def get_columns(self, schema: str, table: str) -> List[Tuple[str, str]]

    @abstractmethod
    def get_primary_keys(self, schema: str, table: str) -> List[str]

    @abstractmethod
    def get_foreign_keys(self, schema: str, table: str) -> Dict[str, Tuple[str, str]]

    @abstractmethod
    def get_table_ddl(self, schema: str, table: str) -> str

    @abstractmethod
    def get_column_histogram(self, schema: str, table: str, column: str) -> Optional[dict]

    @abstractmethod
    def get_table_dependencies(self, schema: str) -> Dict[str, List[str]]
```

**Data Flow:**

```
1. get_tables()           → List of table names
2. get_table_dependencies() → Topological sort order
3. For each table (in order):
   a. get_table_ddl()     → Base CREATE TABLE
   b. get_columns()       → Column names + types
   c. get_primary_keys()  → PK columns
   d. get_foreign_keys()  → FK relationships
   e. get_column_histogram() → Per-column statistics
4. annotate_table_with_histogram() → Generate .dbgen
```

### 2.3 MySQL-Specific Layer

**Implementation:** `MySQLExtractor` in `lib/schema_extractor.py`

**Histogram Extraction:**
```python
def get_column_histogram(self, schema, table, column):
    cursor.execute("""
        SELECT HISTOGRAM
        FROM information_schema.COLUMN_STATISTICS
        WHERE SCHEMA_NAME = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
    """, (schema, table, column))
    row = cursor.fetchone()
    return json.loads(row[0]) if row else None
```

**Histogram Regeneration (Full Scan):**
```python
# Ensure accurate histograms by avoiding sampling
cursor.execute("SET GLOBAL histogram_generation_max_mem_size = 1000000000")
cursor.execute(f"ANALYZE TABLE `{schema}`.`{table}` UPDATE HISTOGRAM ON ...")
```

**FK Relationship Extraction:**
```python
def get_foreign_keys(self, schema, table):
    cursor.execute("""
        SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
        FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
          AND REFERENCED_TABLE_NAME IS NOT NULL
    """, (schema, table))
    return {col: (ref_table, ref_col) for col, ref_table, ref_col in cursor}
```

**Distinct Count Query:**
```python
def get_distinct_count(self, schema, table, column):
    cursor.execute(f"SELECT COUNT(DISTINCT `{column}`) FROM `{schema}`.`{table}`")
    return cursor.fetchone()[0]
```

### 2.4 Expression Generation (GenerateDbgen.py)

**Main Entry Point:**
```python
def annotate_table_with_histogram(host, user, password, database, table,
                                   target_database=None, generated_appendages=None):
    """
    Generate .dbgen file for a single table.

    Args:
        database: Source schema (for metadata extraction)
        target_database: Target schema (for FK value ranges)
        generated_appendages: Pre-computed FK expressions from MasterRun

    Returns:
        Annotated DDL string
    """
```

**Expression Generators:**

| Function | Column Type | Output |
|----------|-------------|--------|
| `rownum` | Single PK | `rownum` or `rownum - 1 + min` |
| `build_single_fk_expression()` | FK | sparse CASE or `rand.range()` |
| `histogram_to_case()` | Numeric (equi-height) | Bucket-cycling CASE |
| `string_values_to_case()` | String (low card) | Weighted CASE with synthetic values |
| `get_date_range_expression()` | Date | `TIMESTAMP + INTERVAL rand.range()` |

**Histogram to CASE (Numeric):**
```python
def histogram_to_case(buckets, row_count, column_name, is_small_table):
    """
    Generate deterministic CASE expression from equi-height histogram.

    Strategy:
    - Outer CASE: mod(rownum-1, num_buckets) selects bucket
    - Inner: mod(div(rownum-1, num_buckets), num_distinct) cycles within bucket

    Example output:
    CASE mod(rownum-1, 100) + 1
      WHEN 1 THEN mod(div(rownum-1, 100), 50) + 0
      WHEN 2 THEN mod(div(rownum-1, 100), 50) + 50
      ...
    END
    """
```

**String Values to CASE:**
```python
def string_values_to_case(value_counts, column_name, col_length):
    """
    Generate weighted CASE with synthetic string values.

    Input: [(count1, 'actual_val1'), (count2, 'actual_val2'), ...]
    Output: CASE rand.weighted([freq1, freq2, ...])
              WHEN 1 THEN 'column_name_1___________'
              WHEN 2 THEN 'column_name_2___________'
            END

    Note: Actual values discarded; only counts used for weights.
    """
```

### 2.5 FK Expression Generation (MasterRun.py)

**Pre-computation in MasterRun:**
```python
def build_fk_appendages(table, fk_info, conn):
    """
    Generate FK expressions BEFORE calling GenerateDbgen.

    Returns: Dict[column_name, expression_string]

    Decision tree:
    1. Coverage > 80%? → rand.range(min, max+1)
    2. Singleton histogram? → weighted CASE sampling from target
    3. Composite FK? → N-cycling (div + mod)
    4. Default → mod(rownum-1, distinct) + min
    """
```

**N-Cycling for Composite FK+PK:**
```python
def build_composite_fk_expression(columns, ref_table, ref_row_count):
    """
    When ALL PK columns are also FKs (e.g., PARTSUPP).

    Strategy:
    - Largest dimension: div(rownum-1, rows_per_largest) + min
    - Other dimensions: mod(rownum-1, distinct_count) + min

    Example (PARTSUPP: 200K parts × 10K suppliers = 800K rows):
    ps_partkey = div(rownum-1, 4) + 1       # grouped
    ps_suppkey = mod(rownum-1, 10000) + 1   # cycling
    """
```

### 2.6 Processing Pipeline

```
Step 1: Regenerate Histograms
    SET histogram_generation_max_mem_size = 1GB
    ANALYZE TABLE ... UPDATE HISTOGRAM ON all_columns
    Verify sampling_rate = 1.0

Step 2: Build FK Appendages
    For each table (topological order):
        For each FK column:
            Determine approach (sparse/dense/cycling)
            Generate expression
            Store in appendages dict

Step 3: Generate .dbgen Files
    For each table:
        Get DDL
        For each column:
            If FK: use pre-computed appendage
            If PK: rownum or cycling
            If Date: synthetic range
            If Numeric: histogram CASE
            If String: weighted synthetic CASE
        Write annotated DDL to .dbgen file

Step 4: Run dbgen Binary
    Input: .dbgen file + row count
    Output: CSV with generated data

Step 5: Load and Validate
    CREATE TABLE in target schema
    LOAD DATA from CSV
    ANALYZE TABLE
    Compare statistics (cardinality, histograms)
```

### 2.7 Key Files Reference

| File | Purpose |
|------|---------|
| `MasterRun.py` | Orchestration, FK appendage generation |
| `GenerateDbgen.py` | Expression generation, .dbgen file creation |
| `lib/schema_extractor.py` | Database-agnostic metadata extraction |
| `config.py` | Database credentials, schema names, paths |
| `dbgen_files/*.dbgen` | Generated annotated DDL files |
| `dbgen_tmp_out/*.csv` | Generated data files |

---

## Appendix: Expression Examples

### A.1 Primary Key (Simple)
```sql
`n_nationkey` int NOT NULL /*{{ @n_nationkey := rownum }}*/
```

### A.2 Primary Key (Composite with Grouping)
```sql
`ss_ticket_number` int NOT NULL /*{{ @ss_ticket_number := div(rownum-1, 10) + 1 }}*/
`ss_item_sk` int NOT NULL /*{{ @ss_item_sk := mod(rownum-1, 18000) + 1 }}*/
```

### A.3 Foreign Key (Dense Range)
```sql
`o_custkey` int /*{{ @o_custkey := rand.range(1, 150001) }}*/
```

### A.4 Foreign Key (Sparse Weighted)
```sql
`n_regionkey` int /*{{ @n_regionkey := case rand.weighted(array[0.2, 0.2, 0.2, 0.2, 0.2])
  when 1 then 1 when 2 then 2 when 3 then 3 when 4 then 4 when 5 then 5
end }}*/
```

### A.5 Date (Synthetic Range)
```sql
`d_date` date /*{{ @d_date := TIMESTAMP '2000-01-01 00:00:00' + INTERVAL rand.range(0, 73049) DAY }}*/
```

### A.6 Numeric (Equi-height Histogram)
```sql
`ps_availqty` int /*{{ @ps_availqty := case mod(rownum-1, 100) + 1
  when 1 then mod(div(rownum-1, 100), 99) + 0
  when 2 then mod(div(rownum-1, 100), 100) + 99
  when 3 then mod(div(rownum-1, 100), 100) + 199
  ...
end }}*/
```

### A.7 String (Weighted Synthetic)
```sql
`n_name` char(25) /*{{ @n_name := case rand.weighted(array[0.04, 0.04, 0.04, ...])
  when 1 then 'n_name_1_________________'
  when 2 then 'n_name_2_________________'
  when 3 then 'n_name_3_________________'
  ...
end }}*/
```
