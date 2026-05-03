#!/usr/bin/env python3
"""
TPC-H Query Plan Comparator

Compares EXPLAIN plans between original TPC-H schema and synthetic schema.
Automatically translates date parameters based on actual data ranges.

Usage:
    python compare_tpch_plans.py [--query N] [--verbose]

    --query N    Run only query N (1-22)
    --verbose    Show full EXPLAIN output
"""

import argparse
import json
import re
import sys
from pathlib import Path

import mysql.connector

from config import HOST, USER, PASSWORD, SOURCE_SCHEMA, TARGET_SCHEMA

# Configuration
ORIG_SCHEMA = SOURCE_SCHEMA
SYNTH_SCHEMA = TARGET_SCHEMA

# Query directories per schema
QUERIES_DIRS = {
    "tpch": Path.home() / "work/db/datagenx/tpch/tpch-dbgen/queries_mysql",
    "tpcds": Path.home() / "work/db/datagenx/tpcds/tpcds-kit/query_templates_mysql",
}

def get_queries_dir(schema):
    """Get the queries directory for a schema."""
    schema_lower = schema.lower()
    if schema_lower in QUERIES_DIRS:
        return QUERIES_DIRS[schema_lower]
    # Default: look for queries_mysql in current directory
    return Path("queries_mysql")

# TPC-H original date range (approximate)
ORIG_DATE_BASE = "1992-01-01"
ORIG_DATE_END = "1998-12-31"

def get_connection():
    return mysql.connector.connect(
        host=HOST,
        user=USER,
        password=PASSWORD,
        charset="utf8mb4"
    )

def get_tables_in_schema(conn, schema):
    """Get list of all tables in a schema."""
    cursor = conn.cursor()
    cursor.execute(f"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s", (schema,))
    tables = [row[0] for row in cursor.fetchall()]
    cursor.close()
    return tables

def analyze_all_tables(conn, tables):
    """Run ANALYZE TABLE on all tables in both schemas for consistent statistics."""
    cursor = conn.cursor()
    print("Refreshing table statistics...")
    for schema in [ORIG_SCHEMA, SYNTH_SCHEMA]:
        for table in tables:
            try:
                cursor.execute(f"ANALYZE TABLE `{schema}`.`{table}`")
                cursor.fetchall()  # consume results
            except Exception as e:
                print(f"  Warning: Could not analyze {schema}.{table}: {e}")
    cursor.close()
    print("  Done.\n")

def get_date_range(conn, schema, table, column):
    """Get MIN and MAX date from a table column."""
    cursor = conn.cursor()
    cursor.execute(f"SELECT MIN(`{column}`), MAX(`{column}`) FROM `{schema}`.`{table}`")
    min_dt, max_dt = cursor.fetchone()
    cursor.close()
    return min_dt, max_dt

def find_date_column(conn, schema, tables):
    """Find a date/datetime column in any table for date translation."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT TABLE_NAME, COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s
        AND DATA_TYPE IN ('date', 'datetime', 'timestamp')
        LIMIT 1
    """, (schema,))
    row = cursor.fetchone()
    cursor.close()
    if row:
        return row[0], row[1]
    return None, None

def get_sample_value(conn, schema, table, column):
    """Get a sample value from a column (for string parameters)."""
    cursor = conn.cursor()
    cursor.execute(f"SELECT `{column}` FROM `{schema}`.`{table}` LIMIT 1")
    row = cursor.fetchone()
    cursor.close()
    return row[0] if row else None

