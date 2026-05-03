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

    Three approaches based on coverage (actual_distinct / ref_table_size):
    1. Sparse weighted (<20%): Weighted CASE with sampled values - preserves distribution
    2. Moderate cycling (20-80%): mod() cycling - matches exact distinct count
    3. Dense random (>=80%): rand.range() - full coverage

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
    # Get actual distinct count and row count from source (privacy-safe: just counts)
    cursor.execute(f"""
        SELECT COUNT(DISTINCT `{col}`), COUNT(*) FROM `{source_db}`.`{table}` WHERE `{col}` IS NOT NULL
    """)
    actual_distinct, source_row_count = cursor.fetchone()
    actual_distinct = actual_distinct or 0
    source_row_count = source_row_count or 1

    # Get referenced table size and min value from target
    cursor.execute(f"SELECT COUNT(*), MIN(`{ref_col}`) FROM `{target_db}`.`{ref_table}`")
    ref_table_size, ref_min = cursor.fetchone()
    ref_table_size = ref_table_size or 1
    ref_min = ref_min if ref_min is not None else 1

    # Calculate coverage ratio (how much of referenced table is used)
    coverage_ratio = actual_distinct / ref_table_size if ref_table_size > 0 else 1.0

    # Calculate distinct ratio (how unique values are in source table)
    # High ratio means random selection will cause collisions (birthday paradox)
    distinct_ratio = actual_distinct / source_row_count if source_row_count > 0 else 1.0

    # Decision based on coverage AND distinct_ratio:
    # - If distinct_ratio > 0.5: use mod() cycling (random would cause collisions)
    # - Else if coverage < 20%: try sparse weighted approach (preserves distribution shape)
    # - Else if coverage < 80%: use mod() cycling (matches exact distinct count)
    # - Else (coverage >= 80%): use dense rand.range (nearly full coverage anyway)

    # High distinct ratio means random selection causes collisions - use mod() cycling
    if distinct_ratio > 0.5 and coverage_ratio < 0.80:
        expression = f"mod(rownum-1, {actual_distinct}) + {ref_min}"
        description = f"cycling mod({actual_distinct})+{ref_min} (high distinct ratio {distinct_ratio*100:.0f}%)"
        return (expression, description)

    # Try histogram-aware generation first. Singleton FK histograms are common
    # for dimension references and should preserve their value weights even
    # when coverage is high (for example TPC-H nation -> region).
    sparse_result = _try_sparse_fk_expression(
        cursor, source_db, target_db, table, col, ref_table, ref_col, source_row_count
    )
    if sparse_result:
        return sparse_result

    if coverage_ratio < 0.20:
        # Non-singleton sparse approaches only apply to low-coverage FKs.
        sparse_result = _try_sparse_fk_expression(
            cursor, source_db, target_db, table, col, ref_table, ref_col, source_row_count,
            singleton_only=False,
        )
        if sparse_result:
            return sparse_result
        # Fall through to moderate if sparse doesn't apply

    if coverage_ratio < 0.80:
        # Moderate coverage: use mod() cycling to match exact distinct count
        # This generates values 1 to actual_distinct (assuming ref_min=1)
        expression = f"mod(rownum-1, {actual_distinct}) + {ref_min}"
        description = f"cycling mod({actual_distinct})+{ref_min} ({coverage_ratio*100:.1f}% coverage)"
        return (expression, description)

    # High coverage (>=80%): use dense rand.range
    return _build_dense_fk_expression(cursor, target_db, ref_table, ref_col)


def _try_sparse_fk_expression(
    cursor, source_db, target_db, table, col, ref_table, ref_col,
    source_row_count=None, singleton_only=True,
):
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
            cursor, target_db, ref_table, ref_col, buckets, n_distinct, source_row_count
        )

    elif hist_type == "equi-height":
        if singleton_only:
            return None

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


