"""
Schema extraction adapters for different database systems.
"""

from abc import ABC, abstractmethod
import mysql.connector
from mysql.connector import Error
import json
import re


class SchemaExtractor(ABC):
    """Abstract base class for database schema extraction."""

    def __init__(self, host, user, password, database, port=3306):
        self.host = host
        self.user = user
        self.password = password
        self.database = database
        self.port = port
        self.conn = None
        self.cursor = None

    def connect(self):
        """Establish database connection."""
        try:
            self.conn = mysql.connector.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database
            )
            self.cursor = self.conn.cursor()
            print(f"✅ Connected to {self.database} at {self.host}")
            return True
        except Error as e:
            print(f"❌ Connection failed: {e}")
            return False

    def close(self):
        """Close database connection."""
        if self.cursor:
            self.cursor.close()
        if self.conn and self.conn.is_connected():
            self.conn.close()

    def get_tables(self):
        """Get all tables in the database."""
        self.cursor.execute("""
            SELECT TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        """, (self.database,))
        return [t[0] for t in self.cursor.fetchall()]

    def get_columns(self, table):
        """Get all columns for a table with their types."""
        self.cursor.execute("""
            SELECT COLUMN_NAME, DATA_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
        """, (self.database, table))
        return {col: dtype.lower() for col, dtype in self.cursor.fetchall()}

    def get_primary_keys(self, table):
        """Get primary key columns for a table."""
        self.cursor.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
              AND CONSTRAINT_NAME = 'PRIMARY'
        """, (self.database, table))
        return {r[0] for r in self.cursor.fetchall()}

    def get_foreign_keys(self, table):
        """Get foreign key mappings: column -> (referenced_table, referenced_column)."""
        self.cursor.execute("""
            SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
              AND REFERENCED_TABLE_NAME IS NOT NULL
        """, (self.database, table))
        return {
            col: (ref_table, ref_col)
            for col, ref_table, ref_col in self.cursor.fetchall()
        }

    def get_table_ddl(self, table):
        """Get CREATE TABLE statement."""
        self.cursor.execute(f"SHOW CREATE TABLE `{self.database}`.`{table}`")
        return self.cursor.fetchone()[1]

    def get_table_dependencies(self):
        """Get foreign key dependencies: {table: [tables it depends on]}."""
        self.cursor.execute("""
            SELECT TABLE_NAME, REFERENCED_TABLE_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s
              AND REFERENCED_TABLE_NAME IS NOT NULL
        """, (self.database,))

        dependencies = {}
        for table, referenced_table in self.cursor.fetchall():
            if table not in dependencies:
                dependencies[table] = set()
            if referenced_table and referenced_table != table:
                dependencies[table].add(referenced_table)

        return {k: list(v) for k, v in dependencies.items()}

    @abstractmethod
    def analyze_table(self, table):
        """Run ANALYZE to update statistics."""
        pass

    @abstractmethod
    def get_column_histogram(self, table, column):
        """
        Get histogram for a column.
        Returns dict with format: {
            "histogram-type": "equi-height" | "singleton",
            "buckets": [[lower, upper, cumulative_freq, count], ...]
        }
        Returns None if no histogram available.
        """
        pass


class MySQLExtractor(SchemaExtractor):
    """Schema extractor for MySQL databases."""

    def analyze_table(self, table):
        """Run ANALYZE TABLE and update histograms for numeric columns."""
        self.cursor.execute(f"ANALYZE TABLE `{self.database}`.`{table}`")
        self.cursor.fetchall()

        # Get numeric columns
        self.cursor.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
              AND DATA_TYPE IN (
                  'tinyint','smallint','mediumint','int','bigint',
                  'decimal','numeric','float','double'
              )
        """, (self.database, table))

        cols = [c[0] for c in self.cursor.fetchall()]
        if not cols:
            return

        # Update histograms
        self.cursor.execute(
            f"""
            ANALYZE TABLE `{self.database}`.`{table}`
            UPDATE HISTOGRAM ON {','.join(f'`{c}`' for c in cols)}
            WITH 100 BUCKETS
            """
        )
        self.cursor.fetchall()

    def get_column_histogram(self, table, column):
        """Get histogram from MySQL's column_statistics."""
        try:
            self.cursor.execute("""
                SELECT HISTOGRAM
                FROM information_schema.column_statistics
                WHERE SCHEMA_NAME = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
            """, (self.database, table, column))

            result = self.cursor.fetchone()
            if result and result[0]:
                return json.loads(result[0])
            return None
        except Exception as e:
            # column_statistics might not exist in older MySQL versions
            return None


