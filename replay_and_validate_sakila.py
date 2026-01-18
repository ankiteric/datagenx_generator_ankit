import mysql.connector
from mysql.connector import Error
import argparse
import time
import re
import sys
import json
from pathlib import Path


# ------------------------------------------------------------
# Utilities
# ------------------------------------------------------------

def normalize_ddl(ddl: str) -> str:
    ddl = re.sub(r"`sakila_[^`]+`\.", "", ddl)
    ddl = re.sub(r"`sakila`\.", "", ddl)
    ddl = re.sub(r"\s+", " ", ddl)
    return ddl.strip().lower()


def extract_table_name(ddl: str) -> str:
    m = re.search(r"create\s+table\s+`([^`]+)`", ddl, re.IGNORECASE)
    if not m:
        raise ValueError("Could not extract table name from DDL")
    return m.group(1)


def execute_statements(cursor, sql_text):
    statements = [s.strip() for s in sql_text.split(";") if s.strip()]
    for stmt in statements:
        cursor.execute(stmt)


def pct_diff(a, b):
    if a == 0 and b == 0:
        return 0.0
    if a is None or b is None:
        return 1.0
    return abs(a - b) / max(a, b)


# ------------------------------------------------------------
# Histogram + Stats helpers
# ------------------------------------------------------------

def load_histograms(cursor, schema, table):
    cursor.execute("""
        SELECT COLUMN_NAME, HISTOGRAM
        FROM information_schema.column_statistics
        WHERE SCHEMA_NAME = %s AND TABLE_NAME = %s
    """, (schema, table))

    return {
        col: json.loads(hist)
        for col, hist in cursor.fetchall()
        if hist is not None
    }

# Keep histograms updated. 
def clone_histograms(cursor, src_schema, tgt_schema, table):
    cursor.execute("""
        SELECT COLUMN_NAME
        FROM information_schema.column_statistics
        WHERE SCHEMA_NAME = %s AND TABLE_NAME = %s
    """, (src_schema, table))

    cols = [row[0] for row in cursor.fetchall()]
    if not cols:
        return

    col_list = ", ".join(f"`{c}`" for c in cols)

    cursor.execute(
        f"""
        ANALYZE TABLE `{tgt_schema}`.`{table}`
        UPDATE HISTOGRAM ON {col_list}
        WITH 100 BUCKETS
        """
    )
    cursor.fetchall()  # IMPORTANT

def histogram_difference(h1, h2):
    """
    Compute Total Variation Distance between two MySQL histograms.
    Returns a float in [0.0, 1.0].
    """

    if not h1 or not h2:
        return 1.0  # maximal difference if one is missing

    b1 = h1.get("buckets", [])
    b2 = h2.get("buckets", [])

    if not b1 or not b2:
        return 1.0

    def probs(hist):
        buckets = hist["buckets"]
        hist_type = hist["histogram-type"]

        p = []
        prev = 0.0
        for b in buckets:
            cumulative = b[-2] if hist_type == "equi-height" else b[1]
            p.append(max(0.0, cumulative - prev))
            prev = cumulative
        return p

    p1 = probs(h1)
    p2 = probs(h2)

    n = min(len(p1), len(p2))
    if n == 0:
        return 1.0

    return 0.5 * sum(abs(p1[i] - p2[i]) for i in range(n))



def compare_histograms(h1, h2):
    mismatches = []

    all_cols = set(h1.keys()) | set(h2.keys())
    for col in sorted(all_cols):
        if col not in h1:
            mismatches.append((col, "missing in source"))
        elif col not in h2:
            mismatches.append((col, "missing in target"))
        elif h1[col] != h2[col]:
            diff = histogram_difference(h1[col], h2[col])
            mismatches.append((col, f"histogram diff = {diff:.5f}" ))
            #print(f"Histogram difference for {col} = {diff:.4f}")
    
    return mismatches


def load_table_stats(cursor, schema, table):
    cursor.execute("""
        SELECT TABLE_ROWS, AVG_ROW_LENGTH, DATA_LENGTH, INDEX_LENGTH
        FROM information_schema.tables
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
    """, (schema, table))
    return cursor.fetchone()


def load_index_stats(cursor, schema, table):
    cursor.execute("""
        SELECT INDEX_NAME, COLUMN_NAME, CARDINALITY
        FROM information_schema.statistics
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY INDEX_NAME, SEQ_IN_INDEX
    """, (schema, table))
    return cursor.fetchall()


# ------------------------------------------------------------
# Reporting
# ------------------------------------------------------------

def report_ddl_mismatch(orig, new):
    print("\n❌ DDL MISMATCH")
    print("----- ORIGINAL -----")
    print(orig)
    print("----- REPLAYED -----")
    print(new)


def report_rowcount_mismatch(orig, new):
    print("\n❌ ROW COUNT MISMATCH")
    print(f"Original : {orig}")
    print(f"Replayed : {new}")


def report_histogram_mismatch(mismatches):
    print("\n❌ HISTOGRAM MISMATCHES")
    for col, reason in mismatches:
        print(f" - Column `{col}`: {reason}")


