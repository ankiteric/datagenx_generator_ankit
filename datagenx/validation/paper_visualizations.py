#!/usr/bin/env python3
"""Generate paper-oriented DataGenX validation visualizations.

This report is intentionally different from validation_report.py. The standard
report is an engineering dashboard. This file creates fewer, more explanatory
figures that are suitable for paper screenshots or figure drafting.
"""

import argparse
import html
import re
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from config import HOST, PASSWORD, SOURCE_SCHEMA, TARGET_SCHEMA, USER
from datagenx.validation.validation_report import (
    connect,
    figure_to_html,
    get_fk_orphans,
    get_histogram_summary,
    get_histograms,
    get_row_counts,
    get_row_overlap,
    get_distinct_summary,
    get_tables,
    histogram_probabilities,
    pct_diff,
)


DEFAULT_PLAN_FILES = [
    "/tmp/tpch_all_query_plan_validation_after_ndv_fix.txt",
    "/tmp/tpch_all_query_plan_validation_rerun.txt",
    "/tmp/tpch_all_query_plan_validation.txt",
]


def status_counts(df):
    if df.empty or "status" not in df:
        return {"PASS": 0, "NOTE": 0, "FAIL": 0}
    return {status: int((df["status"] == status).sum()) for status in ("PASS", "NOTE", "FAIL")}


def worst_status(statuses):
    order = {"PASS": 0, "NOTE": 1, "FAIL": 2}
    statuses = [status for status in statuses if status]
    if not statuses:
        return "PASS"
    return max(statuses, key=lambda status: order.get(status, 0))


def parse_plan_file(path):
    path = Path(path)
    if not path.exists():
        return {}, pd.DataFrame()

    text = path.read_text(errors="replace")
    summary = {}
    for key, label in (
        ("total", "Total queries"),
        ("identical", "Plan IDENTICAL"),
        ("similar", "Plan SIMILAR"),
        ("different", "Plan DIFFERENT"),
    ):
        match = re.search(rf"{re.escape(label)}:\s+(\d+)", text)
        if match:
            summary[key] = int(match.group(1))

    rows = []
    for line in text.splitlines():
        match = re.match(r"^(Q\d+\.SQL)\s+\|\s+([A-Z]+)\s+\|\s+(.*)$", line.strip())
        if match:
            rows.append({
                "query": match.group(1).lower().replace(".sql", ""),
                "status": match.group(2),
                "details": match.group(3).strip(),
            })

    return summary, pd.DataFrame(rows)


def find_plan_file(explicit):
    if explicit:
        return explicit if Path(explicit).exists() else None
    for candidate in DEFAULT_PLAN_FILES:
        if Path(candidate).exists():
            return candidate
    return None


def build_metric_cards(row_df, hist_df, distinct_df, orphan_df, overlap_df, plan_summary):
    rows = status_counts(row_df)
    hist = status_counts(hist_df)
    distinct = status_counts(distinct_df)
    fk = status_counts(orphan_df)
    privacy = status_counts(overlap_df)

    cards = [
        ("Rows", f"{rows['PASS']}/{len(row_df)}", "tables match source row count"),
        ("Histograms", f"{hist['PASS']}/{len(hist_df)}", "distribution-shape checks pass"),
        ("NDV", f"{distinct['PASS']}/{len(distinct_df)}", "distinct-count checks pass"),
        ("FK Integrity", f"{fk['PASS']}/{len(orphan_df)}", "source and target orphan checks pass"),
        ("Privacy", f"{privacy['PASS']}/{len(overlap_df)}", "exact row overlap below threshold"),
    ]
    if plan_summary:
        total = plan_summary.get("total", 0)
        identical = plan_summary.get("identical", 0)
        cards.append(("TPC-H Plans", f"{identical}/{total}", "queries with identical plan shape"))

    parts = ["<section class='summary'><h2>Validation At A Glance</h2><div class='cards'>"]
    for title, value, caption in cards:
        parts.append(
            "<div class='card'>"
            f"<div class='card-title'>{html.escape(title)}</div>"
            f"<div class='card-value'>{html.escape(value)}</div>"
            f"<div class='card-caption'>{html.escape(caption)}</div>"
            "</div>"
        )
    parts.append("</div></section>")
    return "".join(parts)