def translate_date(orig_date_str, orig_min, orig_max, synth_min, synth_max):
    """
    Translate a date from original range to synthetic range.
    Preserves relative position within the date span.
    """
    from datetime import datetime, timedelta

    # Parse dates
    if isinstance(orig_date_str, str):
        # Handle various formats
        orig_date_str = orig_date_str.replace("date ", "").replace("'", "")
        try:
            orig_date = datetime.strptime(orig_date_str, "%Y-%m-%d")
        except:
            return orig_date_str  # Can't parse, return as-is
    else:
        orig_date = orig_date_str

    orig_min_dt = datetime.strptime(ORIG_DATE_BASE, "%Y-%m-%d")
    orig_max_dt = datetime.strptime(ORIG_DATE_END, "%Y-%m-%d")

    # Calculate relative position (0.0 to 1.0)
    orig_span = (orig_max_dt - orig_min_dt).days
    if orig_span == 0:
        position = 0.5
    else:
        position = (orig_date - orig_min_dt).days / orig_span

    # Apply to synthetic range
    synth_span = (synth_max - synth_min).days
    synth_date = synth_min + timedelta(days=int(position * synth_span))

    return synth_date.strftime("%Y-%m-%d")

def transform_query_for_schema(query, schema, tables, date_translations=None, string_translations=None):
    """
    Transform a query to use a specific schema and translated parameters.
    """
    result = query

    # Replace table names with schema-qualified names
    # Only match in FROM/JOIN contexts to avoid replacing column aliases
    for table in tables:
        # Match table in FROM clause: "from table", "from\n\ttable", ", table"
        # or JOIN clause: "join table"
        patterns = [
            (rf'(\bfrom\s+){table}\b', rf'\1{schema}.{table}'),
            (rf'(,\s*){table}\b', rf'\1{schema}.{table}'),
            (rf'(\bjoin\s+){table}\b', rf'\1{schema}.{table}'),
        ]
        for pattern, replacement in patterns:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

    # Translate dates if provided
    if date_translations:
        for orig, synth in date_translations.items():
            result = result.replace(orig, f"'{synth}'")

    # Translate strings if provided
    if string_translations:
        for orig, synth in string_translations.items():
            result = result.replace(f"'{orig}'", f"'{synth}'")

    return result

def extract_date_literals(query):
    """Extract date literals from query."""
    # Match patterns like: date '1995-03-15' or '1995-03-15'
    pattern = r"date\s*'(\d{4}-\d{2}-\d{2})'"
    return re.findall(pattern, query, re.IGNORECASE)

def extract_string_params(query):
    """
    Extract string parameter values and their likely source columns.
    Returns dict of {value: (table, column)} based on common TPC-H patterns.
    """
    params = {}

    # Known TPC-H string parameters and their source columns
    patterns = [
        (r"c_mktsegment\s*=\s*'([^']+)'", "customer", "c_mktsegment"),
        (r"r_name\s*=\s*'([^']+)'", "region", "r_name"),
        (r"n_name\s*=\s*'([^']+)'", "nation", "n_name"),
        (r"p_type\s*=\s*'([^']+)'", "part", "p_type"),
        (r"p_type\s+like\s*'([^']+)'", "part", "p_type"),
        (r"p_brand\s*=\s*'([^']+)'", "part", "p_brand"),
        (r"p_container\s*=\s*'([^']+)'", "part", "p_container"),
        (r"l_shipmode\s+in\s*\(([^)]+)\)", "lineitem", "l_shipmode"),
        (r"l_shipinstruct\s*=\s*'([^']+)'", "lineitem", "l_shipinstruct"),
        (r"o_orderpriority\s*=\s*'([^']+)'", "orders", "o_orderpriority"),
        (r"s_comment\s+like\s*'([^']+)'", "supplier", "s_comment"),
    ]

    for pattern, table, column in patterns:
        matches = re.findall(pattern, query, re.IGNORECASE)
        for match in matches:
            # Handle IN clause with multiple values
            if "," in match:
                for val in match.split(","):
                    val = val.strip().strip("'")
                    params[val] = (table, column)
            else:
                params[match] = (table, column)

    return params

def run_explain(conn, query):
    """Run EXPLAIN and return results."""
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(f"EXPLAIN {query}")
        results = cursor.fetchall()
        return results
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        cursor.close()

