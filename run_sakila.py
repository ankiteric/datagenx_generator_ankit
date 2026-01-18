#!/usr/bin/env python3
import os
import subprocess
import mysql.connector
from mysql.connector import Error
import re

# ---------------- CONFIG ----------------
input_dir = "/Users/sreeharshar/work/db/datagenx/datagenx_generator/datagenx_generator/dbgen_output_20260118_150728"
dbgen_binary = "/Users/sreeharshar/work/db/datagenx/code/dbgen/target/release/dbgen"
temp_out_dir = "dbgen_tmp_out"

files_count = "1"
rows_count = "1"

mysql_host = "localhost"
mysql_user = "root"
mysql_password = "newpassword"
mysql_schema = "sakila"
# ---------------------------------------

NUMERIC_TYPES = {
    "tinyint", "smallint", "mediumint", "int", "bigint",
    "decimal", "numeric", "float", "double"
}

DATETIME_TYPES = {"date", "datetime", "timestamp", "time"}

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def normalize_type(t):
    return re.sub(r"\(.*\)", "", t.lower()).strip()


def fetch_mysql_columns(cursor, table):
    cursor.execute("""
        SELECT COLUMN_NAME, DATA_TYPE
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ORDINAL_POSITION
    """, (mysql_schema, table))
    return [(c, normalize_type(t)) for c, t in cursor.fetchall()]


def extract_first_insert_row(sql_path):
    """
    Extract first VALUES(...) row from INSERT statement
    """
    with open(sql_path) as f:
        for line in f:
            if line.upper().startswith("INSERT INTO"):
                continue
            m = re.search(r"\((.*)\)", line)
            if m:
                return split_sql_values(m.group(1))
    return []


def split_sql_values(row):
    """
    Split SQL VALUES row safely (handles quoted strings)
    """
    values = []
    current = ""
    in_string = False

    i = 0
    while i < len(row):
        c = row[i]

        if c == "'" and (i == 0 or row[i - 1] != "\\"):
            in_string = not in_string
            current += c
        elif c == "," and not in_string:
            values.append(current.strip())
            current = ""
        else:
            current += c
        i += 1

    values.append(current.strip())
    return values


def value_matches_type(val, col_type):
    if val.upper() == "NULL":
        return True

    if col_type in NUMERIC_TYPES:
        return re.fullmatch(r"-?\d+(\.\d+)?", val) is not None

    if col_type in DATETIME_TYPES:
        return val.startswith("'") and val.endswith("'")

    return True  # strings, blobs, text, etc.


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
os.makedirs(temp_out_dir, exist_ok=True)

try:
    conn = mysql.connector.connect(
        host=mysql_host,
        user=mysql_user,
        password=mysql_password,
        database=mysql_schema
    )
    cursor = conn.cursor()
except Error as e:
    print("❌ MySQL connection failed:", e)
    exit(1)

validation_failed = False

for filename in os.listdir(input_dir):
    if not filename.endswith(".dbgen"):
        continue

    table = filename.replace(".dbgen", "")
    template_path = os.path.join(input_dir, filename)
    sql_path = os.path.join(temp_out_dir, f"{table}.1.sql")

    print(f"\n⚙️ Processing table: {table}")

    # ---------------- Row count ----------------
    cursor.execute(f"SELECT COUNT(*) FROM `{mysql_schema}`.`{table}`")
    row_count = cursor.fetchone()[0]
    print(f"   📊 Row count = {row_count}")

    # ---------------- Run dbgen ----------------
    cmd = [
        dbgen_binary,
        "--out-dir", temp_out_dir,
        "--files-count", files_count,
        "--rows-per-file", str(row_count),
        "--rows-count", rows_count,
        "--template", template_path
    ]

    print(cmd)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("   ❌ dbgen failed")
        print(result.stderr)
        validation_failed = True
        continue

    # ---------------- INSERT-based validation ----------------
    mysql_cols = fetch_mysql_columns(cursor, table)
    values = extract_first_insert_row(sql_path)

    if not values:
        print("   ❌ No INSERT values found")
        validation_failed = True
        continue

    if len(mysql_cols) != len(values):
        print("   ❌ COLUMN COUNT MISMATCH")
        print(f"      MySQL  : {len(mysql_cols)}")
        print(f"      INSERT : {len(values)}")
        validation_failed = True
        continue

    for (col, col_type), val in zip(mysql_cols, values):
        if not value_matches_type(val, col_type):
            print(
                f"   ❌ TYPE MISMATCH "
                f"column `{col}` type={col_type} value={val}"
            )
            validation_failed = True

    if not validation_failed:
        print("   ✅ INSERT validation passed")

cursor.close()
conn.close()

# ---------------- Final status ----------------
if validation_failed:
    print("\n❌ Validation completed with errors")
    exit(1)
else:
    print("\n🎉 All tables validated successfully")