def methods_note():
    return """
<section>
  <h2>How To Read This Report</h2>
  <p>
    These figures are intended for paper drafting rather than day-to-day debugging.
    They summarize whether the synthetic target preserves optimizer-relevant
    properties of the source schema while avoiding source-row reuse.
  </p>
  <p>
    Histogram comparisons use <strong>distribution shape</strong>: bucket probability
    masses are compared after sorting by mass, so source and synthetic literal
    values are allowed to differ. NDV comparisons check column cardinality.
    Privacy checks look for exact row overlap using row hashes. Plan checks compare
    MySQL <code>EXPLAIN</code> plan shape across rendered TPC-H queries.
  </p>
</section>
"""


def build_table_matrix(row_df, hist_df, distinct_df, orphan_df, overlap_df, tables):
    target_orphans = orphan_df[orphan_df["schema"] == "target"] if not orphan_df.empty else orphan_df
    rows = []
    for table in tables:
        table_rows = row_df[row_df["table"] == table]
        table_hist = hist_df[hist_df["table"] == table] if not hist_df.empty else hist_df
        table_distinct = distinct_df[distinct_df["table"] == table]
        table_orphans = target_orphans[target_orphans["child_table"] == table] if not target_orphans.empty else target_orphans
        table_overlap = overlap_df[overlap_df["table"] == table]

        row_status = table_rows["status"].iloc[0] if not table_rows.empty else "PASS"
        hist_status = worst_status(table_hist["status"].tolist()) if not table_hist.empty else "PASS"
        distinct_status = worst_status(table_distinct["status"].tolist()) if not table_distinct.empty else "PASS"
        fk_status = worst_status(table_orphans["status"].tolist()) if not table_orphans.empty else "PASS"
        privacy_status = worst_status(table_overlap["status"].tolist()) if not table_overlap.empty else "PASS"

        rows.append({
            "table": table,
            "Rows": row_status,
            "Histograms": hist_status,
            "NDV": distinct_status,
            "FK": fk_status,
            "Privacy": privacy_status,
            "max_histogram_diff_pct": float(table_hist["diff_pct"].max()) if not table_hist.empty else 0.0,
            "max_ndv_diff_pct": float(table_distinct["diff_pct"].max()) if not table_distinct.empty else 0.0,
            "overlap_pct": float(table_overlap["overlap_pct"].iloc[0]) if not table_overlap.empty else 0.0,
        })
    return pd.DataFrame(rows)


