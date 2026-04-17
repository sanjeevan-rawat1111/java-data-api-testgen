import pymysql
from pymysql.constants.CLIENT import MULTI_STATEMENTS


class MysqlClient:
    def __init__(self, db_config, logger):
        self.logger = logger
        self.conn = self._connect(db_config)
        self.cursor = self.conn.cursor()

    def _connect(self, db_config):
        self.logger.debug("Connecting to MySQL host=%s db=%s", db_config["host"], db_config["database"])
        return pymysql.connect(
            host=db_config["host"],
            port=int(db_config.get("port", 3306)),
            user=db_config["user"],
            password=db_config["password"],
            database=db_config["database"],
            client_flag=MULTI_STATEMENTS,
            autocommit=False,
            use_unicode=True,
            charset="utf8",
            connect_timeout=30,
        )

    def is_healthy(self):
        try:
            self.conn.ping(reconnect=True)
            return True
        except Exception:
            return False

    def run_query(self, sql):
        self.logger.debug("SQL: %s", sql)
        try:
            self.cursor.execute(sql)
            return True
        except Exception as e:
            self.logger.error("Query failed: %s | SQL: %s", e, sql)
            return False

    def run_select_query(self, sql):
        self.logger.debug("SELECT: %s", sql)
        try:
            cur = self.conn.cursor(pymysql.cursors.DictCursor)
            cur.execute(sql)
            rows = cur.fetchall()
            cur.close()
            return rows
        except Exception as e:
            self.logger.error("Select failed: %s", e)
            return None

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()
