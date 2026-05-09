# config.py - Central configuration for DataGenX Generator
#
# Change SOURCE_SCHEMA and TARGET_SCHEMA to switch between benchmarks:
#   Current TPC-H workflow:
#     SOURCE_SCHEMA = "tpch_vanilla"
#     TARGET_SCHEMA = "tpch_dbgenx"
#   TPC-DS example:
#     SOURCE_SCHEMA = "tpcds"
#     TARGET_SCHEMA = "tpcds_dbgenx"

# Database connection
HOST = "localhost"
USER = "root"
PASSWORD = "newpassword"

# Schema configuration
SOURCE_SCHEMA = "tpch_vanilla"
TARGET_SCHEMA = "tpch_dbgenx"

# Paths
# This must point to the Rust DataGenX dbgen binary, not the official TPC-H
# dbgen binary under /home/hmaduri/contribs/tpch-dbgen.
DBGEN_BINARY = "/home/hmaduri/contribs/dbgen/target/release/dbgen"
DBGEN_FILES_DIR = "generated/dbgen_files"
DBGEN_TMP_OUT_DIR = "generated/dbgen_tmp_out"

# Generation settings
FILES_COUNT = "1"
ROWS_COUNT = "1000"

# Database type (mysql or singlestore)
DB_TYPE = "mysql"