def run_explain_json(conn, query):
    """Run EXPLAIN FORMAT=JSON and return parsed results."""
    cursor = conn.cursor()
    try:
        cursor.execute(f"EXPLAIN FORMAT=JSON {query}")
        result = cursor.fetchone()
        return json.loads(result[0]) if result else None
    except Exception as e:
        return {"error": str(e)}
    finally:
        cursor.close()

def compare_plans(orig_plan, synth_plan):
    """
    Compare two EXPLAIN plans and return comparison results.
    """
    if not orig_plan or not synth_plan:
        return {"match": False, "reason": "Missing plan data"}

    if "error" in orig_plan[0] or "error" in synth_plan[0]:
        return {"match": False, "reason": "Query error",
                "orig_error": orig_plan[0].get("error"),
                "synth_error": synth_plan[0].get("error")}

    differences = []

    # Compare number of steps
    if len(orig_plan) != len(synth_plan):
        differences.append(f"Plan steps differ: {len(orig_plan)} vs {len(synth_plan)}")

    # Compare each step
    for i, (o, s) in enumerate(zip(orig_plan, synth_plan)):
        step_diffs = []

        # Key attributes to compare
        if o.get("type") != s.get("type"):
            step_diffs.append(f"type: {o.get('type')} vs {s.get('type')}")

        # Compare key usage (normalize case for table names)
        o_key = o.get("key") or "NULL"
        s_key = s.get("key") or "NULL"
        if o_key.lower() != s_key.lower():
            step_diffs.append(f"key: {o_key} vs {s_key}")

        # Compare Extra (important for understanding execution strategy)
        o_extra = o.get("Extra") or ""
        s_extra = s.get("Extra") or ""
        # Normalize and compare key aspects
        o_flags = set(o_extra.lower().replace(";", ",").split(","))
        s_flags = set(s_extra.lower().replace(";", ",").split(","))
        o_flags = {f.strip() for f in o_flags if f.strip()}
        s_flags = {f.strip() for f in s_flags if f.strip()}
        if o_flags != s_flags:
            step_diffs.append(f"Extra: {o_extra} vs {s_extra}")

        if step_diffs:
            differences.append(f"Step {i+1}: " + "; ".join(step_diffs))

    return {
        "match": len(differences) == 0,
        "differences": differences
    }

def format_plan_summary(plan):
    """Format plan as a compact summary."""
    if not plan or "error" in plan[0]:
        return "ERROR"

    parts = []
    for row in plan:
        table = row.get("table", "?")
        access = row.get("type", "?")
        key = row.get("key") or "NONE"
        parts.append(f"{table}:{access}({key})")
    return " -> ".join(parts)

def format_join_order(plan):
    """Format plan as a readable join order string."""
    if not plan or "error" in plan[0]:
        return "ERROR"

    parts = []
    for row in plan:
        table = row.get("table", "?")
        access_type = row.get("type", "?")
        key = row.get("key")

        if key and key != "NULL":
            if "PRIMARY" in key:
                parts.append(f"{table} via PK")
            elif key.startswith("FK_"):
                parts.append(f"{table} via FK")
            else:
                parts.append(f"{table} via {key}")
        elif access_type == "ALL":
            extra = row.get("Extra") or ""
            if "hash join" in extra.lower():
                parts.append(f"{table}:ALL(hash)")
            else:
                parts.append(f"{table}:ALL")
        else:
            parts.append(f"{table}:{access_type}")

    return " → ".join(parts)

