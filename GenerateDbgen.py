import mysql.connector
from mysql.connector import Error
import re
import json
from datetime import datetime
import os
import base64


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


def decode_histogram_string(raw_value):
    """Decode a string value from MySQL histogram.

    MySQL stores string values in histograms as base64-encoded strings.
    This function decodes the base64 and strips trailing whitespace
    (CHAR columns are space-padded).

    Returns the decoded string value.
    """
    if not isinstance(raw_value, str):
        return str(raw_value).rstrip()

    # Try base64 decoding
    try:
        # Ensure proper padding (base64 strings should be padded to multiple of 4)
        padded = raw_value + '=' * (-len(raw_value) % 4)
        decoded_bytes = base64.b64decode(padded)
        decoded_str = decoded_bytes.decode('utf-8')
        return decoded_str.rstrip()
    except Exception:
        pass

    # If base64 fails, use the raw value (strip trailing whitespace)
    return raw_value.rstrip()


def get_string_column_length(ddl_line):
    """Extract the length from a CHAR or VARCHAR column definition."""
    m = re.search(r"\b(char|varchar)\s*\(\s*(\d+)\s*\)", ddl_line, re.IGNORECASE)
    if m:
        return int(m.group(2))
    return None


def char_varchar_appendage(ddl_line):
    """Fallback: generate random alphabetic string of the column's length."""
    length = get_string_column_length(ddl_line)
    if length is None:
        return ""
    return f"rand.regex('[a-zA-Z ]{{{length}}}')"


def get_min_max_from_histogram(histogram):
    """Extract min and max values from a MySQL histogram JSON structure.

    Returns (min_val, max_val) tuple, or (None, None) if extraction fails.
    """
    if not histogram:
        return None, None

    buckets = histogram.get("buckets", [])
    if not buckets:
        return None, None

    hist_type = histogram.get("histogram-type")

    if hist_type == "singleton":
        # Singleton: each bucket is [value, cumulative_frequency]
        min_val = buckets[0][0]
        max_val = buckets[-1][0]
    elif hist_type == "equi-height":
        # Equi-height: each bucket is [min, max, cumulative_frequency, num_distinct]
        min_val = buckets[0][0]
        max_val = buckets[-1][1]
    else:
        return None, None

    return min_val, max_val


def get_fk_range_expression(cursor, target_database, ref_table, ref_col):
    """Get min/max values from histogram metadata for the referenced column.

    Uses exclusive upper bound: rand.range(min, max+1) generates min to max inclusive.
    """
    cursor.execute("""
        SELECT HISTOGRAM
        FROM information_schema.column_statistics
        WHERE SCHEMA_NAME = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
    """, (target_database, ref_table, ref_col))

    result = cursor.fetchone()
    if not result or not result[0]:
        return "rand.range(0,1)"

    histogram = json.loads(result[0])
    min_val, max_val = get_min_max_from_histogram(histogram)

    if min_val is None or max_val is None:
        return "rand.range(0,1)"

    # Convert to int (histogram stores numeric values as floats/strings)
    min_val = int(float(min_val))
    max_val = int(float(max_val))

    # rand.range uses exclusive upper bound, so add 1 to max
    return f"rand.range({min_val},{max_val + 1})"


def text_appendage():
    return "rand.regex('[a-zA-Z ]{100}')"


def get_string_column_values(cursor, database, table, column):
    """Get distinct string values and frequencies from histogram metadata.

    Returns a list of (value, count) tuples sorted by count descending,
    or None if histogram doesn't exist, is equi-height, or cardinality
    exceeds STRING_CARDINALITY_THRESHOLD.

    Note: MySQL stores string values in histograms as base64-encoded strings.
    """
    cursor.execute("""
        SELECT HISTOGRAM
        FROM information_schema.column_statistics
        WHERE SCHEMA_NAME = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
    """, (database, table, column))

    result = cursor.fetchone()
    if not result or not result[0]:
        return None

    histogram = json.loads(result[0])
    hist_type = histogram.get("histogram-type")
    buckets = histogram.get("buckets", [])

    if not buckets:
        return None

    # Only singleton histograms contain individual values
    if hist_type != "singleton":
        return None

    # Check cardinality threshold
    if len(buckets) > STRING_CARDINALITY_THRESHOLD:
        return None

    # Extract values and convert cumulative frequencies to individual frequencies
    # Singleton buckets: [base64_value, cumulative_frequency]
    # Scale frequencies to integer counts for compatibility with string_values_to_case
    values_with_counts = []
    prev_cum_freq = 0.0

    for bucket in buckets:
        raw_value = bucket[0]
        cum_freq = bucket[1]
        freq = cum_freq - prev_cum_freq
        prev_cum_freq = cum_freq

        # Decode base64-encoded string value (MySQL encodes string histogram values)
        # First check if it looks like base64 (contains only valid base64 chars)
        value = decode_histogram_string(raw_value)

        # Scale to pseudo-count (maintains relative weights)
        count = int(freq * 1000000)
        values_with_counts.append((value, count))

    # Sort by count descending
    values_with_counts.sort(key=lambda x: x[1], reverse=True)

    return values_with_counts


def string_values_to_case(values_with_counts, column_name, max_length=None):
    """Generate a weighted CASE expression for string values.

    values_with_counts: list of (value, count) tuples
    column_name: name of the column (used to generate synthetic values)
    max_length: optional maximum length for generated strings (from column definition)
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
        # Cap at max_length if provided (to handle CHAR/VARCHAR limits)
        if max_length is not None and original_len > max_length:
            original_len = max_length
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

        # Final safety check - truncate if still too long
        if max_length is not None and len(synthetic_value) > max_length:
            synthetic_value = synthetic_value[:max_length]

        case_lines.append(f"when {i} then '{synthetic_value}'")

    return f"""case rand.weighted(array[{','.join(map(str, weights))}])
    {' '.join(case_lines)}
    end"""


def get_date_range_expression(cursor, database, table, column, col_type):
    """Get min/max values from histogram metadata for DATE/DATETIME/TIMESTAMP columns
    and generate a dbgen expression that produces random values within that range.

    For DATE columns, generates:
        TIMESTAMP 'YYYY-MM-DD' + INTERVAL rand.range(0, day_span) DAY

    For DATETIME/TIMESTAMP columns, generates:
        TIMESTAMP 'YYYY-MM-DD HH:MM:SS' + INTERVAL rand.range(0, second_span) SECOND

    Returns None if histogram doesn't exist for the column.
    """
    cursor.execute("""
        SELECT HISTOGRAM
        FROM information_schema.column_statistics
        WHERE SCHEMA_NAME = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
    """, (database, table, column))

    result = cursor.fetchone()
    if not result or not result[0]:
        return None

    histogram = json.loads(result[0])
    min_val, max_val = get_min_max_from_histogram(histogram)

    if min_val is None or max_val is None:
        return None

    # MySQL stores dates in histograms as strings in format 'YYYY-MM-DD HH:MM:SS.ffffff'
    if col_type == "date":
        # Parse date strings from histogram
        min_date = datetime.strptime(min_val[:10], "%Y-%m-%d").date()
        max_date = datetime.strptime(max_val[:10], "%Y-%m-%d").date()
        base_date = min_date.strftime("%Y-%m-%d 00:00:00")
        day_span = (max_date - min_date).days
        return f"TIMESTAMP '{base_date}' + INTERVAL rand.range(0, {day_span}) DAY"

    elif col_type in ("datetime", "timestamp"):
        # Parse datetime strings from histogram (handle optional microseconds)
        fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in min_val else "%Y-%m-%d %H:%M:%S"
        min_ts = datetime.strptime(min_val, fmt)
        fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in max_val else "%Y-%m-%d %H:%M:%S"
        max_ts = datetime.strptime(max_val, fmt)
        base_ts = min_ts.strftime("%Y-%m-%d %H:%M:%S")
        second_span = int((max_ts - min_ts).total_seconds())
        return f"TIMESTAMP '{base_ts}' + INTERVAL rand.range(0, {second_span}) SECOND"

    elif col_type == "time":
        # TIME is stored as 'HH:MM:SS' or 'HH:MM:SS.ffffff'
        def parse_time_to_secs(t):
            parts = t.split(":")
            h, m = int(parts[0]), int(parts[1])
            s = float(parts[2]) if len(parts) > 2 else 0
            return h * 3600 + m * 60 + int(s)

        min_secs = parse_time_to_secs(min_val)
        max_secs = parse_time_to_secs(max_val)
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

        # Ensure histograms exist on FK referenced columns in target database
        if foreign_keys:
            ensure_fk_histograms(cursor, target_database, foreign_keys)

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

            # 🔴 FOREIGN KEY → get range from histogram metadata
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
                # Try to get distinct values from histogram metadata
                values = get_string_column_values(cursor, database, table, col)
                col_max_length = get_string_column_length(line)
                if values:
                    synthetic = string_values_to_case(values, col, max_length=col_max_length)
                else:
                    # High cardinality or empty — fall back to random strings
                    synthetic = char_varchar_appendage(line)

            elif col_type in TEXT_TYPES:
                synthetic = text_appendage()

            elif col_type in DATETIME_TYPES:
                # Get min/max from histogram metadata to generate dates within range
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
    """Create/update histograms for all relevant column types.

    Creates histograms for:
    - Numeric columns (for value distribution)
    - Date/DateTime/Timestamp columns (for min/max extraction)
    - Char/Varchar columns (for distinct value extraction)
    """
    cursor.execute(f"ANALYZE TABLE `{database}`.`{table}`")
    cursor.fetchall()

    # Get all columns that need histograms
    cursor.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
          AND DATA_TYPE IN (
              'tinyint','smallint','mediumint','int','bigint',
              'decimal','numeric','float','double',
              'date','datetime','timestamp','time',
              'char','varchar'
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


def ensure_fk_histograms(cursor, target_database, foreign_keys):
    """Ensure histograms exist on FK referenced columns in target database.

    foreign_keys: dict of {column: (ref_table, ref_col)}
    """
    # Group by table to minimize ANALYZE calls
    tables_columns = {}
    for col, (ref_table, ref_col) in foreign_keys.items():
        if ref_table not in tables_columns:
            tables_columns[ref_table] = set()
        tables_columns[ref_table].add(ref_col)

    for ref_table, ref_cols in tables_columns.items():
        # Check which columns already have histograms
        cursor.execute("""
            SELECT COLUMN_NAME
            FROM information_schema.column_statistics
            WHERE SCHEMA_NAME = %s AND TABLE_NAME = %s AND COLUMN_NAME IN ({})
        """.format(','.join(['%s'] * len(ref_cols))),
            (target_database, ref_table, *ref_cols))

        existing = {row[0] for row in cursor.fetchall()}
        missing = ref_cols - existing

        if missing:
            cursor.execute(
                f"""
                ANALYZE TABLE `{target_database}`.`{ref_table}`
                UPDATE HISTOGRAM ON {','.join(f'`{c}`' for c in missing)}
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
