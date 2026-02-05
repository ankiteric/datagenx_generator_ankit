import mysql.connector
from mysql.connector import Error
import re
import json
from datetime import datetime
import os


NUMERIC_TYPES = {
    "tinyint", "smallint", "mediumint", "int", "bigint",
    "decimal", "numeric", "float", "double"
}

DATETIME_TYPES = {
    "date", "datetime", "timestamp", "time"
}

CHAR_TYPES = {"char", "varchar"}
TEXT_TYPES = {"text", "blob"}
YEAR = {"year"}


def char_varchar_appendage(ddl_line):
    m = re.search(r"\b(char|varchar)\s*\(\s*(\d+)\s*\)", ddl_line, re.IGNORECASE)
    if not m:
        return ""
    length = m.group(2)
    return f"rand.regex('[a-zA-Z ]{{{length}}}')"


def text_appendage():
    return "rand.regex('[a-zA-Z ]{100}')"


def histogram_to_case(hist, ddl_line):
    buckets = hist.get("buckets", [])
    if not buckets:
        return ""

    try:
        float(buckets[0][0])
    except (ValueError, TypeError, IndexError):
        return ""

    hist_type = hist["histogram-type"]

    decimal_match = re.search(
        r"decimal\(\s*\d+\s*,\s*(\d+)\s*\)",
        ddl_line,
        re.IGNORECASE
    )
    scale = 10 ** int(decimal_match.group(1)) if decimal_match else 1

    weights, ranges = [], []
    prev = 0.0

    for b in buckets:
        cumulative = b[-2] if hist_type == "equi-height" else b[1]
        weights.append(round(cumulative - prev, 5))
        prev = cumulative

    for b in buckets:
        if hist_type == "singleton":
            v = int(round(float(b[0]) * scale))
            ranges.append((v, v))
        else:
            ranges.append((
                int(round(float(b[0]) * scale)),
                int(round(float(b[1]) * scale))
            ))

    case_lines = []
    for i, (lo, hi) in enumerate(ranges, start=1):
        if lo == hi:
            case_lines.append(f"when {i} then {lo / scale}")
        else:
            span = hi - lo
            if scale == 1:
                case_lines.append(f"when {i} then rand.range(0,{span})+{lo}")
            else:
                case_lines.append(
                    f"when {i} then rand.range(0,{span})/{scale}+{lo/scale}"
                )

    return f"""case rand.weighted(array[{','.join(map(str, weights))}])
    {' '.join(case_lines)}
    end"""


def annotate_table_with_histogram(host, user, password, database, table, generated_appendages=None):
    if generated_appendages is None:
        generated_appendages = {}

    try:
        conn = mysql.connector.connect(
            host=host, user=user, password=password, database=database
        )
        cursor = conn.cursor()

        # CREATE TABLE
        cursor.execute(f"SHOW CREATE TABLE `{database}`.`{table}`")
        ddl = cursor.fetchone()[1]

        # Column types
        cursor.execute("""
            SELECT COLUMN_NAME, DATA_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
        """, (database, table))
        column_types = {c: t.lower() for c, t in cursor.fetchall()}

        # PRIMARY KEY columns
        cursor.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
              AND CONSTRAINT_NAME = 'PRIMARY'
        """, (database, table))
        primary_key_columns = {r[0] for r in cursor.fetchall()}

        # FOREIGN KEY mappings: column -> (referenced_table, referenced_column)
        cursor.execute("""
            SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
              AND REFERENCED_TABLE_NAME IS NOT NULL
        """, (database, table))
        foreign_keys = {
            col: (ref_table, ref_col)
            for col, ref_table, ref_col in cursor.fetchall()
        }

        analyze_and_update_histograms(cursor, database, table)

        # Histograms
        cursor.execute("""
            SELECT COLUMN_NAME, HISTOGRAM
            FROM information_schema.column_statistics
            WHERE SCHEMA_NAME = %s AND TABLE_NAME = %s
        """, (database, table))
        histograms = {
            col: json.loads(hist)
            for col, hist in cursor.fetchall()
            if hist
        }

        new_lines = []

        for line in ddl.splitlines():
            m = re.match(r"\s*`([^`]+)`", line)
            if not m:
                new_lines.append(line)
                continue

            col = m.group(1)
            col_type = column_types.get(col)
            synthetic = ""

            # 🔴 FOREIGN KEY → use generated appendage if available, else @ref_col
            if col in foreign_keys:
                if col in generated_appendages:
                    synthetic = generated_appendages[col]
                else:
                    ref_table, ref_col = foreign_keys[col]
                    synthetic = f"@{ref_col}"

            # 🔴 PRIMARY KEY or AUTO_INCREMENT → rownum
            elif (
                re.search(r"\bauto_increment\b", line, re.IGNORECASE)
                or col in primary_key_columns
            ):
                synthetic = "rownum"

            elif col_type in CHAR_TYPES:
                synthetic = char_varchar_appendage(line)

            elif col_type in TEXT_TYPES:
                synthetic = text_appendage()

            elif col_type in DATETIME_TYPES:
                synthetic = "rand.u31_timestamp()"

            elif col_type in YEAR:
                synthetic = "rand.range(1975,2025)"

            elif col_type in NUMERIC_TYPES:
                if col in histograms:
                    synthetic = histogram_to_case(histograms[col], line)
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

    except Error as e:
        print("❌ Error:", e)
        return None
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def analyze_and_update_histograms(cursor, database, table):
    cursor.execute(f"ANALYZE TABLE `{database}`.`{table}`")
    cursor.fetchall()

    cursor.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
          AND DATA_TYPE IN (
              'tinyint','smallint','mediumint','int','bigint',
              'decimal','numeric','float','double'
          )
    """, (database, table))

    cols = [c[0] for c in cursor.fetchall()]
    if not cols:
        return

    cursor.execute(
        f"""
        ANALYZE TABLE `{database}`.`{table}`
        UPDATE HISTOGRAM ON {','.join(f'`{c}`' for c in cols)}
        WITH 100 BUCKETS
        """
    )
    cursor.fetchall()


