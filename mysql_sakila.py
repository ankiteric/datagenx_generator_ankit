import mysql.connector
from mysql.connector import Error
import re
import json
from datetime import datetime
import os

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
        # Step 1: Get CREATE TABLE
        # ------------------------------------------------------------
        cursor.execute(f"SHOW CREATE TABLE `{database}`.`{table}`;")
        ddl = cursor.fetchone()[1]

        # ------------------------------------------------------------
        # Step 2: Get list of all columns
        # ------------------------------------------------------------
        cursor.execute("""
            SELECT COLUMN_NAME 
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION;
        """, (database, table))
        columns = [row[0] for row in cursor.fetchall()]
        
        # Ensure histograms exist
        analyze_and_update_histograms(cursor, database, table)
        
        # ------------------------------------------------------------
        # Step 3: Get histogram JSON for table
        # ------------------------------------------------------------
        cursor.execute("""
            SELECT COLUMN_NAME, HISTOGRAM
            FROM information_schema.column_statistics
            WHERE SCHEMA_NAME = %s AND TABLE_NAME = %s;
        """, (database, table))

        hist_raw = cursor.fetchall()
        histograms = {
            col: json.loads(hist)
            for col, hist in hist_raw
            if hist is not None
        }

        # ------------------------------------------------------------
        # Step 4: Convert histogram bucket → CASE expression
        # ------------------------------------------------------------
        def histogram_to_case(col_name, hist, ddl_line):
            buckets = hist["buckets"]
            hist_type = hist["histogram-type"]

            # Detect DATETIME / TIMESTAMP
            if re.search(r"\b(date|datetime|timestamp|time)\b", ddl_line, re.IGNORECASE):
                return "{{rand.u31_timestamp()}}"

            # Detect DECIMAL(p,s)
            decimal_match = re.search(r"decimal\(\s*\d+\s*,\s*(\d+)\s*\)", ddl_line, re.IGNORECASE)
            scale = 10 ** int(decimal_match.group(1)) if decimal_match else 1

            weights = []
            ranges = []

            # Compute bucket weights
            prev = 0.0
            for b in buckets:
                cumulative = b[-2] if hist_type == "equi-height" else b[1]
                weight = cumulative - prev
                prev = cumulative
                weights.append(round(weight, 5))

            # Build integer-scaled ranges
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

            # CASE expression
            case_lines = []
            for i, (low_i, high_i) in enumerate(ranges, start=1):
                if low_i == high_i:
                    val = low_i / scale
                    case_lines.append(f"when {i} then {val}")
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

        # ------------------------------------------------------------
        # Step 5: Rewrite DDL column definitions
        # ------------------------------------------------------------
        new_lines = []
        datetime_pattern = re.compile(r"\b(date|datetime|timestamp|time)\b", re.IGNORECASE)

        for line in ddl.splitlines():
            col_match = re.match(r"\s*`([^`]+)`", line)

            if not col_match:
                new_lines.append(line)
                continue

            col_name = col_match.group(1)
            synthetic_block = ""

            # Histogram-based synthetic data
            if col_name in histograms:
                synthetic_block = histogram_to_case(col_name, histograms[col_name], line)

            # Datetime / timestamp handling
            if datetime_pattern.search(line):
                synthetic_block += "{{rand.u31_timestamp()}}"

            if synthetic_block:
                if line.rstrip().endswith(','):
                    newline = line.rstrip()[:-1] + f" {synthetic_block},"
                else:
                    newline = line + f" {synthetic_block}"
                new_lines.append(newline)
            else:
                new_lines.append(line)

        final_ddl = "\n".join(new_lines)

        print("\n📜 Generated Histogram-Based Synthetic DDL:\n")
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
    # Step 1: ANALYZE TABLE (refresh stats)
    cursor.execute(f"ANALYZE TABLE `{database}`.`{table}`;")
    cursor.fetchall()  # consume result to avoid "Unread result found"

    # Step 2: Get histogram-eligible columns
    cursor.execute("""
        SELECT COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s
          AND TABLE_NAME = %s
          AND DATA_TYPE IN (
              'tinyint','smallint','mediumint','int','bigint',
              'decimal','numeric','float','double',
              'date','datetime','timestamp'
          );
    """, (database, table))

    cols = [row[0] for row in cursor.fetchall()]

    if not cols:
        return

    # Step 3: UPDATE HISTOGRAM
    col_list = ", ".join(f"`{c}`" for c in cols)

    cursor.execute(
        f"""
        ANALYZE TABLE `{database}`.`{table}`
        UPDATE HISTOGRAM ON {col_list}
        WITH 100 BUCKETS;
        """
    )
    cursor.fetchall()  # consume result


if __name__ == "__main__":
    host = "localhost"
    user = "root"
    password = "newpassword"
    database = "sakila"

    conn = mysql.connector.connect(
        host=host,
        user=user,
        password=password,
        database=database
    )
    cursor = conn.cursor()

    # Get all table names
    cursor.execute("""
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s
          AND TABLE_TYPE = 'BASE TABLE';
    """, (database,))
    tables = [row[0] for row in cursor.fetchall()]

    cursor.close()
    conn.close()

    # Create timestamped output directory
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = f"dbgen_output_{ts}"
    os.makedirs(output_dir, exist_ok=True)

    print(f"📂 Writing outputs to: {output_dir}\n")

    # Run for each table
    for table in tables:
        print(f"⚙️ Processing table: {table}")

        ddl = annotate_table_with_histogram(
            host=host,
            user=user,
            password=password,
            database=database,
            table=table
        )

        if ddl:
            out_path = os.path.join(output_dir, f"{table}.dbgen")
            with open(out_path, "w") as f:
                f.write(ddl)

            print(f"   ✅ Written: {out_path}")
        else:
            print(f"   ❌ Failed: {table}")