class SingleStoreExtractor(SchemaExtractor):
    """Schema extractor for SingleStore databases."""

    def connect(self):
        """Establish database connection with SingleStore-compatible auth."""
        try:
            self.conn = mysql.connector.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                auth_plugin='mysql_native_password',
            )
            self.cursor = self.conn.cursor()
            print(f"Connected to {self.database} at {self.host}")
            return True
        except Error as e:
            print(f"Connection failed: {e}")
            return False

    def get_primary_keys(self, table):
        """Get primary key columns for a SingleStore table.

        SingleStore uses UNIQUE KEY `pk` (UNENFORCED RELY) instead of PRIMARY KEY.
        Falls back to parsing DDL if information_schema doesn't have it.
        """
        # Try standard PRIMARY first
        result = super().get_primary_keys(table)
        if result:
            return result

        # SingleStore: try UNIQUE KEY named 'pk'
        self.cursor.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
              AND CONSTRAINT_NAME = 'pk'
        """, (self.database, table))
        result = {r[0] for r in self.cursor.fetchall()}
        if result:
            return result

        # Last resort: parse DDL for UNIQUE KEY `pk` or PRIMARY KEY
        ddl = self.get_table_ddl(table)
        for pattern in [
            r"PRIMARY\s+KEY\s*\(([^)]+)\)",
            r"UNIQUE\s+KEY\s+`pk`\s*\(([^)]+)\)",
        ]:
            m = re.search(pattern, ddl, re.IGNORECASE)
            if m:
                return {c.strip().strip('`') for c in m.group(1).split(',')}

        return set()

    def analyze_table(self, table):
        """Run ANALYZE TABLE to collect statistics."""
        # SingleStore ANALYZE syntax
        self.cursor.execute(f"ANALYZE TABLE `{self.database}`.`{table}`")
        self.cursor.fetchall()

    def get_column_histogram(self, table, column):
        """Get histogram from SingleStore's ADVANCED_HISTOGRAMS view."""
        try:
            # Try ADVANCED_HISTOGRAMS first (SingleStore 8.0+)
            self.cursor.execute("""
                SELECT BUCKET_INDEX, RANGE_MIN, RANGE_MAX,
                       CARDINALITY, UNIQUE_COUNT
                FROM information_schema.ADVANCED_HISTOGRAMS
                WHERE DATABASE_NAME = %s
                  AND TABLE_NAME = %s
                  AND COLUMN_NAME = %s
                  AND BUCKET_INDEX >= 0
                ORDER BY BUCKET_INDEX
            """, (self.database, table, column))

            buckets = self.cursor.fetchall()
            if buckets:
                return self._convert_singlestore_histogram(buckets)

        except Exception as e:
            # ADVANCED_HISTOGRAMS might not be available
            # Try alternative: COLUMN_STATISTICS
            try:
                self.cursor.execute("""
                    SELECT HISTOGRAM
                    FROM information_schema.COLUMN_STATISTICS
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME = %s
                      AND COLUMN_NAME = %s
                """, (self.database, table, column))

                result = self.cursor.fetchone()
                if result and result[0]:
                    # If it's JSON format (similar to MySQL)
                    if isinstance(result[0], str):
                        return json.loads(result[0])
                    return result[0]
            except:
                pass

        return None

    def _convert_singlestore_histogram(self, buckets):
        """
        Convert SingleStore ADVANCED_HISTOGRAMS buckets to standard format.
        Input: [(bucket_index, range_min, range_max, cardinality, unique_count), ...]
        Output: {"histogram-type": "equi-height", "buckets": [...]}
        """
        if not buckets:
            return None

        # Filter out invalid buckets (with None values)
        valid_buckets = [b for b in buckets if b[1] is not None and b[2] is not None and b[3] is not None]

        if not valid_buckets:
            return None

        histogram = {
            "histogram-type": "equi-height",
            "buckets": []
        }

        cumulative_freq = 0.0
        total_freq = sum(row[3] for row in valid_buckets)  # Sum all cardinalities

        if total_freq == 0:
            return None

        for bucket_index, range_min, range_max, cardinality, unique_count in valid_buckets:
            cumulative_freq += (cardinality / total_freq)

            # Convert to format: [lower, upper, cumulative_freq, num_distinct]
            # bucket[3] must be num_distinct (not row count) because
            # histogram_to_case() uses it to control how many synthetic values to generate
            histogram["buckets"].append([
                float(range_min),
                float(range_max),
                round(cumulative_freq, 5),
                int(unique_count) if unique_count else 1
            ])

        return histogram
