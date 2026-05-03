#!/usr/bin/env python3
"""Generate an HTML validation report for DataGenX source/target schemas."""

import argparse
import json
from pathlib import Path

import mysql.connector
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from config import HOST, PASSWORD, SOURCE_SCHEMA, TARGET_SCHEMA, USER


DEFAULT_COLUMNS = [
    ("customer", "c_nationkey"),
    ("nation", "n_regionkey"),
    ("supplier", "s_nationkey"),
    ("lineitem", "l_linenumber"),
    ("part", "p_size"),
    ("orders", "o_custkey"),
    ("lineitem", "l_orderkey"),
    ("lineitem", "l_partkey"),
    ("lineitem", "l_suppkey"),
]


def connect(args):
    return mysql.connector.connect(
        host=args.host,
        user=args.user,
        password=args.password,
        autocommit=True,
    )


def fetch_df(cursor, query, params=None):
    cursor.execute(query, params or ())
    rows = cursor.fetchall()
    cols = cursor.column_names
    return pd.DataFrame(rows, columns=cols)


def pct_diff(source, target):
    if source == 0 and target == 0:
        return 0.0
    if source is None or target is None:
        return 1.0
    return abs(source - target) / max(source, target)


def get_tables(cursor, schema):
    df = fetch_df(
        cursor,
        """
        SELECT TABLE_NAME
        FROM information_schema.tables
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
        """,
        (schema,),
    )
    return df["TABLE_NAME"].tolist()


