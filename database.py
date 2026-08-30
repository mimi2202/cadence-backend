from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from config import DATABASE_URL

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, kwargs={"row_factory": dict_row})


def q(sql: str, params: tuple = (), *, one: bool = False):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchone() if one else cur.fetchall()


def execute(sql: str, params: tuple = ()) -> int:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount