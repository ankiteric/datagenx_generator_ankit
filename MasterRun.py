#!/usr/bin/env python3
"""
MasterRun.py — End-to-end tpch data generation and validation.

Orchestrates GenerateDbgen, dbgen binary, and PopulateNewTableAndValidate
for every table in `tpch`, writing results into `dbgenx`.
"""

import os
import re
import subprocess
import sys

import mysql.connector
from mysql.connector import Error

from GenerateDbgen import annotate_table_with_histogram, topological_sort
from PopulateNewTableAndValidate import (
    clone_histograms,
    compare_histograms,
    execute_statements,
    load_distinct_counts,
    load_histograms,
    load_index_stats,
    load_table_stats,
    normalize_ddl,
    pct_diff,
    report_ddl_mismatch,
    report_distinct_counts,
    report_histogram_mismatch,
    report_index_stats,
    report_rowcount_mismatch,
    report_table_stats,
)

# ----------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------
HOST = "localhost"
USER = "root"
PASSWORD = "your password"
SOURCE_SCHEMA = "tpch"
TARGET_SCHEMA = "dbgenx"

DBGEN_BINARY = "/Users/ankit/Documents/tpch/tpch-dbgen/dbgen"
DBGEN_FILES_DIR = "dbgen_files"
DBGEN_TMP_OUT_DIR = "dbgen_tmp_out"

FILES_COUNT = "1"
ROWS_COUNT = "1000"


# ----------------------------------------------------------------
# 1. Setup
# ----------------------------------------------------------------
def discover_tables_and_dependencies(cursor, database):
    """Return (all_tables, dependencies) from INFORMATION_SCHEMA.

    Table names from TABLES and KEY_COLUMN_USAGE can differ in case
    (e.g. 'region' vs 'REGION').  We normalise FK references to match
    the canonical name returned by INFORMATION_SCHEMA.TABLES so the
    topological sort works correctly.
    """
    cursor.execute("""
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
    """, (database,))
    all_tables = [t[0] for t in cursor.fetchall()]

    # lowercase -> actual name, for resolving case mismatches in FK refs
    canonical = {t.lower(): t for t in all_tables}

    cursor.execute("""
        SELECT TABLE_NAME, REFERENCED_TABLE_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND REFERENCED_TABLE_NAME IS NOT NULL
    """, (database,))

    dependencies = {}
    for table, referenced_table in cursor.fetchall():
        # resolve both sides to canonical casing
        table = canonical.get(table.lower(), table)
        referenced_table = canonical.get(referenced_table.lower(), referenced_table)

        if table not in dependencies:
            dependencies[table] = set()
        if referenced_table and referenced_table != table:
            dependencies[table].add(referenced_table)

    dependencies = {k: list(v) for k, v in dependencies.items()}
    return all_tables, dependencies


def prepare_target_schema(cursor, target_schema):
    """Create target schema; drop all existing tables if any."""
    cursor.execute(
        "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = %s",
        (target_schema,),
    )
    exists = cursor.fetchone() is not None

    if exists:
        cursor.execute("""
            SELECT TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        """, (target_schema,))
        tables = [t[0] for t in cursor.fetchall()]

        if tables:
            print(f"Dropping {len(tables)} existing table(s) in `{target_schema}`...")
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
            for t in tables:
                cursor.execute(f"DROP TABLE IF EXISTS `{target_schema}`.`{t}`")
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
    else:
        cursor.execute(f"CREATE SCHEMA `{target_schema}`")
        print(f"Created schema `{target_schema}`.")