def get_columns(cursor, schema, table):
    df = fetch_df(
        cursor,
        """
        SELECT COLUMN_NAME
        FROM information_schema.columns
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (schema, table),
    )
    return df["COLUMN_NAME"].tolist()


def get_column_types(cursor, schema, table):
    df = fetch_df(
        cursor,
        """
        SELECT COLUMN_NAME, COLUMN_TYPE
        FROM information_schema.columns
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """,
        (schema, table),
    )
    return dict(zip(df["COLUMN_NAME"], df["COLUMN_TYPE"]))


def get_indexed_columns(cursor, schema, table):
    df = fetch_df(
        cursor,
        """
        SELECT DISTINCT COLUMN_NAME
        FROM information_schema.statistics
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """,
        (schema, table),
    )
    return set(df["COLUMN_NAME"].tolist())


def is_string_type(col_type):
    col_type = (col_type or "").lower()
    return any(kind in col_type for kind in ("char", "varchar", "text", "blob"))


def is_decimal_type(col_type):
    col_type = (col_type or "").lower()
    return any(kind in col_type for kind in ("decimal", "numeric"))


def get_row_counts(cursor, source_schema, target_schema, tables):
    rows = []
    for table in tables:
        cursor.execute(f"SELECT COUNT(*) FROM `{source_schema}`.`{table}`")
        source_count = cursor.fetchone()[0]
        cursor.execute(f"SELECT COUNT(*) FROM `{target_schema}`.`{table}`")
        target_count = cursor.fetchone()[0]
        diff = pct_diff(source_count, target_count)
        rows.append({
            "table": table,
            "source_rows": source_count,
            "target_rows": target_count,
            "diff_pct": diff * 100,
            "status": "PASS" if diff == 0 else "FAIL",
        })
    return pd.DataFrame(rows)


def get_distinct_summary(cursor, source_schema, target_schema, tables):
    rows = []
    for table in tables:
        for col in get_columns(cursor, source_schema, table):
            cursor.execute(f"SELECT COUNT(DISTINCT `{col}`) FROM `{source_schema}`.`{table}`")
            source_count = cursor.fetchone()[0]
            cursor.execute(f"SELECT COUNT(DISTINCT `{col}`) FROM `{target_schema}`.`{table}`")
            target_count = cursor.fetchone()[0]
            diff = pct_diff(source_count, target_count)
            rows.append({
                "table": table,
                "column": col,
                "source_distinct": source_count,
                "target_distinct": target_count,
                "diff_pct": diff * 100,
                "status": "PASS" if diff < 20 else "FAIL",
            })
    return pd.DataFrame(rows)


def histogram_probabilities(hist):
    buckets = hist.get("buckets", [])
    hist_type = hist.get("histogram-type")
    probs = []
    prev = 0.0
    for bucket in buckets:
        cumulative = bucket[-2] if hist_type == "equi-height" else bucket[1]
        probs.append(max(0.0, cumulative - prev))
        prev = cumulative
    return probs


def histogram_diff(source_hist, target_hist):
    if not source_hist or not target_hist:
        return 1.0
    source_probs = histogram_probabilities(source_hist)
    target_probs = histogram_probabilities(target_hist)
    n = min(len(source_probs), len(target_probs))
    if n == 0:
        return 1.0
    return 0.5 * sum(abs(source_probs[i] - target_probs[i]) for i in range(n))


def get_histograms(cursor, schema, table):
    df = fetch_df(
        cursor,
        """
        SELECT COLUMN_NAME, HISTOGRAM
        FROM information_schema.column_statistics
        WHERE SCHEMA_NAME = %s AND TABLE_NAME = %s
        """,
        (schema, table),
    )
    return {
        row["COLUMN_NAME"]: json.loads(row["HISTOGRAM"])
        for _, row in df.iterrows()
        if row["HISTOGRAM"]
    }


def get_histogram_summary(cursor, source_schema, target_schema, tables):
    rows = []
    for table in tables:
        source_hist = get_histograms(cursor, source_schema, table)
        target_hist = get_histograms(cursor, target_schema, table)
        indexed_cols = get_indexed_columns(cursor, source_schema, table)
        column_types = get_column_types(cursor, source_schema, table)
        for col in sorted(set(source_hist) | set(target_hist)):
            if col not in source_hist:
                diff = 1.0
                reason = "missing in source"
            elif col not in target_hist:
                diff = 1.0
                reason = "missing in target"
            else:
                diff = histogram_diff(source_hist[col], target_hist[col])
                reason = "compared"
            col_type = column_types.get(col, "unknown")
            indexed = col in indexed_cols
            if diff < 0.05:
                status = "PASS"
            elif is_string_type(col_type) and not indexed:
                status = "NOTE"
            elif is_decimal_type(col_type) and not indexed:
                status = "NOTE"
            else:
                status = "FAIL"
            rows.append({
                "table": table,
                "column": col,
                "column_type": col_type,
                "indexed": indexed,
                "histogram_diff": diff,
                "diff_pct": diff * 100,
                "reason": reason,
                "status": status,
            })
    return pd.DataFrame(rows)


def get_frequency_df(cursor, source_schema, target_schema, table, column):
    query = f"""
        WITH source_freq AS (
            SELECT CAST(`{column}` AS CHAR) AS value, COUNT(*) AS source_count
            FROM `{source_schema}`.`{table}`
            GROUP BY `{column}`
        ),
        target_freq AS (
            SELECT CAST(`{column}` AS CHAR) AS value, COUNT(*) AS target_count
            FROM `{target_schema}`.`{table}`
            GROUP BY `{column}`
        )
        SELECT
            COALESCE(source_freq.value, target_freq.value) AS value,
            COALESCE(source_count, 0) AS source_count,
            COALESCE(target_count, 0) AS target_count
        FROM source_freq
        LEFT JOIN target_freq USING (value)
        UNION
        SELECT
            COALESCE(source_freq.value, target_freq.value) AS value,
            COALESCE(source_count, 0) AS source_count,
            COALESCE(target_count, 0) AS target_count
        FROM target_freq
        LEFT JOIN source_freq USING (value)
        WHERE source_freq.value IS NULL
    """
    df = fetch_df(cursor, query)
    if df.empty:
        return df
    df["source_count"] = df["source_count"].astype(int)
    df["target_count"] = df["target_count"].astype(int)
    df["sort_value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.sort_values(["sort_value", "value"], na_position="last")
    return df.drop(columns=["sort_value"])


def get_fk_orphans(cursor, source_schema, target_schema):
    # TPC-H checks. If a table is absent, skip that check.
    checks = [
        ("nation_region", "nation n", "region r", "n.n_regionkey = r.r_regionkey", "r.r_regionkey IS NULL"),
        ("supplier_nation", "supplier s", "nation n", "s.s_nationkey = n.n_nationkey", "n.n_nationkey IS NULL"),
        ("customer_nation", "customer c", "nation n", "c.c_nationkey = n.n_nationkey", "n.n_nationkey IS NULL"),
        ("orders_customer", "orders o", "customer c", "o.o_custkey = c.c_custkey", "c.c_custkey IS NULL"),
        ("lineitem_orders", "lineitem l", "orders o", "l.l_orderkey = o.o_orderkey", "o.o_orderkey IS NULL"),
        ("lineitem_partsupp", "lineitem l", "partsupp ps", "l.l_partkey = ps.ps_partkey AND l.l_suppkey = ps.ps_suppkey", "ps.ps_partkey IS NULL"),
    ]
    rows = []
    for schema_name, label_prefix in ((source_schema, "source"), (target_schema, "target")):
        for name, child, parent, join_expr, orphan_expr in checks:
            child_table = child.split()[0]
            parent_table = parent.split()[0]
            try:
                cursor.execute(f"""
                    SELECT COUNT(*)
                    FROM `{schema_name}`.{child}
                    LEFT JOIN `{schema_name}`.{parent}
                      ON {join_expr}
                    WHERE {orphan_expr}
                """)
                count = cursor.fetchone()[0]
            except mysql.connector.Error:
                continue
            rows.append({
                "schema": label_prefix,
                "check": name,
                "child_table": child_table,
                "parent_table": parent_table,
                "orphan_count": count,
                "status": "PASS" if count == 0 else "FAIL",
            })
    return pd.DataFrame(rows)


def figure_to_html(fig, include_plotlyjs=False):
    return fig.to_html(full_html=False, include_plotlyjs=include_plotlyjs)


def build_summary_figure(row_df, hist_df, distinct_df, orphan_df):
    labels = ["row counts", "histograms", "distinct counts", "FK orphans"]
    pass_counts = [
        int((row_df["status"] == "PASS").sum()),
        int((hist_df["status"] == "PASS").sum()) if not hist_df.empty else 0,
        int((distinct_df["status"] == "PASS").sum()),
        int((orphan_df["status"] == "PASS").sum()) if not orphan_df.empty else 0,
    ]
    fail_counts = [
        int((row_df["status"] == "FAIL").sum()),
        int((hist_df["status"] == "FAIL").sum()) if not hist_df.empty else 0,
        int((distinct_df["status"] == "FAIL").sum()),
        int((orphan_df["status"] == "FAIL").sum()) if not orphan_df.empty else 0,
    ]
    fig = go.Figure()
    fig.add_bar(name="PASS", x=labels, y=pass_counts, marker_color="#2ca02c")
    fig.add_bar(name="FAIL", x=labels, y=fail_counts, marker_color="#d62728")
    if not hist_df.empty:
        note_counts = [0, int((hist_df["status"] == "NOTE").sum()), 0, 0]
        fig.add_bar(name="NOTE", x=labels, y=note_counts, marker_color="#ffbf00")
    fig.update_layout(
        title="Validation Status Summary",
        barmode="stack",
        height=420,
        margin=dict(l=40, r=20, t=60, b=40),
    )
    return fig


def build_histogram_heatmap(hist_df):
    if hist_df.empty:
        return None
    pivot = hist_df.pivot_table(
        index="table",
        columns="column",
        values="histogram_diff",
        aggfunc="max",
        fill_value=0,
    )
    fig = go.Figure(
        data=go.Heatmap(
            z=pivot.values,
            x=pivot.columns,
            y=pivot.index,
            colorscale="RdYlGn_r",
            zmin=0,
            zmax=max(0.10, float(pivot.values.max()) if pivot.size else 0.10),
            colorbar=dict(title="diff"),
        )
    )
    fig.update_layout(
        title="Histogram Difference Heatmap",
        height=max(420, 40 * len(pivot.index)),
        margin=dict(l=120, r=20, t=60, b=160),
    )
    return fig


def build_frequency_figure(freq_map):
    if not freq_map:
        return None
    rows = len(freq_map)
    fig = make_subplots(
        rows=rows,
        cols=1,
        subplot_titles=list(freq_map.keys()),
        vertical_spacing=min(0.08, 0.35 / max(rows, 1)),
    )
    for idx, (label, df) in enumerate(freq_map.items(), start=1):
        fig.add_bar(
            x=df["value"],
            y=df["source_count"],
            name="source" if idx == 1 else None,
            marker_color="#1f77b4",
            showlegend=idx == 1,
            row=idx,
            col=1,
        )
        fig.add_bar(
            x=df["value"],
            y=df["target_count"],
            name="target" if idx == 1 else None,
            marker_color="#ff7f0e",
            showlegend=idx == 1,
            row=idx,
            col=1,
        )
    fig.update_layout(
        title="Source vs Target Frequency Distributions",
        barmode="group",
        height=max(420, 280 * rows),
        margin=dict(l=60, r=20, t=80, b=50),
    )
    return fig


def table_html(df, title, max_rows=30):
    if df.empty:
        body = "<p>No rows.</p>"
    else:
        body = df.head(max_rows).to_html(index=False, classes="data-table")
        if len(df) > max_rows:
            body += f"<p class='note'>Showing first {max_rows} of {len(df)} rows.</p>"
    return f"<section><h2>{title}</h2>{body}</section>"


def generate_report(args):
    conn = connect(args)
    cursor = conn.cursor()
    try:
        tables = sorted(set(get_tables(cursor, args.source_schema)) & set(get_tables(cursor, args.target_schema)))
        row_df = get_row_counts(cursor, args.source_schema, args.target_schema, tables)
        distinct_df = get_distinct_summary(cursor, args.source_schema, args.target_schema, tables)
        hist_df = get_histogram_summary(cursor, args.source_schema, args.target_schema, tables)
        orphan_df = get_fk_orphans(cursor, args.source_schema, args.target_schema)

        freq_map = {}
        source_tables = set(get_tables(cursor, args.source_schema))
        for table, column in DEFAULT_COLUMNS:
            if table not in source_tables:
                continue
            cols = set(get_columns(cursor, args.source_schema, table))
            if column not in cols:
                continue
            df = get_frequency_df(cursor, args.source_schema, args.target_schema, table, column)
            if not df.empty and len(df) <= args.max_frequency_values:
                freq_map[f"{table}.{column}"] = df

        top_hist = hist_df.sort_values("histogram_diff", ascending=False) if not hist_df.empty else hist_df
        top_distinct = distinct_df.sort_values("diff_pct", ascending=False)

        figures = [
            build_summary_figure(row_df, hist_df, distinct_df, orphan_df),
            build_histogram_heatmap(hist_df),
            build_frequency_figure(freq_map),
        ]

        html_parts = [
            "<!doctype html><html><head><meta charset='utf-8'>",
            "<title>DataGenX Validation Report</title>",
            "<style>",
            "body{font-family:Arial,sans-serif;margin:32px;color:#222}",
            "h1,h2{margin-bottom:8px}",
            ".meta,.note{color:#666}",
            ".data-table{border-collapse:collapse;width:100%;font-size:13px}",
            ".data-table th,.data-table td{border:1px solid #ddd;padding:6px;text-align:left}",
            ".data-table th{background:#f5f5f5}",
            "section{margin:32px 0}",
            "</style></head><body>",
            "<h1>DataGenX Validation Report</h1>",
            f"<p class='meta'>Source: <code>{args.source_schema}</code> &nbsp; Target: <code>{args.target_schema}</code></p>",
        ]

        first_figure = True
        for fig in figures:
            if fig is not None:
                html_parts.append(figure_to_html(fig, include_plotlyjs=first_figure))
                first_figure = False

        html_parts.extend([
            table_html(row_df, "Row Count Comparison"),
            table_html(top_hist, "Top Histogram Differences"),
            table_html(top_distinct, "Top Distinct Count Differences"),
            table_html(orphan_df, "Referential Integrity Orphan Checks"),
            "</body></html>",
        ])

        output = Path(args.output)
        output.write_text("\n".join(html_parts))
        return output
    finally:
        cursor.close()
        conn.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Generate an HTML DataGenX validation report.")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--user", default=USER)
    parser.add_argument("--password", default=PASSWORD)
    parser.add_argument("--source-schema", default=SOURCE_SCHEMA)
    parser.add_argument("--target-schema", default=TARGET_SCHEMA)
    parser.add_argument("--output", default="/tmp/tpch_validation_report.html")
    parser.add_argument("--max-frequency-values", type=int, default=100)
    return parser.parse_args()


def main():
    args = parse_args()
    output = generate_report(args)
    print(f"Wrote validation report to {output}")


if __name__ == "__main__":
    main()
