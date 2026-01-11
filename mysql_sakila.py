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


def char_varchar_appendage(ddl_line):
    m = re.search(r"\b(char|varchar)\s*\(\s*(\d+)\s*\)", ddl_line, re.IGNORECASE)
    if not m:
        return ""
    length = m.group(2)
    return f"/*{{{{ rand.regex('[a-zA-Z ]{{{length}}}') }}}}*/"


def histogram_to_case(hist, ddl_line):
    """
    Convert numeric histogram to weighted CASE expression.
    Safe against empty or non-numeric histograms.
    """
    buckets = hist.get("buckets", [])
    if not buckets:
        return ""

    # Ensure numeric histogram
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

    weights = []
    ranges = []

    prev = 0.0
    for b in buckets:
        cumulative = b[-2] if hist_type == "equi-height" else b[1]
        weight = cumulative - prev
        prev = cumulative
        weights.append(round(weight, 5))

    for b in buckets:
        if hist_type == "singleton":
            v = float(b[0])
            iv = int(round(v * scale))
            ranges.append((iv, iv))
        else:
            low = float(b[0])
            high = float(b[1])
            ranges.append(
                (int(round(low * scale)), int(round(high * scale)))
            )

    case_lines = []
    for i, (low_i, high_i) in enumerate(ranges, start=1):
        if low_i == high_i:
            case_lines.append(f"when {i} then {low_i / scale}")
        else:
            span = high_i - low_i
            if scale == 1:
                case_lines.append(
                    f"when {i} then rand.range(0,{span})+{low_i}"
                )
            else:
                case_lines.append(
                    f"when {i} then rand.range(0,{span})/{scale}+{low_i/scale}"
                )

    weights_str = ",".join(str(w) for w in weights)
    case_body = "\n".join(case_lines)

    return f"""{{{{
    case rand.weighted(array[{weights_str}])
    {case_body}
    end
}}}}"""


def annotate_table_with_histogram(host, user, password, database, table):
    try:
        conn = mysql.connector.connect(
            host=host,
            user=user,
            password=password,
            database=database
        )
        cursor = conn.cursor()

        # ------------------------------------------------------------
        # Get CREATE TABLE
        # ------------------------------------------------------------
        cursor.execute(f"SHOW CREATE TABLE `{database}`.`{table}`;")
        ddl = cursor.fetchone()[1]

        # ------------------------------------------------------------
        # Column types
        # ------------------------------------------------------------
        cursor.execute("""
            SELECT COLUMN_NAME, DATA_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION;
        """, (database, table))

        column_types = {c: t.lower() for c, t in cursor.fetchall()}

        # ------------------------------------------------------------
        # Update numeric histograms only
        # ------------------------------------------------------------
        analyze_and_update_histograms(cursor, database, table)

        # ------------------------------------------------------------
        # Load histograms
        # ------------------------------------------------------------
        cursor.execute("""
            SELECT COLUMN_NAME, HISTOGRAM
            FROM information_schema.column_statistics
            WHERE SCHEMA_NAME = %s AND TABLE_NAME = %s;
        """, (database, table))

        histograms = {
            col: json.loads(hist)
            for col, hist in cursor.fetchall()
            if hist
        }

        # ------------------------------------------------------------
        # Rewrite DDL
        # ------------------------------------------------------------
        new_lines = []

        for line in ddl.splitlines():
            m = re.match(r"\s*`([^`]+)`", line)
            if not m:
                new_lines.append(line)
                continue

            col = m.group(1)
            col_type = column_types.get(col)
            synthetic = ""

            # CHAR / VARCHAR
            if col_type in {"char", "varchar"}:
                synthetic = char_varchar_appendage(line)

            # DATETIME / TIMESTAMP
            elif col_type in DATETIME_TYPES:
                synthetic = "{{rand.u31_timestamp()}}"

            # NUMERIC HISTOGRAM
            elif col_type in NUMERIC_TYPES and col in histograms:
                synthetic = histogram_to_case(histograms[col], line)

            if synthetic:
                if line.rstrip().endswith(","):
                    line = line.rstrip()[:-1] + f" {synthetic},"
                else:
                    line = line + f" {synthetic}"

            new_lines.append(line)

        final_ddl = "\n".join(new_lines)

        print("\n📜 Generated Synthetic DDL:\n")
        print(final_ddl)
        return final_ddl

    except Error as e:
        print("❌ Error:", e)
        return None
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def analyze_and_update_histograms(cursor, database, table):
    cursor.execute(f"ANALYZE TABLE `{database}`.`{table}`;")
    cursor.fetchall()

    cursor.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
          AND TABLE_NAME = %s
          AND DATA_TYPE IN (
              'tinyint','smallint','mediumint','int','bigint',
              'decimal','numeric','float','double'
          );
    """, (database, table))

    cols = [c[0] for c in cursor.fetchall()]
    if not cols:
        return

    col_list = ", ".join(f"`{c}`" for c in cols)

    cursor.execute(f"""
        ANALYZE TABLE `{database}`.`{table}`
        UPDATE HISTOGRAM ON {col_list}
        WITH 100 BUCKETS;
    """)
    cursor.fetchall()


if __name__ == "__main__":
    host = "localhost"
    user = "root"
    password = "newpassword"
    database = "sakila"

    conn = mysql.connector.connect(
        host=host, user=user, password=password, database=database
    )
    cursor = conn.cursor()

    cursor.execute("""
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE';
    """, (database,))

    tables = [t[0] for t in cursor.fetchall()]
    cursor.close()
    conn.close()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = f"dbgen_output_{ts}"
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