def infer_root_cause(orig_plan, synth_plan, differences):
    """Infer the root cause of plan differences."""
    if not orig_plan or not synth_plan:
        return "Missing plan data"

    if "error" in orig_plan[0] or "error" in synth_plan[0]:
        return "Query execution error"

    # Extract table sequences
    orig_tables = [r.get("table", "?") for r in orig_plan]
    synth_tables = [r.get("table", "?") for r in synth_plan]

    # Check for join order changes
    if orig_tables != synth_tables:
        # Find which tables swapped
        for i, (o, s) in enumerate(zip(orig_tables, synth_tables)):
            if o != s:
                # Identify the tables involved
                tables_involved = set([o, s])
                if "customer" in tables_involved and "orders" in tables_involved:
                    return "Different customer/orders cardinality"
                elif "nation" in tables_involved:
                    return "Different nation cardinality stats"
                elif "supplier" in tables_involved:
                    return "Different supplier cardinality stats"
                else:
                    return f"Join order changed: {o} ↔ {s}"

    # Check for access type changes
    for diff in differences:
        if "type:" in diff:
            if "ref vs ALL" in diff or "ALL vs ref" in diff:
                return "Index usage decision differs"
            if "eq_ref vs ref" in diff or "ref vs eq_ref" in diff:
                return "Join method differs (unique vs non-unique)"

    # Check for key usage changes
    for diff in differences:
        if "key:" in diff:
            if "FK_" in diff:
                return "FK index usage differs"
            if "PRIMARY" in diff:
                return "PK access method differs"

    return "Minor execution strategy difference"

def print_difference_table(diff_details):
    """Print a formatted table of plan differences with root causes."""
    if not diff_details:
        return

    # Calculate column widths
    col1_width = 5   # Query
    col2_width = 28  # Original Join Order
    col3_width = 28  # Synthetic Join Order
    col4_width = 40  # Root Cause

    # Box drawing characters
    tl, tr, bl, br = "┌", "┐", "└", "┘"
    h, v = "─", "│"
    lm, rm, tm, bm, cross = "├", "┤", "┬", "┴", "┼"

    def pad(s, width):
        return s[:width].ljust(width)

    # Header
    print()
    print(f"{tl}{h*(col1_width+2)}{tm}{h*(col2_width+2)}{tm}{h*(col3_width+2)}{tm}{h*(col4_width+2)}{tr}")
    print(f"{v} {pad('Query', col1_width)} {v} {pad('Original Join Order', col2_width)} {v} {pad('Synthetic Join Order', col3_width)} {v} {pad('Root Cause', col4_width)} {v}")
    print(f"{lm}{h*(col1_width+2)}{cross}{h*(col2_width+2)}{cross}{h*(col3_width+2)}{cross}{h*(col4_width+2)}{rm}")

    # Rows
    for i, detail in enumerate(diff_details):
        query = f"Q{detail['query']}"
        orig = detail['orig_join']
        synth = detail['synth_join']
        cause = detail['root_cause']

        print(f"{v} {pad(query, col1_width)} {v} {pad(orig, col2_width)} {v} {pad(synth, col3_width)} {v} {pad(cause, col4_width)} {v}")

        if i < len(diff_details) - 1:
            print(f"{lm}{h*(col1_width+2)}{cross}{h*(col2_width+2)}{cross}{h*(col3_width+2)}{cross}{h*(col4_width+2)}{rm}")

    # Footer
    print(f"{bl}{h*(col1_width+2)}{bm}{h*(col2_width+2)}{bm}{h*(col3_width+2)}{bm}{h*(col4_width+2)}{br}")

