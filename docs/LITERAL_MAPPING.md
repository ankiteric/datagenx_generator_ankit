# Literal Mapping and Query Rewriting

DataGenX normally validates distribution shape without preserving source
literals. For query testing, some predicates use source string literals:

```sql
WHERE l_returnflag = 'R'
WHERE l_shipmode IN ('MAIL', 'SHIP')
```

The synthetic target uses synthetic values, so the target-side query needs the
corresponding synthetic literals.

## Sensitive Mapping File

Build a local mapping file:

```bash
python3 validate.py literal-map \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx
```

Default output:

```text
generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json
```

This file is **sensitive** because it contains source literals. It is ignored by
git and should stay local.

Example mapping:

```json
{
  "lineitem.l_returnflag": [
    {
      "source_literal": "N",
      "target_literal": "1",
      "rank": 1
    },
    {
      "source_literal": "R",
      "target_literal": "2",
      "rank": 2
    }
  ]
}
```

## Rewrite a Query

Rewrite SQL text:

```bash
python3 validate.py rewrite-query \
  --mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json \
  --sql "select * from lineitem where l_returnflag = 'R';"
```

Rewrite a SQL file:

```bash
python3 validate.py rewrite-query \
  --mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json \
  --query-file queries_mysql/q12.sql \
  --output-file /tmp/q12_target.sql
```

The rewrite is conservative:

```text
unique source literal      -> rewritten
ambiguous source literal   -> left unchanged and reported
unmapped source literal    -> left unchanged
```

## Run Query/Plan Validation With Mapping

Use the original query for the source schema and the rewritten query for the
target schema:

```bash
python3 validate.py query q12 \
  --queries-dir /path/to/queries_mysql \
  --literal-mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json
```

Run all plan comparisons with target-side literal rewriting:

```bash
python3 validate.py plans \
  --queries-dir /path/to/queries_mysql \
  --literal-mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json
```

## Privacy Note

The mapping file is not part of the synthetic dataset. It is a local validation
tool that bridges source query literals to synthetic target literals. Sharing it
would expose source literals, so keep it private.
