#!/usr/bin/env python3
"""
MasterRun.py — End-to-end data generation and validation.

Orchestrates GenerateDbgen, dbgen binary, and PopulateNewTableAndValidate
for every table in SOURCE_SCHEMA, writing results into TARGET_SCHEMA.
"""

import argparse
import os
import re
import subprocess
import sys

import mysql.connector
from mysql.connector import Error

# Global flags (set by argparse)
VERBOSE = True
COMPARE_HISTOGRAMS = False  # Disabled by default - histogram comparison is unreliable

from GenerateDbgen import annotate_table_with_histogram, topological_sort, build_single_fk_expression
from PopulateNewTableAndValidate import (
    clone_histograms,
    compare_histograms,
    execute_statements,
    load_column_types,
    load_distinct_counts,
    load_histograms,
    load_index_stats,
    load_indexed_columns,
    load_table_stats,
    normalize_ddl,
    pct_diff,
    report_ddl_mismatch,
    report_distinct_counts,
    report_histogram_comparison,
    report_index_stats,
    report_rowcount_mismatch,
    report_table_stats,
)

# ----------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------
HOST = "localhost"
USER = "root"
PASSWORD = "newpassword"
SOURCE_SCHEMA = "tpcds"
TARGET_SCHEMA = "tpcds_harsha"