def _build_singleton_fk_expression(cursor, target_db, ref_table, ref_col, buckets, n_distinct, source_row_count=None):
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

    if source_row_count:
        # Deterministic searched CASE avoids small-table random drift while
        # still using only distribution weights from source histograms and
        # synthetic target values.
        counts = [int(round(w * source_row_count)) for w in weights]
        diff = source_row_count - sum(counts)
        if counts:
            counts[-1] += diff

        case_lines = []
        cumulative = 0
        for count, val in zip(counts, sampled_values):
            if count <= 0:
                continue
            cumulative += count
            case_lines.append(f"when rownum <= {cumulative} then {val}")

        if case_lines:
            expression = f"""case
    {' '.join(case_lines)}
    else {sampled_values[-1]}
    end"""
            description = f"sparse ({n_distinct} distinct values, deterministic weighted)"
            return (expression, description)

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
    """Get synthetic string bucket values and frequencies from histogram metadata.

    Returns a list of (value, count) tuples sorted by count descending,
    or None if histogram doesn't exist or cardinality exceeds
    STRING_CARDINALITY_THRESHOLD.

    Note: MySQL stores string values in histograms as base64-encoded strings.
    We only use decoded values to infer representative lengths. Generated
    values are synthetic and do not reuse these source literals.
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

    if hist_type not in {"singleton", "equi-height"}:
        return None

    # Check cardinality threshold
    if len(buckets) > STRING_CARDINALITY_THRESHOLD:
        return None

    # Extract representative bucket values and convert cumulative frequencies
    # to individual frequencies.
    # Singleton buckets: [base64_value, cumulative_frequency]
    # Equi-height buckets: cumulative frequency is the penultimate element.
    # Scale frequencies to integer counts for compatibility with string_values_to_case
    values_with_counts = []
    prev_cum_freq = 0.0

    for i, bucket in enumerate(buckets, start=1):
        raw_value = bucket[0]
        cum_freq = bucket[-2] if hist_type == "equi-height" else bucket[1]
        freq = cum_freq - prev_cum_freq
        prev_cum_freq = cum_freq

        # Decode base64-encoded string value (MySQL encodes string histogram values)
        # First check if it looks like base64 (contains only valid base64 chars)
        value = decode_histogram_string(raw_value)
        if not value:
            value = f"{column}_{i}"

        # Scale to pseudo-count (maintains relative weights)
        count = int(freq * 1000000)
        values_with_counts.append((value, count))

    # Sort by count descending
    values_with_counts.sort(key=lambda x: x[1], reverse=True)

    return values_with_counts


def string_values_to_case(values_with_counts, column_name, max_length=None, row_count=None):
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

    If row_count is available, uses deterministic weighted bands instead of
    rand.weighted. This avoids random collisions that collapse singleton string
    buckets on small or near-unique columns.
    """
    if not values_with_counts:
        return ""

    total = sum(cnt for _, cnt in values_with_counts)
    if total == 0:
        return ""

    weights = [round(cnt / total, 6) for _, cnt in values_with_counts]

    synthetic_values = []
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

        synthetic_values.append(synthetic_value)

    if row_count and row_count > 0:
        counts = [int(round(weight * row_count)) for weight in weights]
        diff = row_count - sum(counts)
        if counts:
            counts[-1] += diff

        cumulative = 0
        deterministic_lines = []
        for synthetic_value, count in zip(synthetic_values, counts):
            if count <= 0:
                continue
            cumulative += count
            deterministic_lines.append(f"when rownum <= {cumulative} then '{synthetic_value}'")

        if deterministic_lines:
            return f"""case
    {' '.join(deterministic_lines)}
    else '{synthetic_values[-1]}'
    end"""

    case_lines = [
        f"when {i} then '{synthetic_value}'"
        for i, synthetic_value in enumerate(synthetic_values, start=1)
    ]

    return f"""case rand.weighted(array[{','.join(map(str, weights))}])
    {' '.join(case_lines)}
    end"""