def topological_sort(tables, dependencies):
    """
    Sort tables in dependency order using topological sort.
    dependencies is a dict: {table: [list of tables it depends on]}
    """
    # Build in-degree map and adjacency list
    in_degree = {table: 0 for table in tables}
    graph = {table: [] for table in tables}

    for table in tables:
        for dep in dependencies.get(table, []):
            if dep in graph:  # Only consider dependencies within our table set
                graph[dep].append(table)
                in_degree[table] += 1

    # Find all tables with no dependencies
    queue = [table for table in tables if in_degree[table] == 0]
    result = []

    while queue:
        # Sort queue for deterministic output
        queue.sort()
        current = queue.pop(0)
        result.append(current)

        # Reduce in-degree for dependent tables
        for dependent in graph[current]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    # Check for circular dependencies
    if len(result) != len(tables):
        # Add remaining tables (circular dependencies) at the end
        remaining = [t for t in tables if t not in result]
        result.extend(sorted(remaining))

    return result


if __name__ == "__main__":
    host = "localhost"
    user = "root"
    password = "newpassword"
    database = "tpch"

    conn = mysql.connector.connect(
        host=host, user=user, password=password, database=database
    )
    cursor = conn.cursor()

    # Get all tables
    cursor.execute("""
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
    """, (database,))
    all_tables = [t[0] for t in cursor.fetchall()]

    # Build dependency map: table -> [tables it depends on]
    cursor.execute("""
        SELECT TABLE_NAME, REFERENCED_TABLE_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND REFERENCED_TABLE_NAME IS NOT NULL
    """, (database,))

    dependencies = {}
    for table, referenced_table in cursor.fetchall():
        if table not in dependencies:
            dependencies[table] = set()
        if referenced_table and referenced_table != table:  # Avoid self-references
            dependencies[table].add(referenced_table)

    # Convert sets to lists for easier use
    dependencies = {k: list(v) for k, v in dependencies.items()}

    cursor.close()
    conn.close()

    # Sort tables in dependency order
    sorted_tables = topological_sort(all_tables, dependencies)

    out_dir = "dbgen_files"

    # Clean up old files if directory exists
    if os.path.exists(out_dir):
        for file in os.listdir(out_dir):
            file_path = os.path.join(out_dir, file)
            if os.path.isfile(file_path):
                os.remove(file_path)
    else:
        os.makedirs(out_dir)

    print("=" * 60)
    print("PROCESSING TABLES IN DEPENDENCY ORDER")
    print("=" * 60)
    print(f"Order: {' -> '.join(sorted_tables)}\n")

    for table in sorted_tables:
        deps = dependencies.get(table, [])
        if deps:
            print(f"⚙️ Processing table: {table} (depends on: {', '.join(deps)})")
        else:
            print(f"⚙️ Processing table: {table} (no dependencies)")

        ddl = annotate_table_with_histogram(
            host, user, password, database, table
        )
        if ddl:
            with open(os.path.join(out_dir, f"{table}.dbgen"), "w") as f:
                f.write(ddl)
            print("   ✅ Done")
