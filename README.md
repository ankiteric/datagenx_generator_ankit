# DataGenX Generator

Generate synthetic database data that preserves optimizer-relevant statistics
from a source schema while avoiding direct use of source data values.

## Current TPC-H Setup

The current local setup uses:

```text
source schema: tpch_vanilla
target schema: tpch_dbgenx
```

`tpch_vanilla` is loaded from TPC-H `dbgen` `.tbl` files. `tpch_dbgenx` is
created by this project.

Expected small TPC-H source data is scale factor `0.01`, with row counts around:

```text
region       5
nation       25
supplier     100
customer     1500
part         2000
partsupp     8000
orders       15000
lineitem     about 60000
```

## Prerequisites

Install MySQL and make password login work for the configured user:

```bash
sudo apt update
sudo apt install mysql-server
sudo mysql -u root
```

```sql
ALTER USER 'root'@'localhost'
IDENTIFIED WITH caching_sha2_password BY 'newpassword';
FLUSH PRIVILEGES;
EXIT;
```

Verify:

```bash
mysql -u root -pnewpassword -e "SELECT VERSION();"
```

Install the Python MySQL connector inside the active virtualenv:

```bash
python3 -m pip install mysql-connector-python
```

Build the DataGenX Rust `dbgen` binary:

```bash
cd /home/hmaduri/contribs/dbgen
cargo build --release --bin dbgen
```

Expected binary:

```text
/home/hmaduri/contribs/dbgen/target/release/dbgen
```

## Required config.py Values

For the current TPC-H workflow, `config.py` should contain:

```python
HOST = "localhost"
USER = "root"
PASSWORD = "newpassword"

SOURCE_SCHEMA = "tpch_vanilla"
TARGET_SCHEMA = "tpch_dbgenx"

DBGEN_BINARY = "/home/hmaduri/contribs/dbgen/target/release/dbgen"
DBGEN_FILES_DIR = "generated/dbgen_files"
DBGEN_TMP_OUT_DIR = "generated/dbgen_tmp_out"
```

Change only `SOURCE_SCHEMA`, `TARGET_SCHEMA`, and `DBGEN_BINARY` when switching
environments.

## Load TPC-H Source Data

Build the official TPC-H data generator:

```bash
cd /home/hmaduri/contribs/tpch-dbgen
make MACHINE=LINUX DATABASE=MYSQL WORKLOAD=TPCH
```

Generate small TPC-H source data:

```bash
./dbgen -vf -s 0.01
```

Load it into MySQL as `tpch_vanilla`:

```bash
cd /home/hmaduri/contribs/datagenx_generator
mysql --local-infile=1 -u root -pnewpassword < sql/load_tpch_vanilla.sql
```

The load script creates tables, loads `.tbl` files, adds primary/foreign keys,
and runs `ANALYZE TABLE`.

## Generate Synthetic Data

Run:

```bash
python3 MasterRun.py
```

By default, `MasterRun.py` now:

```text
generates .dbgen templates
runs the DataGenX dbgen binary
creates and loads target tables
clones MySQL histograms from source to target
skips built-in validation
```

To force the older built-in validation path:

```bash
python3 MasterRun.py --run-validation
```

## Validate Separately

Use the unified validation entry point:

```bash
python3 validate.py stats \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx
```

Faster stats validation:

```bash
python3 validate.py stats \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --skip-distinct
```

Run benchmark-specific SQL validation:

```bash
python3 validate.py sql tpch \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx
```

Render SQL without executing:

```bash
python3 validate.py sql tpch \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --render-only
```

Other validation helpers:

```bash
python3 validate.py replay \
  --ddl-file generated/dbgen_tmp_out/orders-schema.sql \
  --insert-file generated/dbgen_tmp_out/orders.1.csv

python3 validate.py plans
python3 validate.py query q3
python3 validate.py all --skip-distinct
```

## Literal Mapping for Query Rewrites

Render the original TPC-H query templates into runnable MySQL SQL files. This
is optional and never runs unless explicitly requested:

```bash
python3 validate.py tpch-queries \
  --template-dir /home/hmaduri/contribs/tpch-dbgen/queries \
  --output-dir generated/tpch_queries_mysql
```

Build a sensitive local source-literal to synthetic-literal mapping:

```bash
python3 validate.py literal-map \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx
```

Default output:

```text
generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json
```

Use it to rewrite target-side query literals:

```bash
python3 validate.py rewrite-query \
  --mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json \
  --sql "select * from lineitem where l_returnflag = 'R';"
```

Use it during plan/query validation:

```bash
python3 validate.py query q12 \
  --queries-dir generated/tpch_queries_mysql \
  --literal-mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json
```

Run all rendered TPC-H query plan comparisons:

```bash
python3 validate.py plans \
  --queries-dir generated/tpch_queries_mysql \
  --literal-mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json \
  --output-file /tmp/tpch_plan_comparison.txt
```

The mapping file contains source literals and must stay local/private. More
detail: [Literal Mapping](docs/LITERAL_MAPPING.md).

## Smoke Tests