def process_query(conn, query_num, query_text, tables, verbose=False):
    """Process a single query and compare plans. Returns (match, detail_dict)."""
    print(f"\n{'='*60}")
    print(f"Query {query_num}")
    print('='*60)

    # Extract dates and get synthetic date range
    dates = extract_date_literals(query_text)
    date_translations = {}

    if dates:
        # Try to find a date column to get synthetic range
        date_table, date_col = find_date_column(conn, SYNTH_SCHEMA, tables)
        if date_table and date_col:
            synth_min, synth_max = get_date_range(conn, SYNTH_SCHEMA, date_table, date_col)
            if synth_min and synth_max:
                for date_str in dates:
                    translated = translate_date(date_str, None, None, synth_min, synth_max)
                    date_translations[f"date '{date_str}'"] = translated
                    if verbose:
                        print(f"  Date: {date_str} -> {translated}")

    # Extract string parameters
    string_params = extract_string_params(query_text)
    string_translations = {}

    for value, (table, column) in string_params.items():
        synth_value = get_sample_value(conn, SYNTH_SCHEMA, table, column)
        if synth_value:
            string_translations[value] = synth_value
            if verbose:
                print(f"  String: '{value}' -> '{synth_value}'")

    # Transform queries
    orig_query = transform_query_for_schema(query_text, ORIG_SCHEMA, tables)
    synth_query = transform_query_for_schema(
        query_text, SYNTH_SCHEMA, tables,
        date_translations, string_translations
    )

    if verbose:
        print(f"\nOriginal query:\n{orig_query[:200]}...")
        print(f"\nSynthetic query:\n{synth_query[:200]}...")

    # Run EXPLAIN
    orig_plan = run_explain(conn, orig_query)
    synth_plan = run_explain(conn, synth_query)

    # Compare
    comparison = compare_plans(orig_plan, synth_plan)

    # Output
    print(f"\nOriginal plan:  {format_plan_summary(orig_plan)}")
    print(f"Synthetic plan: {format_plan_summary(synth_plan)}")

    if comparison["match"]:
        print(f"\n  MATCH - Plans are equivalent")
    else:
        print(f"\n  DIFFER")
        if "reason" in comparison:
            print(f"    Reason: {comparison['reason']}")
        for diff in comparison.get("differences", []):
            print(f"    - {diff}")

    if verbose:
        print("\nOriginal EXPLAIN:")
        for row in orig_plan:
            print(f"  {row}")
        print("\nSynthetic EXPLAIN:")
        for row in synth_plan:
            print(f"  {row}")

    # Build detail dict for differences
    detail = None
    if not comparison["match"]:
        detail = {
            "query": query_num,
            "orig_join": format_join_order(orig_plan),
            "synth_join": format_join_order(synth_plan),
            "root_cause": infer_root_cause(orig_plan, synth_plan, comparison.get("differences", [])),
            "orig_plan": orig_plan,
            "synth_plan": synth_plan,
        }

    return comparison["match"], detail

def main():
    parser = argparse.ArgumentParser(description="Compare TPC-H query plans")
    parser.add_argument("--query", "-q", type=int, help="Run only query N (1-22)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    conn = get_connection()

    # Get tables from source schema
    tables = get_tables_in_schema(conn, ORIG_SCHEMA)
    print(f"Schema: {ORIG_SCHEMA} ({len(tables)} tables)")

    # Refresh statistics for consistent results
    analyze_all_tables(conn, tables)

    # Find query files
    queries_dir = get_queries_dir(ORIG_SCHEMA)
    if not queries_dir.exists():
        print(f"Error: Queries directory not found: {queries_dir}")
        conn.close()
        return 1

    if args.query:
        query_files = [queries_dir / f"{args.query}_mysql.sql"]
    else:
        query_files = sorted(queries_dir.glob("*_mysql.sql"),
                            key=lambda p: int(p.stem.split("_")[0]))

    results = {"match": 0, "differ": 0, "error": 0}
    diff_details = []

    for query_file in query_files:
        if not query_file.exists():
            print(f"Query file not found: {query_file}")
            continue

        query_num = int(query_file.stem.split("_")[0])
        query_text = query_file.read_text()

        # Skip comment lines
        lines = [l for l in query_text.split("\n") if not l.strip().startswith("--")]
        query_text = "\n".join(lines)

        try:
            match, detail = process_query(conn, query_num, query_text, tables, args.verbose)
            if match:
                results["match"] += 1
            else:
                results["differ"] += 1
                if detail:
                    diff_details.append(detail)
        except Exception as e:
            print(f"\nQuery {query_num}: ERROR - {e}")
            results["error"] += 1

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print('='*60)
    print(f"  Matching plans:  {results['match']}")
    print(f"  Different plans: {results['differ']}")
    print(f"  Errors:          {results['error']}")

    # Print detailed difference table
    if diff_details:
        print_difference_table(diff_details)

    conn.close()
    return 0 if results["differ"] == 0 and results["error"] == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