def report_table_stats(orig, new):
    labels = ["TABLE_ROWS", "AVG_ROW_LENGTH", "DATA_LENGTH", "INDEX_LENGTH"]
    print("\n📊 TABLE STATISTICS COMPARISON")
    for i, label in enumerate(labels):
        diff = pct_diff(orig[i], new[i])
        status = "OK" if diff < 0.10 else "DIVERGED"
        print(f"{label:16} diff={diff:.2%} → {status}")


def report_index_stats(orig, new):
    print("\n📊 INDEX CARDINALITY COMPARISON")

    def to_map(rows):
        return {(r[0], r[1]): r[2] for r in rows}

    o = to_map(orig)
    n = to_map(new)

    all_keys = set(o.keys()) | set(n.keys())
    for key in sorted(all_keys):
        oc = o.get(key)
        nc = n.get(key)
        diff = pct_diff(oc, nc)
        status = "OK" if diff < 0.20 else "DIVERGED"
        print(f"{key}: diff={diff:.2%} → {status}")


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--ddl-file", required=True)
    parser.add_argument("--insert-file", required=True)
    args = parser.parse_args()

    ddl_sql = Path(args.ddl_file).read_text()
    insert_sql = Path(args.insert_file).read_text()

    table = extract_table_name(ddl_sql)
    ts = int(time.time())
    new_schema = f"sakila_{ts}"

    ddl_ok = True
    rows_ok = True
    hist_ok = True

    conn = cursor = None

    try:
        conn = mysql.connector.connect(
            host=args.host,
            user=args.user,
            password=args.password,
            autocommit=True
        )
        cursor = conn.cursor()
        
        # Disable foreign key checks. Might enable it later. 
        cursor.execute("SET FOREIGN_KEY_CHECKS = 0")

        # Set timezone to UTC to avoid problems with DST
        cursor.execute("SET time_zone = '+00:00' ")

        # Source row count
        cursor.execute(f"SELECT COUNT(*) FROM sakila.`{table}`")
        src_rows = cursor.fetchone()[0]

        # Create schema + table
        cursor.execute(f"CREATE SCHEMA `{new_schema}`")
        ddl_new = ddl_sql.replace(f"`{table}`", f"`{new_schema}`.`{table}`", 1)
        cursor.execute(ddl_new)

        # Inserts
        insert_new = re.sub(
            rf"(insert\s+into\s+)`?{table}`?",
            rf"\1`{new_schema}`.`{table}`",
            insert_sql,
            flags=re.IGNORECASE
        )
        execute_statements(cursor, insert_new)

        # Target row count
        cursor.execute(f"SELECT COUNT(*) FROM `{new_schema}`.`{table}`")
        tgt_rows = cursor.fetchone()[0]

        if src_rows != tgt_rows:
            rows_ok = False
            report_rowcount_mismatch(src_rows, tgt_rows)

        # Refresh stats
        cursor.execute(f"ANALYZE TABLE sakila.`{table}`")
        cursor.fetchall()   # consume result
        
        cursor.execute(f"ANALYZE TABLE `{new_schema}`.`{table}`")
        cursor.fetchall()   # consume result

        clone_histograms(cursor, "sakila", new_schema, table)


        # DDL compare
        cursor.execute(f"SHOW CREATE TABLE sakila.`{table}`")
        src_ddl = cursor.fetchone()[1]
        cursor.execute(f"SHOW CREATE TABLE `{new_schema}`.`{table}`")
        tgt_ddl = cursor.fetchone()[1]

        if normalize_ddl(src_ddl) != normalize_ddl(tgt_ddl):
            ddl_ok = False
            report_ddl_mismatch(src_ddl, tgt_ddl)

        # Histogram compare
        src_hist = load_histograms(cursor, "sakila", table)
        tgt_hist = load_histograms(cursor, new_schema, table)
        hist_diff = compare_histograms(src_hist, tgt_hist)

        if hist_diff:
            hist_ok = False
            report_histogram_mismatch(hist_diff)

        # Table stats
        report_table_stats(
            load_table_stats(cursor, "sakila", table),
            load_table_stats(cursor, new_schema, table)
        )

        # Index stats
        report_index_stats(
            load_index_stats(cursor, "sakila", table),
            load_index_stats(cursor, new_schema, table)
        )

        print("\n================ FINAL SUMMARY ================")
        print(f"DDL match        : {'✅' if ddl_ok else '❌'}")
        print(f"Row count match  : {'✅' if rows_ok else '❌'}")
        print(f"Histograms match : {'✅' if hist_ok else '❌'}")
        print("==============================================")

        if not (ddl_ok and rows_ok and hist_ok):
            sys.exit(2)

    except Error as e:
        print("❌ MySQL Error:", e)
        sys.exit(1)

    finally:
        if cursor:
            print(f"\n🧹 Dropping schema `{new_schema}`")
            cursor.execute(f"DROP SCHEMA IF EXISTS `{new_schema}`")
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
        if conn and conn.is_connected():
            cursor.close()
            conn.close()


if __name__ == "__main__":
    main()
