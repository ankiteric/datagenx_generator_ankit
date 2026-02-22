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

# Maximum distinct values to fetch for string columns.
# Above this threshold, fall back to random string generation.
STRING_CARDINALITY_THRESHOLD = 1000


def char_varchar_appendage(ddl_line):
    """Fallback: generate random alphabetic string of the column's length."""
    m = re.search(r"\b(char|varchar)\s*\(\s*(\d+)\s*\)", ddl_line, re.IGNORECASE)
    if not m:
        return ""
    length = m.group(2)
    return f"rand.regex('[a-zA-Z ]{{{length}}}')"


def get_fk_range_expression(cursor, target_database, ref_table, ref_col):
    """Query min/max values from target database's referenced table.

    Uses exclusive upper bound: rand.range(min, max+1) generates min to max inclusive.
    """
    cursor.execute(
        f"SELECT MIN(`{ref_col}`), MAX(`{ref_col}`) FROM `{target_database}`.`{ref_table}`"
    )
    min_val, max_val = cursor.fetchone()

    if min_val is None or max_val is None:
        return "rand.range(0,1)"

    # rand.range uses exclusive upper bound, so add 1 to max
    return f"rand.range({min_val},{max_val + 1})"


def text_appendage():
    return "rand.regex('[a-zA-Z ]{100}')"


def get_string_column_values(cursor, database, table, column):
    """Query distinct values and their frequencies for a string column.

    Returns a list of (value, count) tuples sorted by count descending,
    or None if cardinality exceeds STRING_CARDINALITY_THRESHOLD.
    """
    # First check cardinality
    cursor.execute(
        f"SELECT COUNT(DISTINCT `{column}`) FROM `{database}`.`{table}`"
    )
    cardinality = cursor.fetchone()[0]

    if cardinality > STRING_CARDINALITY_THRESHOLD:
        return None

    # Fetch actual values and frequencies
    cursor.execute(f"""
        SELECT `{column}`, COUNT(*) as cnt
        FROM `{database}`.`{table}`
        WHERE `{column}` IS NOT NULL
        GROUP BY `{column}`
        ORDER BY cnt DESC
    """)
    return cursor.fetchall()


def string_values_to_case(values_with_counts, column_name):
    """Generate a weighted CASE expression for string values.

    values_with_counts: list of (value, count) tuples
    column_name: name of the column (used to generate synthetic values)
    Returns a dbgen expression like:
        case rand.weighted(array[0.25,0.50,0.25])
        when 1 then 'col_1___' when 2 then 'col_2___' when 3 then 'col_3___'
        end

    Uses synthetic values (column_name_N, padded to match original length)
    to avoid data leakage while preserving string length distribution.
    """
    if not values_with_counts:
        return ""

    total = sum(cnt for _, cnt in values_with_counts)
    if total == 0:
        return ""

    weights = [round(cnt / total, 6) for _, cnt in values_with_counts]

    case_lines = []
    for i, (value, _) in enumerate(values_with_counts, start=1):
        original_len = len(value) if value else 0
        base = f"{column_name}_{i}"

        if len(base) < original_len:
            # Pad with underscores to match original length
            synthetic_value = base + "_" * (original_len - len(base))
        elif len(base) > original_len:
            # Truncate but try to keep the number visible
            if original_len >= 3:
                # Keep at least the number at the end
                synthetic_value = base[:original_len]
            else:
                synthetic_value = str(i)[:original_len] if original_len > 0 else ""
        else:
            synthetic_value = base

        case_lines.append(f"when {i} then '{synthetic_value}'")

    return f"""case rand.weighted(array[{','.join(map(str, weights))}])
    {' '.join(case_lines)}
    end"""


def get_date_range_expression(cursor, database, table, column, col_type):
    """Query min/max values for a DATE/DATETIME/TIMESTAMP column and generate
    a dbgen expression that produces random values within that range.

    The base date and day span are derived by querying:
        SELECT MIN(column), MAX(column) FROM database.table

    For DATE columns, generates:
        TIMESTAMP 'YYYY-MM-DD' + INTERVAL rand.range(0, day_span) DAY

    For DATETIME/TIMESTAMP columns, generates:
        TIMESTAMP 'YYYY-MM-DD HH:MM:SS' + INTERVAL rand.range(0, second_span) SECOND

    Returns None if the column has no data or only NULL values.
    """
    cursor.execute(
        f"SELECT MIN(`{column}`), MAX(`{column}`) FROM `{database}`.`{table}`"
    )
    result = cursor.fetchone()
    min_val, max_val = result

    if min_val is None or max_val is None:
        return None

    if col_type == "date":
        # For DATE: use day-based offset
        # min_val and max_val are datetime.date objects
        # dbgen requires full timestamp format (YYYY-MM-DD HH:MM:SS)
        base_date = min_val.strftime("%Y-%m-%d 00:00:00")
        day_span = (max_val - min_val).days
        return f"TIMESTAMP '{base_date}' + INTERVAL rand.range(0, {day_span}) DAY"

    elif col_type in ("datetime", "timestamp"):
        # For DATETIME/TIMESTAMP: use second-based offset for finer granularity
        # min_val and max_val are datetime.datetime objects
        base_ts = min_val.strftime("%Y-%m-%d %H:%M:%S")
        second_span = int((max_val - min_val).total_seconds())
        return f"TIMESTAMP '{base_ts}' + INTERVAL rand.range(0, {second_span}) SECOND"

    elif col_type == "time":
        # TIME columns: generate random time within the observed range
        # min_val and max_val are datetime.timedelta objects
        min_secs = int(min_val.total_seconds())
        max_secs = int(max_val.total_seconds())
        span = max_secs - min_secs
        # dbgen doesn't have TIME literal, use interval from midnight
        return f"INTERVAL rand.range({min_secs}, {max_secs}) SECOND"

    return None


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


def annotate_table_with_histogram(host, user, password, database, table, target_database=None, generated_appendages=None):
    if generated_appendages is None:
        generated_appendages = {}
    if target_database is None:
        target_database = database  # Default to source if not specified

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

            # 🔴 FOREIGN KEY → query target database for actual range
            if col in foreign_keys:
                if col in generated_appendages:
                    synthetic = generated_appendages[col]
                else:
                    ref_table, ref_col = foreign_keys[col]
                    synthetic = get_fk_range_expression(cursor, target_database, ref_table, ref_col)

            # 🔴 PRIMARY KEY or AUTO_INCREMENT → rownum
            elif (
                re.search(r"\bauto_increment\b", line, re.IGNORECASE)
                or col in primary_key_columns
            ):
                synthetic = "rownum"

            elif col_type in CHAR_TYPES:
                # Try to get actual distinct values from the source table
                values = get_string_column_values(cursor, database, table, col)
                if values:
                    synthetic = string_values_to_case(values, col)
                else:
                    # High cardinality or empty — fall back to random strings
                    synthetic = char_varchar_appendage(line)

            elif col_type in TEXT_TYPES:
                synthetic = text_appendage()

            elif col_type in DATETIME_TYPES:
                # Query min/max from source to generate dates within actual range
                date_expr = get_date_range_expression(
                    cursor, database, table, col, col_type
                )
                if date_expr:
                    synthetic = date_expr
                else:
                    # Fallback if column is empty or all NULL
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
    target_database = "tpch_harsha"  # Target schema for FK range queries

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
            host, user, password, database, table, target_database
        )
        if ddl:
            with open(os.path.join(out_dir, f"{table}.dbgen"), "w") as f:
                f.write(ddl)
            print("   ✅ Done")
