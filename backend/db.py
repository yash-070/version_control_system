import os
from functools import lru_cache

from mysql.connector.pooling import MySQLConnectionPool


@lru_cache(maxsize=1)
def get_pool():
    return MySQLConnectionPool(
        pool_size=int(os.getenv("DB_CONNECTION_LIMIT", "10")),
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "version_control_system"),
        charset="utf8mb4",
        collation="utf8mb4_unicode_ci",
        autocommit=False,
    )
