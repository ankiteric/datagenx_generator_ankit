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

# Synthetic base date for generating date values.
# We use a synthetic date range to avoid exposing actual source data dates.
# This date is arbitrary - what matters is the distribution shape, not actual values.
SYNTHETIC_BASE_DATE = "2000-01-01"
SYNTHETIC_BASE_DATETIME = "2000-01-01 00:00:00"


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


def build_single_fk_expression(cursor, source_db, target_db, table, col, ref_table, ref_col):
    """Build expression for a single-column FK. Returns (expression, description).

    Tries sparse approach first (for low-cardinality FKs with singleton histograms),
    falls back to dense approach (rand.range) for high-cardinality FKs.

    This is the SINGLE SOURCE OF TRUTH for FK expression generation.
    Called by both MasterRun.py and GenerateDbgen.py.

    Args:
        cursor: MySQL cursor
        source_db: Source schema name (for reading FK column's histogram)
        target_db: Target schema name (for sampling values from referenced table)
        table: Table containing the FK column
        col: FK column name
        ref_table: Referenced table name
        ref_col: Referenced column name

    Returns:
        (expression, description) tuple where:
        - expression: dbgen expression string
        - description: human-readable description for logging
    """
    # First, try sparse approach (for low-cardinality FKs)
    sparse_result = _try_sparse_fk_expression(cursor, source_db, target_db, table, col, ref_table, ref_col)
    if sparse_result:
        return sparse_result

    # Fall back to dense approach
    return _build_dense_fk_expression(cursor, target_db, ref_table, ref_col)


def _try_sparse_fk_expression(cursor, source_db, target_db, table, col, ref_table, ref_col):
    """Try to build sparse FK expression using FK column's own histogram.

    PRIVACY: This function is privacy-safe because:
    - It uses distribution weights from source histogram (statistical pattern only)
    - It samples PK values from TARGET schema (already synthetically generated)
    - No actual source data values are used

    Returns (expression, description) tuple if sparse approach is applicable,
    or None if dense approach should be used instead.
    """
    # Get FK column's histogram from source schema
    cursor.execute("""
        SELECT HISTOGRAM
        FROM information_schema.column_statistics
        WHERE SCHEMA_NAME = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
    """, (source_db, table, col))

    result = cursor.fetchone()
    if not result or not result[0]:
        return None  # No histogram - use dense approach

    histogram = json.loads(result[0])
    hist_type = histogram.get("histogram-type")
    buckets = histogram.get("buckets", [])

    if not buckets:
        return None

    # Get referenced table size to determine if FK has low cardinality
    cursor.execute(f"SELECT COUNT(*) FROM `{target_db}`.`{ref_table}`")
    ref_table_size = cursor.fetchone()[0]

    if hist_type == "singleton":
        # Singleton histogram - use sparse approach directly
        n_distinct = len(buckets)
        return _build_singleton_fk_expression(
            cursor, target_db, ref_table, ref_col, buckets, n_distinct
        )

    elif hist_type == "equi-height":
        # Equi-height histogram - check if FK has low cardinality relative to referenced table
        #
        # IMPORTANT: Histogram's estimated_distinct (sum of bucket[3]) is unreliable.
        # MySQL samples during ANALYZE TABLE, so estimates can be off by 10-50x.
        # Example: ss_item_sk histogram claims ~500 distinct, actual is 18,000.
        #
        # We query the ACTUAL distinct count from source instead.
        # COUNT(DISTINCT) doesn't expose values - it's just a statistical count.
        # See CLAUDE.md "Why We Use COUNT(DISTINCT) Instead of Histogram Estimates"

        cursor.execute(f"""
            SELECT COUNT(DISTINCT `{col}`) FROM `{source_db}`.`{table}`
        """)
        actual_distinct = cursor.fetchone()[0]

        # Calculate coverage ratio using ACTUAL distinct count
        coverage_ratio = actual_distinct / ref_table_size if ref_table_size > 0 else 1.0
        n_buckets = len(buckets)

        # Use equi-height sparse when FK uses a small subset of referenced table:
        # 1. Coverage < 20% (FK uses less than 20% of referenced table's values)
        # 2. At least 10 histogram buckets (enough granularity for weighted distribution)
        # 3. At least 100 actual distinct values (meaningful cardinality)
        if (coverage_ratio < 0.20 and n_buckets >= 10 and actual_distinct >= 100):
            return _build_equiheight_fk_expression(
                cursor, target_db, ref_table, ref_col, buckets, actual_distinct, ref_table_size
            )

    return None  # High cardinality - use dense approach


def _build_singleton_fk_expression(cursor, target_db, ref_table, ref_col, buckets, n_distinct):
    """Build FK expression for singleton histogram (low cardinality).

    Samples N evenly-spaced values from the target table and assigns
    weights from the source histogram.
    """
    # Get N evenly-spaced values from replay's referenced table
    cursor.execute(f"""
        SELECT `{ref_col}` FROM `{target_db}`.`{ref_table}`
        ORDER BY `{ref_col}`
    """)
    all_ref_values = [row[0] for row in cursor.fetchall()]

    if not all_ref_values or len(all_ref_values) < n_distinct:
        return None  # Not enough values in referenced table

    # Sample evenly spaced values to get good coverage
    if len(all_ref_values) == n_distinct:
        sampled_values = all_ref_values
    else:
        step = len(all_ref_values) / n_distinct
        sampled_values = [all_ref_values[int(i * step)] for i in range(n_distinct)]

    # Extract frequency weights from original histogram
    weights = []
    prev_cum = 0.0
    for bucket in buckets:
        cum_freq = bucket[1]
        weights.append(round(cum_freq - prev_cum, 6))
        prev_cum = cum_freq

    # Generate weighted CASE expression
    case_lines = [f"when {i+1} then {val}" for i, val in enumerate(sampled_values)]
    expression = f"""case rand.weighted(array[{','.join(map(str, weights))}])
    {' '.join(case_lines)}
    end"""

    description = f"sparse ({n_distinct} distinct values, weighted)"
    return (expression, description)


def _build_equiheight_fk_expression(cursor, target_db, ref_table, ref_col, buckets, distinct_count, ref_table_size):
    """Build FK expression for equi-height histogram with low cardinality.

    For FK columns that only use a small subset of the referenced table's values,
    we generate weighted ranges based on the histogram buckets.

    Args:
        distinct_count: Actual distinct count from source (via COUNT(DISTINCT)),
                       NOT the unreliable histogram estimate.

    PRIVACY: Uses bucket weights and synthetic range positions, not actual values.
    """
    # Get min/max from target table to calculate synthetic ranges
    cursor.execute(f"SELECT MIN(`{ref_col}`), MAX(`{ref_col}`) FROM `{target_db}`.`{ref_table}`")
    min_val, max_val = cursor.fetchone()

    if min_val is None or max_val is None:
        return None

    # Calculate the proportion of the referenced table that FK uses
    coverage_ratio = distinct_count / ref_table_size

    # Extract bucket weights and relative positions
    weights = []
    prev_cum = 0.0
    for bucket in buckets:
        cum_freq = bucket[2]  # equi-height: [min, max, cum_freq, num_distinct]
        weights.append(round(cum_freq - prev_cum, 6))
        prev_cum = cum_freq

    n_buckets = len(buckets)

    # Generate synthetic ranges within the target table's value space
    # Scale ranges proportionally to coverage_ratio
    total_range = max_val - min_val + 1
    scaled_range = int(total_range * coverage_ratio)
    if scaled_range < n_buckets:
        scaled_range = n_buckets  # At least one value per bucket

    # Distribute values across buckets
    values_per_bucket = max(1, scaled_range // n_buckets)

    case_lines = []
    for i in range(n_buckets):
        bucket_start = min_val + (i * values_per_bucket)
        bucket_end = min_val + ((i + 1) * values_per_bucket) - 1
        if bucket_end > max_val:
            bucket_end = max_val
        if bucket_start > max_val:
            bucket_start = max_val

        if bucket_start == bucket_end:
            case_lines.append(f"when {i+1} then {bucket_start}")
        else:
            span = bucket_end - bucket_start + 1
            case_lines.append(f"when {i+1} then rand.range(0,{span})+{bucket_start}")

    expression = f"""case rand.weighted(array[{','.join(map(str, weights))}])
    {' '.join(case_lines)}
    end"""

    description = f"equi-height sparse ({distinct_count} distinct, {coverage_ratio:.1%} coverage)"
    return (expression, description)


def _build_dense_fk_expression(cursor, target_db, ref_table, ref_col):
    """Build dense FK expression using min/max range from referenced table.

    PRIVACY: This function is privacy-safe because it reads min/max from
    the TARGET schema (synthetically generated PK values), not from source.

    Returns (expression, description) tuple.
    """
    cursor.execute(
        f"SELECT MIN(`{ref_col}`), MAX(`{ref_col}`) FROM `{target_db}`.`{ref_table}`"
    )
    min_val, max_val = cursor.fetchone()

    if min_val is None or max_val is None:
        return ("rand.range(0,1)", "empty table")

    expression = f"rand.range({min_val},{max_val + 1})"
    description = f"range [{min_val}, {max_val}]"
    return (expression, description)


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

        num_suffix = f"_{i}"
        base = f"{column_name}{num_suffix}"

        if len(base) < original_len:
            # Pad with underscores to match original length
            synthetic_value = base + "_" * (original_len - len(base))
        elif len(base) > original_len:
            # Truncate column name but preserve the unique number suffix
            available_for_name = original_len - len(num_suffix)
            if available_for_name >= 1:
                # Keep as much of the column name as possible, plus the number
                synthetic_value = column_name[:available_for_name] + num_suffix
            elif original_len >= len(str(i)):
                # No room for name, use zero-padded number
                synthetic_value = str(i).zfill(original_len)
            else:
                # Extreme case: truncate even the number
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


def _try_sparse_date_expression(cursor, database, table, column, col_type):
    """Try to build sparse date expression using singleton histogram.

    For date columns with singleton histograms (low cardinality), generates
    a weighted CASE expression with SYNTHETIC date values.

    PRIVACY: We use synthetic sequential dates (2000-01-01, 2000-01-02, ...)
    instead of actual source dates to avoid data leakage. Only the distribution
    weights are preserved from the source histogram.

    Returns expression string if sparse approach is applicable, None otherwise.
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

    if not buckets or hist_type != "singleton":
        return None

    # Extract weights from histogram (distribution shape - OK to use)
    # We DO NOT use actual date values from histogram (would be data leak)
    weights = []
    has_null = False
    prev_cum = 0.0

    for bucket in buckets:
        raw_value = bucket[0]
        cum_freq = bucket[1]
        weights.append(round(cum_freq - prev_cum, 6))
        prev_cum = cum_freq

        # Check if any bucket represents zero date (which becomes NULL)
        if isinstance(raw_value, str) and raw_value.startswith("0000-00-00"):
            has_null = True

    # Generate SYNTHETIC date values - sequential from base date
    # We use synthetic dates to preserve privacy while maintaining distribution shape
    n_distinct = len(buckets)
    date_values = []

    for i in range(n_distinct):
        if has_null and i == 0:
            # If source had zero dates, generate NULL for first bucket
            date_values.append("NULL")
        else:
            # Generate synthetic sequential dates
            if col_type == "date":
                # TIMESTAMP 'YYYY-MM-DD HH:MM:SS' + INTERVAL N DAY
                date_values.append(f"TIMESTAMP '{SYNTHETIC_BASE_DATE} 00:00:00' + INTERVAL {i} DAY")
            elif col_type in ("datetime", "timestamp"):
                # For datetime, space values by 1 hour each
                date_values.append(f"TIMESTAMP '{SYNTHETIC_BASE_DATETIME}' + INTERVAL {i} HOUR")
            else:
                return None  # TIME columns use dense approach

    # Generate weighted CASE expression
    case_lines = [f"when {i+1} then {val}" for i, val in enumerate(date_values)]

    return f"""case rand.weighted(array[{','.join(map(str, weights))}])
    {' '.join(case_lines)}
    end"""


def _build_dense_date_expression(cursor, database, table, column, col_type):
    """Build dense date expression using calculated span from histogram.

    PRIVACY: We use a synthetic base date and only extract the SPAN (range size)
    from the histogram, not the actual min/max dates. This avoids data leakage.

    Returns expression string or None if histogram doesn't exist.
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

    # Handle MySQL zero dates - skip range approach if present
    if min_val.startswith("0000-00-00") or max_val.startswith("0000-00-00"):
        return None

    # Calculate SPAN only - we don't use actual min/max values as base
    # Instead, we use synthetic base date + span for privacy
    if col_type == "date":
        min_date = datetime.strptime(min_val[:10], "%Y-%m-%d").date()
        max_date = datetime.strptime(max_val[:10], "%Y-%m-%d").date()
        day_span = (max_date - min_date).days
        # Use synthetic base date, not actual min_date
        return f"TIMESTAMP '{SYNTHETIC_BASE_DATE} 00:00:00' + INTERVAL rand.range(0, {day_span + 1}) DAY"

    elif col_type in ("datetime", "timestamp"):
        fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in min_val else "%Y-%m-%d %H:%M:%S"
        min_ts = datetime.strptime(min_val, fmt)
        fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in max_val else "%Y-%m-%d %H:%M:%S"
        max_ts = datetime.strptime(max_val, fmt)
        second_span = int((max_ts - min_ts).total_seconds())
        # Use synthetic base datetime, not actual min_ts
        return f"TIMESTAMP '{SYNTHETIC_BASE_DATETIME}' + INTERVAL rand.range(0, {second_span + 1}) SECOND"

    elif col_type == "time":
        def parse_time_to_secs(t):
            parts = t.split(":")
            h, m = int(parts[0]), int(parts[1])
            s = float(parts[2]) if len(parts) > 2 else 0
            return h * 3600 + m * 60 + int(s)

        min_secs = parse_time_to_secs(min_val)
        max_secs = parse_time_to_secs(max_val)
        # For TIME, the span is what matters, not actual times
        # Use span from 0 (midnight) for privacy
        time_span = max_secs - min_secs
        return f"INTERVAL rand.range(0, {time_span + 1}) SECOND"

    return None


def get_date_range_expression(cursor, database, table, column, col_type):
    """Get expression for DATE/DATETIME/TIMESTAMP columns.

    Tries sparse approach first (for singleton histograms with few distinct values),
    falls back to dense range approach for high-cardinality date columns.
    """
    # Try sparse approach first
    sparse_expr = _try_sparse_date_expression(cursor, database, table, column, col_type)
    if sparse_expr:
        return sparse_expr

    # Fall back to dense range approach
    return _build_dense_date_expression(cursor, database, table, column, col_type)


def histogram_to_case(hist, ddl_line):
    """Generate weighted CASE expression for numeric columns using SYNTHETIC values.

    PRIVACY: We use synthetic sequential values instead of actual numeric values
    from the histogram. Only the distribution weights and relative range sizes
    are preserved. This avoids data leakage.

    For singleton histograms: Generate values 1, 2, 3, ... (or scaled for decimals)
    For equi-height histograms: Generate synthetic ranges with same relative spans
    """
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

    # Extract weights (distribution shape - OK to use)
    weights = []
    prev = 0.0
    for b in buckets:
        cumulative = b[-2] if hist_type == "equi-height" else b[1]
        weights.append(round(cumulative - prev, 5))
        prev = cumulative

    # Generate SYNTHETIC values/ranges
    # We preserve: number of buckets, weights, relative range sizes
    # We DO NOT use: actual numeric values from source data
    case_lines = []

    if hist_type == "singleton":
        # For singleton: generate synthetic sequential values
        # Use simple sequential integers (or scaled for decimals)
        for i in range(1, len(buckets) + 1):
            synthetic_val = i / scale if scale > 1 else float(i)
            case_lines.append(f"when {i} then {synthetic_val}")
    else:
        # For equi-height: generate synthetic ranges with same relative spans
        # Calculate relative span sizes from original histogram
        original_spans = []
        for b in buckets:
            lo = float(b[0])
            hi = float(b[1])
            original_spans.append(hi - lo)

        # Normalize spans to generate synthetic CONTIGUOUS ranges starting from 0
        # We preserve the relative span sizes but not actual values
        # IMPORTANT: Ranges must be contiguous (no gaps) to avoid doubling distinct count
        synthetic_start = 0
        for i, span in enumerate(original_spans, start=1):
            # Use relative span (normalized to reasonable range)
            # For privacy, we scale spans to start from 0
            synthetic_lo = synthetic_start
            synthetic_hi = synthetic_start + max(1, int(span * scale))

            span_size = synthetic_hi - synthetic_lo
            if scale == 1:
                case_lines.append(f"when {i} then rand.range(0,{span_size + 1})+{synthetic_lo}")
            else:
                case_lines.append(
                    f"when {i} then rand.range(0,{span_size + 1})/{scale}+{synthetic_lo/scale}"
                )
            # Move to next range WITHOUT gap (contiguous ranges)
            synthetic_start = synthetic_hi

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

            # 🔴 FOREIGN KEY → use sparse or dense approach based on histogram type
            if col in foreign_keys:
                if col in generated_appendages:
                    synthetic = generated_appendages[col]
                else:
                    ref_table, ref_col = foreign_keys[col]
                    # Use unified FK expression builder
                    synthetic, _ = build_single_fk_expression(
                        cursor, database, target_database, table, col, ref_table, ref_col
                    )

            # 🔴 PRIMARY KEY or AUTO_INCREMENT
            elif (
                re.search(r"\bauto_increment\b", line, re.IGNORECASE)
                or col in primary_key_columns
            ):
                is_auto_increment = re.search(r"\bauto_increment\b", line, re.IGNORECASE)
                is_composite_pk = len(primary_key_columns) > 1
                is_fk = col in foreign_keys

                # For composite PK columns that are NOT FKs (e.g., ticket_number, order_number),
                # we need to generate repeating values to match source cardinality.
                # Example: ss_ticket_number has 75,807 distinct values across 799,666 rows,
                # meaning each ticket appears ~10 times (multiple items per transaction).
                if is_composite_pk and not is_fk and not is_auto_increment:
                    # Query actual distinct count from source
                    cursor.execute(f"""
                        SELECT COUNT(DISTINCT `{col}`), MIN(`{col}`)
                        FROM `{database}`.`{table}`
                    """)
                    distinct_count, min_val = cursor.fetchone()

                    if distinct_count and distinct_count > 0:
                        min_val = min_val if min_val is not None else 1
                        # Generate cycling values: 1,2,3,...,N,1,2,3,...,N,...
                        synthetic = f"mod(rownum-1, {distinct_count}) + {min_val}"
                    else:
                        synthetic = "rownum"
                else:
                    # Single-column PK or AUTO_INCREMENT → unique per row
                    # Check if source data is 0-based by looking at histogram
                    if col in histograms:
                        min_val, _ = get_min_max_from_histogram(histograms[col])
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
    database = "tpcds"
    target_database = "tpcds_harsha"  # Target schema for FK range queries

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
