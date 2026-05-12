#!/usr/bin/env python3
"""
Extract schema from MySQL or SingleStore database and generate .dbgen files.
Customer-facing tool for schema extraction.
"""

import argparse
import os
import sys
import re
import json
from datetime import datetime

# Import existing code
from datagenx.generation.GenerateDbgen import (
    histogram_to_case, char_varchar_appendage, text_appendage,
    string_values_to_case, get_string_column_length,
    get_min_max_from_histogram, STRING_CARDINALITY_THRESHOLD,
    topological_sort, NUMERIC_TYPES, DATETIME_TYPES, CHAR_TYPES,
    TEXT_TYPES, YEAR
)

# Import our new library
from lib.schema_extractor import available_extractor_types, create_schema_extractor


def _load_table_cardinality(extractor, table):
    """Load table cardinality metadata through the extractor abstraction."""
    try:
        return extractor.get_table_cardinality(table)
    except Exception as e:
        print(f"    Note: cardinality lookup unavailable ({e}), skipping cardinality lookup")
        return {"row_count": None, "columns": {}, "indexes": {}}


def _get_string_value_weights(cursor, database, table, column, cardinality):
    """Get per-value frequency weights for a low-cardinality string column.

    Queries GROUP BY to get actual frequency distribution. This is fast for
    low-cardinality columns (cardinality <= STRING_CARDINALITY_THRESHOLD).
    Returns list of (synthetic_value, count) — actual values are NOT used (privacy).
    """
    try:
        cursor.execute(
            f"SELECT `{column}`, COUNT(*) as cnt "
            f"FROM `{database}`.`{table}` "
            f"GROUP BY `{column}` ORDER BY cnt DESC"
        )
        rows = cursor.fetchall()
        if rows:
            # Return actual counts but with synthetic placeholder values
            # (real values discarded, only frequency distribution preserved)
            return [(f"val_{i}", cnt) for i, (_, cnt) in enumerate(rows, 1)]
    except Exception:
        pass

    # Fallback: equal weights
    return [(f"val_{i}", 1) for i in range(1, cardinality + 1)]


def get_date_range_expression(cursor, database, table, column, col_type):
    """Query min/max values for DATE/DATETIME/TIMESTAMP column."""
    cursor.execute(
        f"SELECT MIN(`{column}`), MAX(`{column}`) FROM `{database}`.`{table}`"
    )
    min_val, max_val = cursor.fetchone()

    if min_val is None or max_val is None:
        return None

    if col_type == "date":
        base_date = min_val.strftime("%Y-%m-%d 00:00:00")
        day_span = (max_val - min_val).days
        return f"TIMESTAMP '{base_date}' + INTERVAL rand.range(0, {day_span}) DAY"

    elif col_type in ("datetime", "timestamp"):
        base_ts = min_val.strftime("%Y-%m-%d %H:%M:%S")
        second_span = int((max_val - min_val).total_seconds())
        return f"TIMESTAMP '{base_ts}' + INTERVAL rand.range(0, {second_span}) SECOND"

    elif col_type == "time":
        min_secs = int(min_val.total_seconds())
        max_secs = int(max_val.total_seconds())
        return f"INTERVAL rand.range({min_secs}, {max_secs}) SECOND"

    return None