Run these from the repository root:

```bash
cd /home/hmaduri/contribs/datagenx_generator
```

Check that the CLI commands are wired:

```bash
/home/hmaduri/myenv/bin/python3 MasterRun.py --help
/home/hmaduri/myenv/bin/python3 validate.py --help
/home/hmaduri/myenv/bin/python3 validate.py tpch-queries --help
/home/hmaduri/myenv/bin/python3 validate.py literal-map --help
/home/hmaduri/myenv/bin/python3 validation_report.py --help
```

Check Python syntax/imports:

```bash
/home/hmaduri/myenv/bin/python3 -m py_compile \
  MasterRun.py \
  validate.py \
  validation_report.py \
  datagenx/generation/GenerateDbgen.py \
  datagenx/validation/ValidateTableStats.py \
  datagenx/validation/literal_mapping.py \
  datagenx/validation/tpch_queries.py \
  datagenx/validation/validation_report.py
```

Validate current source and synthetic schemas:

```bash
/home/hmaduri/myenv/bin/python3 validate.py stats \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --skip-distinct
```

Expected result:

```text
Overall: ✅ ALL PASSED
```

Render benchmark SQL without executing it:

```bash
/home/hmaduri/myenv/bin/python3 validate.py sql tpch \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --render-only \
  --output-sql /tmp/tpch_validation_rendered.sql
```

Generate the HTML validation report:

```bash
/home/hmaduri/myenv/bin/python3 validation_report.py \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --output /tmp/tpch_validation_report.html
```

Render TPC-H query templates and build the sensitive literal mapping:

```bash
/home/hmaduri/myenv/bin/python3 validate.py tpch-queries \
  --template-dir /home/hmaduri/contribs/tpch-dbgen/queries \
  --output-dir generated/tpch_queries_mysql

/home/hmaduri/myenv/bin/python3 validate.py literal-map \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx
```

Check that query literal rewriting works:

```bash
/home/hmaduri/myenv/bin/python3 validate.py rewrite-query \
  --mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json \
  --sql "select * from lineitem where l_returnflag = 'R' and l_shipmode in ('MAIL','SHIP');"
```

Expected rewrite includes:

```sql
l_returnflag = '2'
l_shipmode in ('l__2','l__7')
```

Run one TPC-H plan comparison with target-side literal/date rewriting:

```bash
/home/hmaduri/myenv/bin/python3 validate.py query q12 \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --queries-dir generated/tpch_queries_mysql \
  --literal-mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json
```

Expected result:

```text
Plan Shape:      IDENTICAL
```

Optional full plan smoke test:

```bash
/home/hmaduri/myenv/bin/python3 validate.py plans \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --queries-dir generated/tpch_queries_mysql \
  --literal-mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json \
  --output-file /tmp/tpch_plan_comparison.txt
```

Generated local artifacts from these tests are intentionally ignored by git:

```text
generated/literal_mappings/
generated/tpch_queries_mysql/
/tmp/tpch_validation_report.html
/tmp/tpch_plan_comparison.txt
```

## Visualization Report

Use the project venv when generating validation visuals:

```bash
/home/hmaduri/myenv/bin/python3 validation_report.py \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --output /tmp/tpch_validation_report.html
```

The report includes:

```text
dashboard health cards
table-level validation matrix
top drift columns
histogram-difference heatmap
TPC-H referential-integrity graph and orphan checks
exact source-vs-synthetic row overlap checks
selected source-vs-target frequency distributions
distinct-count differences
```

## Important Behavior

Histogram cloning is part of target creation, not validation. `MasterRun.py`
clones histograms even when validation is skipped, because `validate.py stats`
expects target histograms to exist.

Histogram validation compares distribution shape, not literal bucket values.
For each source/target histogram pair, validation extracts per-bucket frequency
mass from MySQL's cumulative bucket probabilities, sorts those masses, pads
missing buckets with zero, and compares the resulting bucket-frequency shape.
This lets synthetic domains differ from source domains while still checking
bucket count and frequency drift.

More detail: [Histogram Comparison](docs/HISTOGRAM_COMPARISON.md).

For low-cardinality string histograms, generation uses deterministic bucket
assignment instead of random weighted selection. This avoids random collisions
that can collapse target bucket counts.

If validation reports `missing in target` for histograms, rerun:

```bash
python3 MasterRun.py
```

then validate again.
## Repository Layout

```text
MasterRun.py                 root wrapper for generation
validate.py                  root wrapper for validation
validation_report.py         root wrapper for HTML report generation
config.py                    local database and generation settings
datagenx/orchestration/      end-to-end generation workflow
datagenx/generation/         dbgen template and insert helpers
datagenx/validation/         stats, SQL, plan, query, and report validators
datagenx/legacy/             older Sakila helper scripts
sql/                         reusable SQL scripts
docs/                        design notes, reports, and fix writeups
generated/dbgen_files/       generated .dbgen templates
generated/dbgen_tmp_out/     generated CSV/schema outputs from dbgen
scripts/                     standalone shell helpers
```