# ----------------------------------------------------------------
# 2. Per-table processing
# ----------------------------------------------------------------
def build_fk_appendages(cursor, table):
    """For each FK column in `table`, query the distinct count of the
    referenced column in the already-populated target schema and return
    a dict of dbgen expressions.

    FK-only columns get ``rand.range(1,N)`` for a uniform distribution.
    When *all* columns of a composite primary key are foreign keys,
    ``rand.range`` would cause duplicate-key collisions, so those columns
    use deterministic modular arithmetic with ``rownum`` instead.
    """

    # Build canonical name map for target schema (handles case mismatches)
    cursor.execute("""
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
    """, (TARGET_SCHEMA,))
    tgt_canonical = {t[0].lower(): t[0] for t in cursor.fetchall()}

    # FK columns for this table
    cursor.execute("""
        SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND TABLE_NAME = %s
          AND REFERENCED_TABLE_NAME IS NOT NULL
    """, (SOURCE_SCHEMA, table))
    fk_rows = cursor.fetchall()
    if not fk_rows:
        return {}

    # PK columns for this table
    cursor.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND TABLE_NAME = %s
          AND CONSTRAINT_NAME = 'PRIMARY'
    """, (SOURCE_SCHEMA, table))
    pk_columns = {r[0] for r in cursor.fetchall()}

    # Resolve distinct counts for each FK column
    fk_info = []  # (col, actual_ref, ref_col, distinct_count)
    for col, ref_table, ref_col in fk_rows:
        actual_ref = tgt_canonical.get(ref_table.lower())
        if actual_ref is None:
            print(f"      FK {col} -> {ref_table}.{ref_col}: "
                  f"SKIPPED (referenced table not yet in {TARGET_SCHEMA})")
            continue
        cursor.execute(
            f"SELECT COUNT(DISTINCT `{ref_col}`) FROM `{TARGET_SCHEMA}`.`{actual_ref}`"
        )
        distinct_count = cursor.fetchone()[0]
        fk_info.append((col, actual_ref, ref_col, distinct_count))

    # Split into PK+FK vs FK-only
    pk_fk = [(col, ar, rc, dc) for col, ar, rc, dc in fk_info if col in pk_columns]
    fk_only = [(col, ar, rc, dc) for col, ar, rc, dc in fk_info if col not in pk_columns]

    # Check whether every PK column is a FK (composite-PK collision risk)
    all_pk_are_fk = (
        len(pk_columns) > 1
        and pk_columns == {c[0] for c in pk_fk}
    )

    appendages = {}

    # FK-only columns: uniform random
    for col, actual_ref, ref_col, distinct_count in fk_only:
        appendages[col] = f"rand.range(1,{distinct_count})"
        print(f"      FK {col} -> {actual_ref}.{ref_col}: "
              f"{distinct_count} distinct values -> rand.range(1,{distinct_count})")

    if all_pk_are_fk:
        # Composite PK where every column is a FK — use modular arithmetic
        # to guarantee unique tuples.  Sort by distinct count descending so
        # the largest domain gets the most coverage.
        pk_fk.sort(key=lambda x: x[3], reverse=True)
        divisor = 1
        for col, actual_ref, ref_col, distinct_count in pk_fk:
            if divisor == 1:
                expr = f"mod(rownum-1, {distinct_count})+1"
            else:
                expr = f"mod(div(rownum-1, {divisor}), {distinct_count})+1"
            appendages[col] = expr
            print(f"      FK+PK {col} -> {actual_ref}.{ref_col}: "
                  f"{distinct_count} distinct values -> {expr}")
            divisor *= distinct_count
    else:
        # At least one PK column is not a FK and will get `rownum`,
        # so the composite PK is unique regardless — safe to use random.
        for col, actual_ref, ref_col, distinct_count in pk_fk:
            appendages[col] = f"rand.range(1,{distinct_count})"
            print(f"      FK {col} -> {actual_ref}.{ref_col}: "
                  f"{distinct_count} distinct values -> rand.range(1,{distinct_count})")

    return appendages


def step_a_generate_dbgen(cursor, table):
    """Generate .dbgen template file via annotate_table_with_histogram."""
    print(f"  [A] Generating .dbgen template ...")

    generated_appendages = build_fk_appendages(cursor, table)

    ddl = annotate_table_with_histogram(
        HOST, USER, PASSWORD, SOURCE_SCHEMA, table,
        generated_appendages=generated_appendages,
    )
    if ddl is None:
        print(f"  [A] FAILED — annotate_table_with_histogram returned None")
        return False

    path = os.path.join(DBGEN_FILES_DIR, f"{table}.dbgen")
    with open(path, "w") as f:
        f.write(ddl)
    print(f"  [A] Wrote {path}")
    return True


def step_b_run_dbgen(cursor, table):
    """Run dbgen binary to produce .sql file. Returns True on success."""
    print(f"  [B] Running dbgen binary ...")

    cursor.execute(f"SELECT COUNT(*) FROM `{SOURCE_SCHEMA}`.`{table}`")
    row_count = cursor.fetchone()[0]
    print(f"      Source row count = {row_count}")

    template_path = os.path.join(DBGEN_FILES_DIR, f"{table}.dbgen")
    cmd = [
        DBGEN_BINARY,
        "--out-dir", DBGEN_TMP_OUT_DIR,
        "--files-count", FILES_COUNT,
        "--rows-per-file", str(row_count),
        "--rows-count", ROWS_COUNT,
        "--template", template_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [B] FAILED — dbgen returned {result.returncode}")
        print(result.stderr)
        return False

    sql_path = os.path.join(DBGEN_TMP_OUT_DIR, f"{table}.1.sql")
    if not os.path.isfile(sql_path):
        print(f"  [B] FAILED — expected output file not found: {sql_path}")
        return False

    print(f"  [B] Generated {sql_path}")
    return True


def step_c_create_insert_validate(cursor, table):
    """Create table in target schema, insert generated data, validate."""
    print(f"  [C] Creating table, inserting data, validating ...")

    ddl_ok = rows_ok = hist_ok = distinct_ok = True

    # --- Read DDL from .dbgen file ---
    dbgen_path = os.path.join(DBGEN_FILES_DIR, f"{table}.dbgen")
    with open(dbgen_path) as f:
        dbgen_ddl = f.read()

    # Strip dbgen annotations to get clean DDL
    clean_ddl = re.sub(r"/\*\{\{.*?\}\}\*/", "", dbgen_ddl)

    # Replace table name with target-schema-qualified name
    create_stmt = re.sub(
        r"CREATE\s+TABLE\s+`" + re.escape(table) + r"`",
        f"CREATE TABLE `{TARGET_SCHEMA}`.`{table}`",
        clean_ddl,
        count=1,
        flags=re.IGNORECASE,
    )
    cursor.execute(create_stmt)
    print(f"      Created `{TARGET_SCHEMA}`.`{table}`")

    # --- Read and execute INSERT statements ---
    sql_path = os.path.join(DBGEN_TMP_OUT_DIR, f"{table}.1.sql")
    with open(sql_path) as f:
        insert_sql = f.read()

    insert_sql = re.sub(
        rf"(INSERT\s+INTO\s+)`?{re.escape(table)}`?",
        rf"\1`{TARGET_SCHEMA}`.`{table}`",
        insert_sql,
        flags=re.IGNORECASE,
    )
    execute_statements(cursor, insert_sql)

    # --- Validate row count ---
    cursor.execute(f"SELECT COUNT(*) FROM `{SOURCE_SCHEMA}`.`{table}`")
    src_rows = cursor.fetchone()[0]

    cursor.execute(f"SELECT COUNT(*) FROM `{TARGET_SCHEMA}`.`{table}`")
    tgt_rows = cursor.fetchone()[0]

    if src_rows != tgt_rows:
        rows_ok = False
        report_rowcount_mismatch(src_rows, tgt_rows)
    else:
        print(f"      Row count: {tgt_rows} (matches source)")

    # --- Analyze tables for stats ---
    cursor.execute(f"ANALYZE TABLE `{SOURCE_SCHEMA}`.`{table}`")
    cursor.fetchall()
    cursor.execute(f"ANALYZE TABLE `{TARGET_SCHEMA}`.`{table}`")
    cursor.fetchall()

    # --- DDL validation ---
    cursor.execute(f"SHOW CREATE TABLE `{SOURCE_SCHEMA}`.`{table}`")
    src_ddl = cursor.fetchone()[1]
    cursor.execute(f"SHOW CREATE TABLE `{TARGET_SCHEMA}`.`{table}`")
    tgt_ddl = cursor.fetchone()[1]

    if normalize_ddl(src_ddl, SOURCE_SCHEMA) != normalize_ddl(tgt_ddl, SOURCE_SCHEMA):
        ddl_ok = False
        report_ddl_mismatch(src_ddl, tgt_ddl)
    else:
        print(f"      DDL match: OK")

    # --- Histogram validation ---
    clone_histograms(cursor, SOURCE_SCHEMA, TARGET_SCHEMA, table)

    src_hist = load_histograms(cursor, SOURCE_SCHEMA, table)
    tgt_hist = load_histograms(cursor, TARGET_SCHEMA, table)
    hist_diff = compare_histograms(src_hist, tgt_hist)

    if hist_diff:
        hist_ok = False
        report_histogram_mismatch(hist_diff)
    else:
        print(f"      Histograms: OK")

    # --- Table stats ---
    report_table_stats(
        load_table_stats(cursor, SOURCE_SCHEMA, table),
        load_table_stats(cursor, TARGET_SCHEMA, table),
    )

    # --- Index stats ---
    report_index_stats(
        load_index_stats(cursor, SOURCE_SCHEMA, table),
        load_index_stats(cursor, TARGET_SCHEMA, table),
    )

    # --- Distinct counts ---
    src_distinct = load_distinct_counts(cursor, SOURCE_SCHEMA, table)
    tgt_distinct = load_distinct_counts(cursor, TARGET_SCHEMA, table)
    distinct_mismatches = report_distinct_counts(src_distinct, tgt_distinct)

    if distinct_mismatches:
        distinct_ok = False

    return {
        "ddl": ddl_ok,
        "rows": rows_ok,
        "histograms": hist_ok,
        "distinct": distinct_ok,
    }


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------
def main():
    # --- Setup directories ---
    os.makedirs(DBGEN_FILES_DIR, exist_ok=True)
    os.makedirs(DBGEN_TMP_OUT_DIR, exist_ok=True)

    # --- Connect ---
    try:
        conn = mysql.connector.connect(
            host=HOST, user=USER, password=PASSWORD, database=SOURCE_SCHEMA,
            autocommit=True,
        )
        cursor = conn.cursor()
    except Error as e:
        print(f"MySQL connection failed: {e}")
        sys.exit(1)

    cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
    cursor.execute("SET time_zone = '+00:00'")

    # --- Discover and sort tables ---
    all_tables, dependencies = discover_tables_and_dependencies(cursor, SOURCE_SCHEMA)
    sorted_tables = topological_sort(all_tables, dependencies)

    print("=" * 60)
    print("MASTER RUN — tpch -> ankit")
    print("=" * 60)
    print(f"Tables ({len(sorted_tables)}): {' -> '.join(sorted_tables)}")
    print()

    # --- Prepare target schema ---
    prepare_target_schema(cursor, TARGET_SCHEMA)
    print()

    # --- Process each table ---
    results = {}

    for i, table in enumerate(sorted_tables, 1):
        deps = dependencies.get(table, [])
        dep_str = f" (depends on: {', '.join(deps)})" if deps else ""
        print(f"[{i}/{len(sorted_tables)}] Table: {table}{dep_str}")
        print("-" * 50)

        # Step A
        if not step_a_generate_dbgen(cursor, table):
            results[table] = {"error": "dbgen template generation failed"}
            print()
            continue

        # Step B
        if not step_b_run_dbgen(cursor, table):
            results[table] = {"error": "dbgen binary execution failed"}
            print()
            continue

        # Step C
        results[table] = step_c_create_insert_validate(cursor, table)
        print()

    # --- Cleanup ---
    cursor.execute("SET FOREIGN_KEY_CHECKS = 1")

    # --- Final summary ---
    print("=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)

    any_failure = False
    for table in sorted_tables:
        r = results.get(table)
        if r is None:
            print(f"  {table}: SKIPPED (no result)")
            any_failure = True
        elif "error" in r:
            print(f"  {table}: FAILED — {r['error']}")
            any_failure = True
        else:
            all_ok = all(r.values())
            status_parts = []
            for key in ("ddl", "rows", "histograms", "distinct"):
                status_parts.append(f"{key}={'OK' if r[key] else 'FAIL'}")
            overall = "PASS" if all_ok else "FAIL"
            print(f"  {table}: {overall}  [{', '.join(status_parts)}]")
            if not all_ok:
                any_failure = True

    print("=" * 60)

    if any_failure:
        print("Some tables had validation failures.")
        sys.exit(2)
    else:
        print("All tables passed validation.")

    cursor.close()
    conn.close()


if __name__ == "__main__":
    main()
