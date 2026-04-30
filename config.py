# config.py - Central configuration for DataGenX Generator
#
# Change SOURCE_SCHEMA and TARGET_SCHEMA to switch between benchmarks:
#   TPC-DS: SOURCE_SCHEMA = "tpcds", TARGET_SCHEMA = "tpcds_harsha"
#   TPC-H:  SOURCE_SCHEMA = "tpch",  TARGET_SCHEMA = "tpch_harsha"

# Database connection
HOST = "localhost"
USER = "root"
PASSWORD = "newpassword"

# Schema configuration
SOURCE_SCHEMA = "tpch"
TARGET_SCHEMA = "tpch_harsha"

# Paths
DBGEN_BINARY = "/Users/sreeharshar/work/db/datagenx/code/dbgen/target/release/dbgen"
DBGEN_FILES_DIR = "dbgen_files"
DBGEN_TMP_OUT_DIR = "dbgen_tmp_out"

# Generation settings
FILES_COUNT = "1"
ROWS_COUNT = "1000"
