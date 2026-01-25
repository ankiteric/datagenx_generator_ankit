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
    return f"/*{{{{ rand.regex('[a-zA-Z ]{{{length}}}') }}}}*/"


def text_appendage():
    return "/*{{ rand.regex('[a-zA-Z ]{100}') }}*/"


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

    return f"""{{{{
    case rand.weighted(array[{','.join(map(str, weights))}])
    {' '.join(case_lines)}
    end
}}}}"""


def annotate_table_with_histogram(host, user, password, database, table):
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

            # 🔴 PRIMARY KEY or AUTO_INCREMENT → {{rownum}}
            if (
                re.search(r"\bauto_increment\b", line, re.IGNORECASE)
                or col in primary_key_columns
            ):
                synthetic = "{{rownum}}"

            elif col_type in CHAR_TYPES:
                synthetic = char_varchar_appendage(line)

            elif col_type in TEXT_TYPES:
                synthetic = text_appendage()

            elif col_type in DATETIME_TYPES:
                synthetic = "{{rand.u31_timestamp()}}"

            elif col_type in YEAR:
                synthetic = "{{rand.range(1975,2025)}}"

            elif col_type in NUMERIC_TYPES:
                if col in histograms:
                    synthetic = histogram_to_case(histograms[col], line)
                else:
                    synthetic = "{{rand.range(0,5)}}"

            if synthetic:
                if line.rstrip().endswith(","):
                    line = line.rstrip()[:-1] + f" {synthetic},"
                else:
                    line = line + f" {synthetic}"

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


if __name__ == "__main__":
    host = "localhost"
    user = "root"
    password = "newpassword"
    database = "tpch"

    conn = mysql.connector.connect(
        host=host, user=user, password=password, database=database
    )
    cursor = conn.cursor()

    cursor.execute("""
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
    """, (database,))
    tables = [t[0] for t in cursor.fetchall()]
    cursor.close()
    conn.close()

    out_dir = f"dbgen_output_{datetime.now():%Y%m%d_%H%M%S}"
    os.makedirs(out_dir, exist_ok=True)

    for table in tables:
        print(f"⚙️ Processing table: {table}")
        ddl = annotate_table_with_histogram(
            host, user, password, database, table
        )
        if ddl:
            with open(os.path.join(out_dir, f"{table}.dbgen"), "w") as f:
                f.write(ddl)
            print("   ✅ Done")