def annotate_table_with_statistics(extractor, database, table, generated_appendages=None):
    """
    Generate .dbgen file with statistical annotations.
    Uses schema extractor to get statistics from any supported database.
    """
    if generated_appendages is None:
        generated_appendages = {}

    # Get table DDL
    ddl = extractor.get_table_ddl(table)

    # Get column types
    column_types = extractor.get_columns(table)

    # Get primary keys
    primary_key_columns = extractor.get_primary_keys(table)

    # Get foreign keys
    foreign_keys = extractor.get_foreign_keys(table)

    # Analyze table to ensure statistics are up to date
    print(f"    Analyzing table {table}...")
    extractor.analyze_table(table)

    # Load cardinality metadata through the engine-specific extractor.
    table_cardinality = _load_table_cardinality(extractor, table)
    col_cardinality = table_cardinality.get("columns", {})
    table_row_count = table_cardinality.get("row_count")

    new_lines = []

    for line in ddl.splitlines():
        m = re.match(r"\s*`([^`]+)`", line)
        if not m:
            new_lines.append(line)
            continue

        col = m.group(1)
        col_type = column_types.get(col)
        synthetic = ""

        # FOREIGN KEY → use generated appendage if provided
        if col in foreign_keys:
            if col in generated_appendages:
                synthetic = generated_appendages[col]
            else:
                ref_table, ref_col = foreign_keys[col]
                comment = f"/*{{{{ @{col} := @{ref_col} }}}}*/"
                if line.rstrip().endswith(","):
                    line = line.rstrip()[:-1] + f" {comment},"
                else:
                    line = line + f" {comment}"

        # PRIMARY KEY or AUTO_INCREMENT
        elif (
            re.search(r"\bauto_increment\b", line, re.IGNORECASE)
            or col in primary_key_columns
        ):
            is_auto_increment = re.search(r"\bauto_increment\b", line, re.IGNORECASE)
            is_composite_pk = len(primary_key_columns) > 1
            is_fk = col in foreign_keys

            if is_composite_pk and not is_fk and not is_auto_increment:
                # Composite PK non-FK: generate repeating values to match source cardinality
                # (mirrors MySQL path's div/mod grouping logic)
                distinct_count = col_cardinality.get(col, 0)
                if distinct_count and distinct_count > 1:
                    # Estimate rows_per_value from source row count
                    try:
                        row_count = table_row_count or extractor.get_table_row_count(table)
                        rows_per_value = max(1, row_count // distinct_count)
                    except Exception:
                        rows_per_value = 1

                    if rows_per_value >= 2:
                        synthetic = f"div(rownum-1, {rows_per_value}) + 1"
                    else:
                        synthetic = f"mod(rownum-1, {distinct_count}) + 1"
                else:
                    synthetic = "rownum"
            else:
                # Single-column PK or AUTO_INCREMENT
                # Check if 0-based via histogram
                histogram = extractor.get_column_histogram(table, col)
                if histogram:
                    min_val, _ = get_min_max_from_histogram(histogram)
                    if min_val is not None:
                        try:
                            if int(float(min_val)) == 0:
                                synthetic = "rownum-1"
                            else:
                                synthetic = "rownum"
                        except (ValueError, TypeError):
                            synthetic = "rownum"
                    else:
                        synthetic = "rownum"
                else:
                    synthetic = "rownum"

        elif col_type in CHAR_TYPES:
            # Check cardinality — low-cardinality strings get weighted CASE expression
            card = col_cardinality.get(col, 0)
            col_max_length = get_string_column_length(line)
            if 0 < card <= STRING_CARDINALITY_THRESHOLD:
                values = _get_string_value_weights(
                    extractor.cursor, database, table, col, card)
                synthetic = string_values_to_case(values, col, max_length=col_max_length)
            else:
                synthetic = char_varchar_appendage(line)

        elif col_type in TEXT_TYPES:
            synthetic = text_appendage()

        elif col_type in DATETIME_TYPES:
            date_expr = get_date_range_expression(
                extractor.cursor, database, table, col, col_type
            )
            if date_expr:
                synthetic = date_expr
            else:
                synthetic = "rand.u31_timestamp()"

        elif col_type in YEAR:
            synthetic = "rand.range(1975,2025)"

        elif col_type in NUMERIC_TYPES:
            # Try to get histogram, with actual_distinct_count correction
            histogram = extractor.get_column_histogram(table, col)
            if histogram:
                actual_distinct = col_cardinality.get(col)
                synthetic = histogram_to_case(histogram, line, actual_distinct)
            else:
                synthetic = "rand.range(0,5)"

        else:
            synthetic = ""

        if synthetic:
            comment = f"/*{{{{ @{col} := {synthetic} }}}}*/"
            if line.rstrip().endswith(","):
                line = line.rstrip()[:-1] + f" {comment},"
            else:
                line = line + f" {comment}"

        new_lines.append(line)

    return "\n".join(new_lines)


def build_fk_appendages_from_source(extractor, table):
    """Build FK expressions by querying distinct counts from source DB.

    For each FK column, queries the source (parent) table to find the
    distinct count of the referenced column, then generates a
    rand.range(1, distinct_count + 1) expression.

    This works because parent tables use rownum for PK (values 1..N),
    so rand.range(1, N+1) guarantees valid FK references as long as
    parent and child are generated with the same row count.
    """
    foreign_keys = extractor.get_foreign_keys(table)
    if not foreign_keys:
        return {}

    appendages = {}
    for col, (ref_table, ref_col) in foreign_keys.items():
        try:
            distinct_count = extractor.get_distinct_count(ref_table, ref_col)
            if distinct_count > 0:
                appendages[col] = f"rand.range(1,{distinct_count + 1})"
                print(f"    FK {col} -> {ref_table}.{ref_col}: "
                      f"rand.range(1,{distinct_count + 1})")
        except Exception as e:
            print(f"    FK {col} -> {ref_table}.{ref_col}: "
                  f"could not query ({e}), using placeholder")

    return appendages


def main():
    parser = argparse.ArgumentParser(
        description='Extract schema from MySQL or SingleStore database',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Extract from MySQL
  %(prog)s --db-type mysql --host localhost --user root --database tpch

  # Extract from SingleStore
  %(prog)s --db-type singlestore --host prod.db.com --user admin --database mydb

  # With password from environment
  export DB_PASSWORD=secret
  %(prog)s --db-type mysql --host localhost --user root --database tpch --password-env DB_PASSWORD
        '''
    )

    parser.add_argument('--db-type', required=True, choices=available_extractor_types(),
                        help='Database type')
    parser.add_argument('--host', required=True, help='Database host')
    parser.add_argument('--port', type=int, default=3306, help='Database port (default: 3306)')
    parser.add_argument('--user', required=True, help='Database user')
    parser.add_argument('--password', help='Database password')
    parser.add_argument('--password-env', help='Environment variable containing password')
    parser.add_argument('--database', required=True, help='Database name')
    parser.add_argument('--output-dir', default='dbgen_files', help='Output directory for .dbgen files')

    args = parser.parse_args()

    # Get password
    password = args.password
    if args.password_env:
        password = os.environ.get(args.password_env)
        if not password:
            print(f"❌ Environment variable {args.password_env} not set")
            sys.exit(1)
    if not password:
        print("❌ Password required (use --password or --password-env)")
        sys.exit(1)

    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)

    # Create extractor
    print(f"\n{'='*60}")
    print(f"EXTRACTING SCHEMA FROM {args.db_type.upper()}")
    print(f"{'='*60}")
    print(f"Database: {args.database}")
    print(f"Host: {args.host}")
    print(f"Output: {args.output_dir}/")
    print()

    extractor = create_schema_extractor(
        args.db_type,
        args.host,
        args.user,
        password,
        args.database,
        args.port,
    )

    if not extractor.connect():
        sys.exit(1)

    try:
        # Get all tables
        all_tables = extractor.get_tables()
        print(f"Found {len(all_tables)} tables: {', '.join(all_tables)}\n")

        # Get dependencies and sort
        dependencies = extractor.get_table_dependencies()
        sorted_tables = topological_sort(all_tables, dependencies)
        print(f"Processing order: {' -> '.join(sorted_tables)}\n")

        # Process each table
        for i, table in enumerate(sorted_tables, 1):
            deps = dependencies.get(table, [])
            dep_str = f" (depends on: {', '.join(deps)})" if deps else ""
            print(f"[{i}/{len(sorted_tables)}] Processing: {table}{dep_str}")

            # Build FK expressions from source DB distinct counts
            fk_appendages = build_fk_appendages_from_source(extractor, table)

            ddl = annotate_table_with_statistics(
                extractor, args.database, table,
                generated_appendages=fk_appendages,
            )

            output_file = os.path.join(args.output_dir, f"{table}.dbgen")
            with open(output_file, "w") as f:
                f.write(ddl)

            print(f"    ✅ Generated {output_file}\n")

        print(f"{'='*60}")
        print(f"✅ SUCCESS: {len(sorted_tables)} .dbgen files created in {args.output_dir}/")
        print(f"{'='*60}")

    finally:
        extractor.close()


if __name__ == "__main__":
    main()