def _try_sparse_date_expression(cursor, database, table, column, col_type):
    """Try to build sparse date expression using singleton histogram.

    For date columns with singleton histograms (low cardinality), generates
    either:
    - Weighted CASE expression (for normal tables with repeated values)
    - Deterministic mod() expression (for small tables to avoid birthday paradox)

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

    n_distinct = len(buckets)

    # Check for small table or high distinct ratio
    # These need deterministic generation to avoid birthday paradox
    # See VALIDATION_ISSUES.md Section 5: "Small Table Histogram Issues"
    cursor.execute(f"""
        SELECT COUNT(*), COUNT(DISTINCT `{column}`)
        FROM `{database}`.`{table}`
    """)
    row_count, actual_distinct = cursor.fetchone()

    use_deterministic = False
    if row_count and actual_distinct:
        distinct_ratio = actual_distinct / row_count if row_count > 0 else 0
        use_deterministic = (row_count < 100) or (distinct_ratio > 0.9)

    # For small tables, use deterministic mod() cycling
    if use_deterministic:
        if col_type == "date":
            return f"TIMESTAMP '{SYNTHETIC_BASE_DATE} 00:00:00' + INTERVAL mod(rownum-1, {n_distinct}) DAY"
        elif col_type in ("datetime", "timestamp"):
            return f"TIMESTAMP '{SYNTHETIC_BASE_DATETIME}' + INTERVAL mod(rownum-1, {n_distinct}) HOUR"
        else:
            return None  # TIME columns use dense approach

    # Extract weights from histogram (distribution shape - OK to use)
    # We DO NOT use actual date values from histogram (would be data leak)
    weights = []
    prev_cum = 0.0

    for bucket in buckets:
        cum_freq = bucket[1]
        weights.append(round(cum_freq - prev_cum, 6))
        prev_cum = cum_freq

    # Generate SYNTHETIC date values - sequential from base date
    # We use synthetic dates to preserve privacy while maintaining distribution shape
    # NOTE: MySQL's '0000-00-00' is a valid date (not NULL), so we don't need
    # special handling for it - we just generate synthetic dates for all buckets
    date_values = []

    for i in range(n_distinct):
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

    For 1:1 date columns (where row_count ≈ distinct_count), we use deterministic
    generation (rownum-1) instead of random to avoid birthday paradox collisions.
    See VALIDATION_ISSUES.md for explanation.

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

    # Check if this is a 1:1 date column (row_count ≈ distinct_count) OR a small table
    # If so, use deterministic generation to avoid birthday paradox
    # See VALIDATION_ISSUES.md Section 5: "Small Table Histogram Issues"
    cursor.execute(f"""
        SELECT COUNT(*), COUNT(DISTINCT `{column}`)
        FROM `{database}`.`{table}`
    """)
    row_count, distinct_count = cursor.fetchone()

    # Use deterministic generation if:
    # 1. Small table (<100 rows) - histogram approach unsuitable
    # 2. High distinct ratio (>90%) - birthday paradox would cause collisions
    use_deterministic = (distinct_count and row_count and
                         (row_count < 100 or distinct_count / row_count > 0.9))

    # Calculate SPAN only - we don't use actual min/max values as base
    # Instead, we use synthetic base date + span for privacy
    if col_type == "date":
        min_date = datetime.strptime(min_val[:10], "%Y-%m-%d").date()
        max_date = datetime.strptime(max_val[:10], "%Y-%m-%d").date()
        day_span = (max_date - min_date).days

        if use_deterministic:
            # 1:1 column: use rownum for guaranteed unique dates
            # mod() ensures we stay within the span even if row_count > span
            return f"TIMESTAMP '{SYNTHETIC_BASE_DATE} 00:00:00' + INTERVAL mod(rownum-1, {day_span + 1}) DAY"
        else:
            # Use synthetic base date, not actual min_date
            return f"TIMESTAMP '{SYNTHETIC_BASE_DATE} 00:00:00' + INTERVAL rand.range(0, {day_span + 1}) DAY"

    elif col_type in ("datetime", "timestamp"):
        fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in min_val else "%Y-%m-%d %H:%M:%S"
        min_ts = datetime.strptime(min_val, fmt)
        fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in max_val else "%Y-%m-%d %H:%M:%S"
        max_ts = datetime.strptime(max_val, fmt)
        second_span = int((max_ts - min_ts).total_seconds())

        if use_deterministic:
            # 1:1 column: use rownum for guaranteed unique timestamps
            return f"TIMESTAMP '{SYNTHETIC_BASE_DATETIME}' + INTERVAL mod(rownum-1, {second_span + 1}) SECOND"
        else:
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

        if use_deterministic:
            return f"INTERVAL mod(rownum-1, {time_span + 1}) SECOND"
        else:
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


def histogram_to_case(hist, ddl_line, actual_distinct_count=None, row_count=None):
    """Generate weighted CASE expression for numeric columns using SYNTHETIC values.

    PRIVACY: We use synthetic sequential values instead of actual numeric values
    from the histogram. Only the distribution weights and relative range sizes
    are preserved. This avoids data leakage.

    For singleton histograms: Generate values 1, 2, 3, ... (or scaled for decimals)
    For equi-height histograms: Generate synthetic ranges with same relative spans
    For small tables (<100 rows or >90% distinct): Use deterministic mod() cycling

    Args:
        hist: MySQL histogram JSON object
        ddl_line: DDL line for the column (to detect DECIMAL scale)
        actual_distinct_count: If provided, scale bucket num_distinct values to match
                               this total. This avoids histogram extrapolation errors
                               when sampling_rate < 1.0. See HISTOGRAM_SAMPLING_EXPLAINED.md.
        row_count: If provided, used to detect small tables that need deterministic
                   generation to avoid birthday paradox collisions.
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

    # Detect small tables or high distinct ratio (>90%)
    # These need deterministic generation to avoid birthday paradox collisions
    # See VALIDATION_ISSUES.md Section 5: "Small Table Histogram Issues"
    use_deterministic = False
    if row_count and actual_distinct_count:
        distinct_ratio = actual_distinct_count / row_count if row_count > 0 else 0
        # Small tables (<100 rows) OR high distinct ratio (>90%) need deterministic
        use_deterministic = (row_count < 100) or (distinct_ratio > 0.9)

    # For small tables or 1:1 columns, use simple deterministic mod() cycling
    # This guarantees all distinct values are used exactly once (or evenly distributed)
    if use_deterministic and actual_distinct_count:
        if scale == 1:
            return f"mod(rownum-1, {actual_distinct_count}) + 1"
        else:
            return f"(mod(rownum-1, {actual_distinct_count}) + 1) / {scale}"

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
        # If actual_distinct_count provided, use it instead of bucket count
        num_values = actual_distinct_count if actual_distinct_count else len(buckets)
        for i in range(1, num_values + 1):
            synthetic_val = i / scale if scale > 1 else float(i)
            case_lines.append(f"when {i} then {synthetic_val}")

        # Adjust weights array if needed
        if actual_distinct_count and actual_distinct_count != len(buckets):
            # Redistribute weights evenly for the actual count
            weights = [1.0 / num_values] * num_values

        if row_count and num_values <= 1000:
            # Deterministic weighted bands avoid random histogram drift for
            # low-cardinality numeric columns such as TPC-H part.p_size.
            counts = [int(round(w * row_count)) for w in weights]
            diff = row_count - sum(counts)
            if counts:
                counts[-1] += diff

            cumulative = 0
            deterministic_lines = []
            for i, count in enumerate(counts, start=1):
                if count <= 0:
                    continue
                cumulative += count
                synthetic_val = i / scale if scale > 1 else float(i)
                deterministic_lines.append(f"when rownum <= {cumulative} then {synthetic_val}")

            if deterministic_lines:
                final_val = num_values / scale if scale > 1 else float(num_values)
                return f"""case
    {' '.join(deterministic_lines)}
    else {final_val}
    end"""

        return f"""case rand.weighted(array[{','.join(map(str, weights))}])
    {' '.join(case_lines)}
    end"""
    else:
        # For equi-height: use DETERMINISTIC bucket assignment to guarantee all distinct values
        #
        # Problem with rand.weighted: randomly assigns rows to buckets, causing collisions
        # when we try to generate num_distinct values per bucket.
        #
        # Solution: Use mod(rownum, num_buckets) for bucket selection (equi-height = equal frequency)
        # Then use div(rownum, num_buckets) as "local row number" to cycle through all values.
        #
        # This guarantees:
        # 1. Equal distribution across buckets (matches equi-height histogram)
        # 2. All distinct values are used (no random collisions)

        num_buckets = len(buckets)

        # Get raw num_distinct from each bucket
        raw_distinct_counts = []
        for b in buckets:
            num_distinct = int(b[3]) if len(b) > 3 else 1
            raw_distinct_counts.append(max(1, num_distinct))

        # If actual_distinct_count is provided, scale bucket counts proportionally
        # This fixes histogram extrapolation errors when sampling_rate < 1.0
        histogram_total = sum(raw_distinct_counts)
        if actual_distinct_count and histogram_total > 0:
            scale_factor = actual_distinct_count / histogram_total
            distinct_counts = [max(1, int(round(c * scale_factor))) for c in raw_distinct_counts]
            # Adjust to match exact total (rounding may cause slight mismatch)
            diff = actual_distinct_count - sum(distinct_counts)
            if diff != 0:
                # Add/subtract from largest bucket
                max_idx = distinct_counts.index(max(distinct_counts))
                distinct_counts[max_idx] = max(1, distinct_counts[max_idx] + diff)
        else:
            distinct_counts = raw_distinct_counts

        # Generate case expressions with deterministic cycling
        # local_row = div(rownum-1, num_buckets) gives 0, 0, 0, ..., 1, 1, 1, ..., 2, 2, 2, ...
        # mod(local_row, num_distinct) cycles through all distinct values
        synthetic_start = 0
        for i, num_distinct in enumerate(distinct_counts, start=1):
            synthetic_lo = synthetic_start

            if num_distinct == 1:
                # Single value bucket - generate exactly 1 value
                if scale == 1:
                    case_lines.append(f"when {i} then {synthetic_lo}")
                else:
                    case_lines.append(f"when {i} then {synthetic_lo}/{scale}")
                synthetic_start = synthetic_lo + 1
            else:
                # Multi-value bucket - use div/mod for deterministic cycling through all values
                # div(rownum-1, num_buckets) = local row number (0, 1, 2, ... for each bucket)
                # mod(local_row, num_distinct) = cycles through 0 to num_distinct-1
                if scale == 1:
                    case_lines.append(f"when {i} then mod(div(rownum-1,{num_buckets}),{num_distinct})+{synthetic_lo}")
                else:
                    case_lines.append(
                        f"when {i} then (mod(div(rownum-1,{num_buckets}),{num_distinct})+{synthetic_lo})/{scale}"
                    )
                synthetic_start = synthetic_lo + num_distinct

        # Use deterministic bucket selection: mod(rownum-1, num_buckets)+1 gives buckets 1,2,3,...,N,1,2,3,...
        return f"""case mod(rownum-1,{num_buckets})+1
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

            # 🟢 CHECK generated_appendages FIRST for ALL columns
            # MasterRun.py may have generated expressions for FK, PK, or composite FK columns
            if col in generated_appendages:
                synthetic = generated_appendages[col]

            # 🔴 FOREIGN KEY → use sparse or dense approach based on histogram type
            elif col in foreign_keys:
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
                #
                # IMPORTANT: We use div() not mod() to group consecutive rows together.
                # This avoids duplicate PK combinations when other PK columns are random.
                # - mod() cycles: 1,2,3,...,N,1,2,3,... → duplicates likely with random other col
                # - div() groups: 1,1,1,...,2,2,2,...   → consecutive rows share value, safe
                if is_composite_pk and not is_fk and not is_auto_increment:
                    # Query actual distinct count and row count from source
                    cursor.execute(f"""
                        SELECT COUNT(DISTINCT `{col}`), MIN(`{col}`), COUNT(*)
                        FROM `{database}`.`{table}`
                    """)
                    distinct_count, min_val, row_count = cursor.fetchone()

                    if distinct_count and distinct_count > 0:
                        min_val = min_val if min_val is not None else 1
                        # Calculate rows per distinct value (how many rows share each value)
                        rows_per_value = row_count // distinct_count

                        if rows_per_value >= 2:
                            # Many rows per value (e.g., sales tables with ~10 items per ticket)
                            # Use div() grouping with mod() capping:
                            # - div() groups consecutive rows: 1,1,1,...,2,2,2,...,3,3,3,...
                            # - mod() caps at distinct_count to avoid over-generation
                            #   (integer division truncation would otherwise create extra values)
                            # See VALIDATION_ISSUES.md Section 3 for explanation.
                            synthetic = f"mod(div(rownum-1, {rows_per_value}), {distinct_count}) + {min_val}"
                        else:
                            # Few rows per value (e.g., returns tables with ~1 item per ticket)
                            # Use mod() cycling to match exact distinct count
                            # This works because the FK column (handled by MasterRun.py) also
                            # uses mod() cycling, and LCM of the two cycle lengths >> row_count
                            synthetic = f"mod(rownum-1, {distinct_count}) + {min_val}"
                    else:
                        synthetic = "rownum"
                else:
                    # Single-column PK or AUTO_INCREMENT → unique per row
                    # Preserve the source key base. TPC-H dimension keys such
                    # as region/nation are 0-based, while many other schemas
                    # are 1-based. Querying MIN is privacy-safe statistical
                    # metadata and is more reliable than assuming histograms
                    # exist for every PK.
                    cursor.execute(f"SELECT MIN(`{col}`) FROM `{database}`.`{table}`")
                    source_min = cursor.fetchone()[0]
                    try:
                        source_min_int = int(source_min) if source_min is not None else 1
                        synthetic = "rownum" if source_min_int == 1 else f"rownum-1+{source_min_int}"
                    except (ValueError, TypeError):
                        synthetic = "rownum"

            elif col_type in CHAR_TYPES:
                # Try to get distinct values from histogram metadata
                values = get_string_column_values(cursor, database, table, col)
                col_max_length = get_string_column_length(line)
                if values:
                    cursor.execute(f"SELECT COUNT(*) FROM `{database}`.`{table}`")
                    table_row_count = cursor.fetchone()[0]
                    synthetic = string_values_to_case(
                        values,
                        col,
                        max_length=col_max_length,
                        row_count=table_row_count,
                    )
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
                # Use histogram-based generation for numeric columns
                # Note: We only treat columns as FKs if declared in DDL - no guessing based on names
                if col in histograms:
                    # Query actual distinct count AND row count
                    # - actual_distinct: avoids histogram extrapolation errors (HISTOGRAM_SAMPLING_EXPLAINED.md)
                    # - row_count: detects small tables needing deterministic generation (VALIDATION_ISSUES.md #5)
                    cursor.execute(f"SELECT COUNT(DISTINCT `{col}`), COUNT(*) FROM `{database}`.`{table}`")
                    actual_distinct, table_row_count = cursor.fetchone()
                    synthetic = histogram_to_case(histograms[col], line, actual_distinct, table_row_count)
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
    from config import HOST, USER, PASSWORD, SOURCE_SCHEMA, TARGET_SCHEMA

    conn = mysql.connector.connect(
        host=HOST, user=USER, password=PASSWORD, database=SOURCE_SCHEMA
    )
    cursor = conn.cursor()

    # Get all tables
    cursor.execute("""
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
    """, (SOURCE_SCHEMA,))
    all_tables = [t[0] for t in cursor.fetchall()]

    # Build dependency map: table -> [tables it depends on]
    cursor.execute("""
        SELECT TABLE_NAME, REFERENCED_TABLE_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND REFERENCED_TABLE_NAME IS NOT NULL
    """, (SOURCE_SCHEMA,))

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

    out_dir = "generated/dbgen_files"

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
            HOST, USER, PASSWORD, SOURCE_SCHEMA, table, TARGET_SCHEMA
        )
        if ddl:
            with open(os.path.join(out_dir, f"{table}.dbgen"), "w") as f:
                f.write(ddl)
            print("   ✅ Done")