DBGEN_BINARY = "/Users/sreeharshar/work/db/datagenx/code/dbgen/target/release/dbgen"
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
    """For each FK column in `table`, build dbgen expressions.

    Handles three cases:
    1. Composite PK where all columns are FKs (e.g., PARTSUPP):
       Uses interleaved arithmetic for full coverage of both domains.
    2. Composite FK referencing another table's composite key (e.g., LINEITEM):
       Uses same interleaved formula as referenced table, cycling as needed.
    3. Single-column FKs: Uses rand.range() for uniform distribution.
    """

    # Build canonical name map for target schema (handles case mismatches)
    cursor.execute("""
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
    """, (TARGET_SCHEMA,))
    tgt_canonical = {t[0].lower(): t[0] for t in cursor.fetchall()}

    # FK columns grouped by constraint name
    cursor.execute("""
        SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND TABLE_NAME = %s
          AND REFERENCED_TABLE_NAME IS NOT NULL
        ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION
    """, (SOURCE_SCHEMA, table))
    fk_rows = cursor.fetchall()
    if not fk_rows:
        return {}

    # Group by constraint name
    from collections import defaultdict
    constraints = defaultdict(list)  # constraint_name -> [(col, ref_table, ref_col), ...]
    for constraint_name, col, ref_table, ref_col in fk_rows:
        constraints[constraint_name].append((col, ref_table, ref_col))

    # PK columns for this table
    cursor.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND TABLE_NAME = %s
          AND CONSTRAINT_NAME = 'PRIMARY'
    """, (SOURCE_SCHEMA, table))
    pk_columns = {r[0] for r in cursor.fetchall()}

    # Get source row count for this table
    cursor.execute(f"SELECT COUNT(*) FROM `{SOURCE_SCHEMA}`.`{table}`")
    source_row_count = cursor.fetchone()[0]

    # Collect all FK columns that are also PK columns
    all_fk_columns = {col for _, cols in constraints.items() for col, _, _ in cols}
    pk_fk_columns = pk_columns & all_fk_columns

    # Check if ALL PK columns are FKs (composite PK case like PARTSUPP)
    # This can happen with multiple single-column FKs forming the PK
    all_pk_are_fk = (len(pk_columns) > 1 and pk_columns == pk_fk_columns)

    appendages = {}

    if all_pk_are_fk:
        # Composite PK where all columns are FKs (e.g., PARTSUPP)
        # Collect info for all PK+FK columns across all constraints
        pk_fk_info = []  # [(col, ref_table, ref_col, distinct_count, min_val), ...]
        for constraint_name, fk_cols in constraints.items():
            for col, ref_table, ref_col in fk_cols:
                if col in pk_columns:
                    actual_ref = tgt_canonical.get(ref_table.lower())
                    if actual_ref is None:
                        continue
                    cursor.execute(
                        f"SELECT COUNT(DISTINCT `{ref_col}`), MIN(`{ref_col}`) FROM `{TARGET_SCHEMA}`.`{actual_ref}`"
                    )
                    distinct_count, min_val = cursor.fetchone()
                    min_val = min_val if min_val is not None else 0
                    pk_fk_info.append((col, actual_ref, ref_col, distinct_count, min_val))

        if len(pk_fk_info) >= 2:
            # Sort by distinct count ascending (smaller domains first)
            pk_fk_info.sort(key=lambda x: x[3])

            # For full coverage of ALL domains:
            # - Smaller domains: use mod(rownum-1, distinct_count)+min_val to cycle
            # - Largest domain: use div(rownum-1, rows_per_large)+min_val to spread
            #
            # This ensures each domain gets full coverage when R >= max(Di)

            # All but the last (largest) use mod cycling
            divisor = 1
            for col, ref_table, ref_col, distinct_count, min_val in pk_fk_info[:-1]:
                if divisor == 1:
                    expr = f"mod(rownum-1, {distinct_count})+{min_val}"
                else:
                    expr = f"mod(div(rownum-1, {divisor}), {distinct_count})+{min_val}"
                appendages[col] = expr
                print(f"      FK+PK {col} -> {ref_table}.{ref_col}: "
                      f"{distinct_count} distinct, min={min_val}, divisor={divisor} -> {expr}")
                divisor *= distinct_count

            # Largest domain also uses mod to stay within valid range
            large_col, large_ref, large_refcol, large_count, large_min = pk_fk_info[-1]
            large_expr = f"mod(div(rownum-1, {divisor}), {large_count})+{large_min}"
            appendages[large_col] = large_expr
            print(f"      FK+PK {large_col} -> {large_ref}.{large_refcol}: "
                  f"{large_count} distinct, min={large_min}, divisor={divisor} -> {large_expr}")

        # Handle any remaining FK-only columns (not part of PK)
        for constraint_name, fk_cols in constraints.items():
            for col, ref_table, ref_col in fk_cols:
                if col not in pk_columns and col not in appendages:
                    actual_ref = tgt_canonical.get(ref_table.lower())
                    if actual_ref is None:
                        continue
                    expression, description = build_single_fk_expression(
                        cursor, SOURCE_SCHEMA, TARGET_SCHEMA, table, col, actual_ref, ref_col
                    )
                    appendages[col] = expression
                    print(f"      FK {col} -> {actual_ref}.{ref_col}: {description}")

        return appendages

    # Normal case: process each constraint
    for constraint_name, fk_cols in constraints.items():
        if len(fk_cols) == 1:
            # Single-column FK - use unified FK expression builder
            col, ref_table, ref_col = fk_cols[0]
            actual_ref = tgt_canonical.get(ref_table.lower())
            if actual_ref is None:
                print(f"      FK {col} -> {ref_table}.{ref_col}: "
                      f"SKIPPED (referenced table not yet in {TARGET_SCHEMA})")
                continue

            expression, description = build_single_fk_expression(
                cursor, SOURCE_SCHEMA, TARGET_SCHEMA, table, col, actual_ref, ref_col
            )
            appendages[col] = expression
            print(f"      FK {col} -> {actual_ref}.{ref_col}: {description}")

        else:
            # Composite FK (multiple columns reference same table, e.g., LINEITEM -> PARTSUPP)
            ref_table = fk_cols[0][1]
            actual_ref = tgt_canonical.get(ref_table.lower())
            if actual_ref is None:
                print(f"      Composite FK -> {ref_table}: "
                      f"SKIPPED (referenced table not yet in {TARGET_SCHEMA})")
                continue

            # Get row count of referenced table (total valid pairs)
            cursor.execute(f"SELECT COUNT(*) FROM `{TARGET_SCHEMA}`.`{actual_ref}`")
            ref_row_count = cursor.fetchone()[0]

            # Get distinct counts and min values for each column in the composite FK
            col_info = []  # [(col, ref_col, distinct_count, min_val), ...]
            for col, _, ref_col in fk_cols:
                cursor.execute(
                    f"SELECT COUNT(DISTINCT `{ref_col}`), MIN(`{ref_col}`) FROM `{TARGET_SCHEMA}`.`{actual_ref}`"
                )
                distinct_count, min_val = cursor.fetchone()
                min_val = min_val if min_val is not None else 0
                col_info.append((col, ref_col, distinct_count, min_val))

            # Sort by distinct count ascending (smaller domains first)
            col_info.sort(key=lambda x: x[2])

            # For composite FK referencing another table:
            # Must generate pairs that exist in the referenced table.
            # Use same formula as the referenced table, but cycle through ref_row_count.
            #
            # - Smaller domains: mod(mod(rownum-1, ref_row_count), distinct_count)+min_val
            # - Largest domain: div(mod(rownum-1, ref_row_count), rows_per_large)+min_val

            # All but the last (largest) use mod cycling
            divisor = 1
            for col, ref_col, distinct_count, min_val in col_info[:-1]:
                if divisor == 1:
                    expr = f"mod(mod(rownum-1, {ref_row_count}), {distinct_count})+{min_val}"
                else:
                    expr = f"mod(div(mod(rownum-1, {ref_row_count}), {divisor}), {distinct_count})+{min_val}"
                appendages[col] = expr
                print(f"      Composite FK {col} -> {actual_ref}.{ref_col}: "
                      f"cycling {ref_row_count} pairs, min={min_val}, divisor={divisor} -> {expr}")
                divisor *= distinct_count

            # Largest domain also uses mod to stay within valid range
            large_col, large_refcol, large_count, large_min = col_info[-1]
            large_expr = f"mod(div(mod(rownum-1, {ref_row_count}), {divisor}), {large_count})+{large_min}"
            appendages[large_col] = large_expr
            print(f"      Composite FK {large_col} -> {actual_ref}.{large_refcol}: "
                  f"cycling {ref_row_count} pairs, min={large_min}, divisor={divisor} -> {large_expr}")

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
    # Note: re.DOTALL makes . match newlines (annotations can span multiple lines)
    clean_ddl = re.sub(r"/\*\{\{.*?\}\}\*/", "", dbgen_ddl, flags=re.DOTALL)

    # Replace table name with target-schema-qualified name
    create_stmt = re.sub(
        r"CREATE\s+TABLE\s+`" + re.escape(table) + r"`",
        f"CREATE TABLE `{TARGET_SCHEMA}`.`{table}`",
        clean_ddl,
        count=1,
        flags=re.IGNORECASE,
    )

    # Update FK REFERENCES to point to target schema
    create_stmt = re.sub(
        r"REFERENCES\s+`([^`]+)`\s*\(",
        rf"REFERENCES `{TARGET_SCHEMA}`.`\1` (",
        create_stmt,
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

    # --- Histogram validation (optional - disabled by default) ---
    clone_histograms(cursor, SOURCE_SCHEMA, TARGET_SCHEMA, table)

    # Get column metadata for categorizing mismatches
    indexed_cols = load_indexed_columns(cursor, SOURCE_SCHEMA, table)
    column_types = load_column_types(cursor, SOURCE_SCHEMA, table)

    if COMPARE_HISTOGRAMS:
        src_hist = load_histograms(cursor, SOURCE_SCHEMA, table)
        tgt_hist = load_histograms(cursor, TARGET_SCHEMA, table)
        hist_results = compare_histograms(src_hist, tgt_hist)

        hist_critical = report_histogram_comparison(hist_results, indexed_cols, column_types, VERBOSE)
        if hist_critical:
            hist_ok = False

    # --- Table stats ---
    report_table_stats(
        load_table_stats(cursor, SOURCE_SCHEMA, table),
        load_table_stats(cursor, TARGET_SCHEMA, table),
        VERBOSE,
    )

    # --- Index stats ---
    report_index_stats(
        load_index_stats(cursor, SOURCE_SCHEMA, table),
        load_index_stats(cursor, TARGET_SCHEMA, table),
        VERBOSE,
    )

    # --- Distinct counts ---
    src_distinct = load_distinct_counts(cursor, SOURCE_SCHEMA, table)
    tgt_distinct = load_distinct_counts(cursor, TARGET_SCHEMA, table)
    distinct_mismatches = report_distinct_counts(src_distinct, tgt_distinct, VERBOSE)

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
    print(f"MASTER RUN — {SOURCE_SCHEMA} -> {TARGET_SCHEMA}")
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
    if not COMPARE_HISTOGRAMS:
        print("(Histogram comparison disabled - use --compare-histograms to enable)")
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
            # When histogram comparison is disabled, don't count it in pass/fail
            if COMPARE_HISTOGRAMS:
                all_ok = all(r.values())
                status_parts = []
                for key in ("ddl", "rows", "histograms", "distinct"):
                    status_parts.append(f"{key}={'OK' if r[key] else 'FAIL'}")
            else:
                # Skip histogram check in pass/fail determination
                all_ok = r["ddl"] and r["rows"] and r["distinct"]
                status_parts = [
                    f"ddl={'OK' if r['ddl'] else 'FAIL'}",
                    f"rows={'OK' if r['rows'] else 'FAIL'}",
                    "histograms=SKIP",
                    f"distinct={'OK' if r['distinct'] else 'FAIL'}",
                ]
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
    parser = argparse.ArgumentParser(
        description="End-to-end data generation and validation"
    )
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Verbose output (show all results, not just failures)")
    parser.add_argument("--compare-histograms", action="store_true",
                        help="Enable histogram comparison (disabled by default - unreliable)")
    args = parser.parse_args()
    VERBOSE = args.verbose
    COMPARE_HISTOGRAMS = args.compare_histograms
    main()