def fig_validation_matrix(matrix_df):
    metrics = ["Rows", "Histograms", "NDV", "FK", "Privacy"]
    status_to_value = {"PASS": 0, "NOTE": 1, "FAIL": 2}
    z = [[status_to_value.get(row[metric], 0) for metric in metrics] for _, row in matrix_df.iterrows()]
    text = [[row[metric] for metric in metrics] for _, row in matrix_df.iterrows()]
    fig = go.Figure(go.Heatmap(
        z=z,
        x=metrics,
        y=matrix_df["table"],
        text=text,
        texttemplate="%{text}",
        colorscale=[
            [0.0, "#1b9e77"],
            [0.49, "#1b9e77"],
            [0.50, "#d9a441"],
            [0.74, "#d9a441"],
            [0.75, "#d95f02"],
            [1.0, "#d95f02"],
        ],
        zmin=0,
        zmax=2,
        showscale=False,
    ))
    fig.update_layout(
        title="Table-Level Validation Matrix",
        height=430,
        margin=dict(l=110, r=30, t=70, b=60),
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    return fig


def fig_drift_by_table(matrix_df):
    df = matrix_df.sort_values("table")
    fig = go.Figure()
    fig.add_bar(
        x=df["table"],
        y=df["max_histogram_diff_pct"],
        name="max histogram drift",
        marker_color="#4c78a8",
    )
    fig.add_bar(
        x=df["table"],
        y=df["max_ndv_diff_pct"],
        name="max NDV drift",
        marker_color="#f58518",
    )
    fig.update_layout(
        title="Maximum Drift by Table",
        barmode="group",
        yaxis_title="drift %",
        height=430,
        margin=dict(l=60, r=30, t=70, b=80),
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    return fig


def fig_ndv_scatter(distinct_df):
    df = distinct_df.copy()
    df = df[(df["source_distinct"] > 0) & (df["target_distinct"] > 0)]
    if df.empty:
        return None

    df["label"] = df["table"] + "." + df["column"]
    max_value = float(max(df["source_distinct"].max(), df["target_distinct"].max()))
    min_value = 1.0
    color_map = {"PASS": "#1b9e77", "NOTE": "#d9a441", "FAIL": "#d95f02"}

    fig = go.Figure()
    for status, group in df.groupby("status"):
        fig.add_trace(go.Scatter(
            x=group["source_distinct"],
            y=group["target_distinct"],
            mode="markers",
            name=status,
            marker=dict(
                size=[12 if indexed else 8 for indexed in group["indexed"]],
                color=color_map.get(status, "#666"),
                opacity=0.82,
                line=dict(color="white", width=0.8),
            ),
            text=group["label"],
            customdata=group[["diff_pct", "column_type", "indexed"]],
            hovertemplate=(
                "%{text}<br>source NDV=%{x}<br>target NDV=%{y}"
                "<br>diff=%{customdata[0]:.3f}%"
                "<br>type=%{customdata[1]}<br>indexed=%{customdata[2]}<extra></extra>"
            ),
        ))
    fig.add_trace(go.Scatter(
        x=[min_value, max_value],
        y=[min_value, max_value],
        mode="lines",
        name="perfect match",
        line=dict(color="#333", dash="dash"),
        hoverinfo="skip",
    ))
    fig.update_layout(
        title="NDV Preservation: Source vs Synthetic",
        xaxis=dict(title="source NDV", type="log"),
        yaxis=dict(title="target NDV", type="log"),
        height=520,
        margin=dict(l=70, r=30, t=70, b=60),
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    return fig


def select_histogram_examples(hist_df, limit=4):
    if hist_df.empty:
        return []
    preferred = [
        ("customer", "c_nationkey"),
        ("orders", "o_custkey"),
        ("lineitem", "l_suppkey"),
        ("lineitem", "l_shipdate"),
        ("part", "p_size"),
    ]
    selected = []
    available = {(row["table"], row["column"]) for _, row in hist_df.iterrows()}
    for item in preferred:
        if item in available and item not in selected:
            selected.append(item)
    for _, row in hist_df.sort_values("diff_pct", ascending=False).iterrows():
        item = (row["table"], row["column"])
        if item not in selected:
            selected.append(item)
        if len(selected) >= limit:
            break
    return selected[:limit]


def fig_histogram_bucket_shapes(cursor, source_schema, target_schema, hist_df):
    examples = select_histogram_examples(hist_df)
    if not examples:
        return None
    fig = make_subplots(
        rows=len(examples),
        cols=1,
        subplot_titles=[f"{table}.{column}" for table, column in examples],
        vertical_spacing=0.10,
    )
    for row_idx, (table, column) in enumerate(examples, start=1):
        source_hist = get_histograms(cursor, source_schema, table).get(column)
        target_hist = get_histograms(cursor, target_schema, table).get(column)
        source_probs = histogram_probabilities(source_hist) if source_hist else []
        target_probs = histogram_probabilities(target_hist) if target_hist else []
        n = max(len(source_probs), len(target_probs))
        source_probs = source_probs + [0.0] * (n - len(source_probs))
        target_probs = target_probs + [0.0] * (n - len(target_probs))
        x = list(range(1, n + 1))
        fig.add_trace(go.Scatter(
            x=x,
            y=[p * 100 for p in source_probs],
            mode="lines+markers",
            name="source" if row_idx == 1 else None,
            showlegend=row_idx == 1,
            line=dict(color="#4c78a8"),
        ), row=row_idx, col=1)
        fig.add_trace(go.Scatter(
            x=x,
            y=[p * 100 for p in target_probs],
            mode="lines+markers",
            name="synthetic" if row_idx == 1 else None,
            showlegend=row_idx == 1,
            line=dict(color="#f58518", dash="dot"),
        ), row=row_idx, col=1)
        fig.update_yaxes(title_text="bucket mass %", row=row_idx, col=1)
    fig.update_xaxes(title_text="bucket rank")
    fig.update_layout(
        title="Histogram Shape Preservation",
        height=max(520, 240 * len(examples)),
        margin=dict(l=70, r=30, t=80, b=60),
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    return fig


def fig_plan_summary(plan_summary, plan_df):
    if not plan_summary and plan_df.empty:
        return None
    total = plan_summary.get("total", len(plan_df))
    labels = ["IDENTICAL", "SIMILAR", "DIFFERENT"]
    values = [
        plan_summary.get("identical", int((plan_df["status"] == "IDENTICAL").sum()) if not plan_df.empty else 0),
        plan_summary.get("similar", int((plan_df["status"] == "SIMILAR").sum()) if not plan_df.empty else 0),
        plan_summary.get("different", int((plan_df["status"] == "DIFFERENT").sum()) if not plan_df.empty else 0),
    ]
    fig = make_subplots(
        rows=1,
        cols=2,
        specs=[[{"type": "bar"}, {"type": "heatmap"}]],
        column_widths=[0.38, 0.62],
        subplot_titles=["Plan shape summary", "Per-query result"],
    )
    fig.add_trace(go.Bar(
        x=labels,
        y=values,
        marker_color=["#1b9e77", "#d9a441", "#d95f02"],
        text=[f"{value}/{total}" for value in values],
        textposition="auto",
        showlegend=False,
    ), row=1, col=1)

    if not plan_df.empty:
        status_to_value = {"IDENTICAL": 0, "SIMILAR": 1, "DIFFERENT": 2}
        ordered = plan_df.copy()
        ordered["query_num"] = ordered["query"].str.extract(r"q(\d+)").astype(int)
        ordered = ordered.sort_values("query_num")
        fig.add_trace(go.Heatmap(
            z=[[status_to_value.get(status, 0) for status in ordered["status"]]],
            x=ordered["query"],
            y=["plan"],
            text=[ordered["status"].tolist()],
            texttemplate="%{text}",
            colorscale=[
                [0.0, "#1b9e77"],
                [0.49, "#1b9e77"],
                [0.50, "#d9a441"],
                [0.74, "#d9a441"],
                [0.75, "#d95f02"],
                [1.0, "#d95f02"],
            ],
            zmin=0,
            zmax=2,
            showscale=False,
        ), row=1, col=2)
    fig.update_layout(
        title="TPC-H Query Plan Equivalence",
        height=430,
        margin=dict(l=60, r=30, t=80, b=80),
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    return fig


def fig_privacy_fk(orphan_df, overlap_df):
    fk_target = orphan_df[orphan_df["schema"] == "target"] if not orphan_df.empty else orphan_df
    fig = make_subplots(
        rows=1,
        cols=2,
        subplot_titles=["Target FK orphan counts", "Exact row overlap"],
        column_widths=[0.52, 0.48],
    )
    if not fk_target.empty:
        fig.add_trace(go.Bar(
            x=fk_target["check"],
            y=fk_target["orphan_count"],
            marker_color="#1b9e77",
            showlegend=False,
        ), row=1, col=1)
    if not overlap_df.empty:
        fig.add_trace(go.Bar(
            x=overlap_df["table"],
            y=overlap_df["overlap_pct"],
            marker_color="#4c78a8",
            showlegend=False,
        ), row=1, col=2)
    fig.update_yaxes(title_text="orphans", row=1, col=1)
    fig.update_yaxes(title_text="overlap %", row=1, col=2)
    fig.update_layout(
        title="Integrity and Privacy Checks",
        height=440,
        margin=dict(l=60, r=30, t=80, b=110),
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    return fig


def table_html(df, title, max_rows=20):
    if df.empty:
        body = "<p>No rows.</p>"
    else:
        body = df.head(max_rows).to_html(index=False, classes="data-table", escape=True)
        if len(df) > max_rows:
            body += f"<p class='note'>Showing first {max_rows} of {len(df)} rows.</p>"
    return f"<section><h2>{html.escape(title)}</h2>{body}</section>"


def generate(args):
    plan_file = find_plan_file(args.plan_file)
    plan_summary, plan_df = parse_plan_file(plan_file) if plan_file else ({}, pd.DataFrame())

    conn = connect(args)
    cursor = conn.cursor()
    try:
        tables = sorted(set(get_tables(cursor, args.source_schema)) & set(get_tables(cursor, args.target_schema)))
        row_df = get_row_counts(cursor, args.source_schema, args.target_schema, tables)
        hist_df = get_histogram_summary(cursor, args.source_schema, args.target_schema, tables)
        distinct_df = get_distinct_summary(cursor, args.source_schema, args.target_schema, tables)
        orphan_df = get_fk_orphans(cursor, args.source_schema, args.target_schema)
        overlap_df = get_row_overlap(cursor, args.source_schema, args.target_schema, tables)
        matrix_df = build_table_matrix(row_df, hist_df, distinct_df, orphan_df, overlap_df, tables)

        top_distinct = distinct_df.sort_values("diff_pct", ascending=False).head(15)
        top_hist = hist_df.sort_values("diff_pct", ascending=False).head(15)

        figures = [
            fig_validation_matrix(matrix_df),
            fig_drift_by_table(matrix_df),
            fig_ndv_scatter(distinct_df),
            fig_histogram_bucket_shapes(cursor, args.source_schema, args.target_schema, hist_df),
            fig_plan_summary(plan_summary, plan_df),
            fig_privacy_fk(orphan_df, overlap_df),
        ]

        parts = [
            "<!doctype html><html><head><meta charset='utf-8'>",
            "<title>DataGenX Paper Visualizations</title>",
            "<style>",
            "body{font-family:Inter,Arial,sans-serif;margin:34px;color:#222;background:#fff}",
            "h1{font-size:30px;margin-bottom:6px}h2{font-size:20px;margin-bottom:10px}",
            ".subtitle,.note{color:#666}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}",
            ".card{border:1px solid #ddd;border-radius:6px;padding:14px;background:#fbfbfb}",
            ".card-title{font-size:12px;text-transform:uppercase;color:#666;font-weight:700}",
            ".card-value{font-size:28px;font-weight:800;margin-top:8px}",
            ".card-caption{font-size:12px;color:#777;margin-top:4px;line-height:1.35}",
            "section{margin:26px 0;padding:18px;border:1px solid #eee;border-radius:6px;background:white}",
            ".figure-caption{font-size:13px;color:#555;line-height:1.45;margin:8px 0 0}",
            ".data-table{border-collapse:collapse;width:100%;font-size:12px}",
            ".data-table th,.data-table td{border:1px solid #ddd;padding:6px;text-align:left}",
            ".data-table th{background:#f6f6f6}",
            "</style></head><body>",
            "<h1>DataGenX Paper Visualizations</h1>",
            f"<p class='subtitle'>Source <code>{html.escape(args.source_schema)}</code> vs synthetic target <code>{html.escape(args.target_schema)}</code>",
            f"{' using plan file <code>' + html.escape(plan_file) + '</code>' if plan_file else ''}</p>",
            methods_note(),
            build_metric_cards(row_df, hist_df, distinct_df, orphan_df, overlap_df, plan_summary),
        ]

        first = True
        captions = [
            (
                "<strong>What it depicts:</strong> Each row is a TPC-H table and each column is a validation dimension: "
                "row count, histogram shape, NDV, foreign-key integrity, and privacy. "
                "<strong>How to read it:</strong> PASS means that table satisfies the corresponding check. "
                "This figure is the compact correctness matrix for the whole generated database."
            ),
            (
                "<strong>What it depicts:</strong> For each table, the bars show the largest residual histogram drift "
                "and largest residual NDV drift observed among that table's columns. "
                "<strong>How to read it:</strong> Smaller bars are better. Low values indicate that the synthetic table "
                "preserves the source table's optimizer-visible statistics. This is useful for explaining that the "
                "target is not merely structurally valid; it is statistically close."
            ),
            (
                "<strong>What it depicts:</strong> Each point is one column. The x-axis is source NDV and the y-axis is "
                "synthetic target NDV, both on a log scale. The dashed diagonal is perfect preservation. "
                "<strong>How to read it:</strong> Points near the diagonal mean DataGenX preserved column cardinality. "
                "Larger markers indicate indexed columns, which are especially important for optimizer behavior."
            ),
            (
                "<strong>What it depicts:</strong> For selected columns, source and synthetic histogram bucket probability "
                "masses are plotted by bucket rank. The x-axis is bucket rank, not the original value. "
                "<strong>How to read it:</strong> Overlapping lines mean the synthetic data preserves the frequency shape "
                "seen by the optimizer. The actual source values are intentionally not required to match synthetic values, "
                "which supports the privacy-preserving generation claim."
            ),
            (
                "<strong>What it depicts:</strong> The left panel summarizes how many rendered TPC-H queries have identical, "
                "similar, or different EXPLAIN plan shapes. The right panel shows the result for each query q1 through q22. "
                "<strong>How to read it:</strong> Green/IDENTICAL cells mean the optimizer chose the same plan shape on the "
                "source and synthetic schemas. This connects statistic preservation to optimizer behavior."
            ),
            (
                "<strong>What it depicts:</strong> The left panel shows target-side foreign-key orphan counts. The right panel "
                "shows exact row overlap percentage between source and synthetic tables. "
                "<strong>How to read it:</strong> Zero orphan counts mean referential integrity is preserved. Near-zero row "
                "overlap means the synthetic database is not copying exact source rows while still matching statistics."
            ),
        ]
        for fig, caption in zip(figures, captions):
            if fig is None:
                continue
            parts.append("<section>")
            parts.append(figure_to_html(fig, include_plotlyjs=first))
            parts.append(f"<p class='figure-caption'>{caption}</p>")
            parts.append("</section>")
            first = False

        parts.extend([
            table_html(matrix_df, "Table Matrix Data"),
            table_html(top_hist, "Top Histogram Drift Columns"),
            table_html(top_distinct, "Top NDV Drift Columns"),
            "</body></html>",
        ])

        output = Path(args.output)
        output.write_text("\n".join(parts))
        return output
    finally:
        cursor.close()
        conn.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Generate paper-oriented DataGenX visualizations.")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--user", default=USER)
    parser.add_argument("--password", default=PASSWORD)
    parser.add_argument("--source-schema", default=SOURCE_SCHEMA)
    parser.add_argument("--target-schema", default=TARGET_SCHEMA)
    parser.add_argument("--plan-file", help="Optional TPC-H plan comparison output file")
    parser.add_argument("--output", default="/tmp/tpch_paper_visualizations.html")
    return parser.parse_args()


def main():
    output = generate(parse_args())
    print(f"Wrote paper visualizations to {output}")


if __name__ == "__main__":
    main()
