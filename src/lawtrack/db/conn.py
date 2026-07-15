"""MySQL 커넥션 관리.

repo.py 는 이 모듈을 통해서만 DB에 접근한다. 커넥션 풀링과 트랜잭션
경계(commit/rollback)를 여기 한 곳에 모아, 호출부마다 각자 commit/
rollback을 챙기다 빠뜨리는 실수를 막는다.

기존 load_full_text.py 는 매 UPDATE 마다 즉시 connection.commit() 을
호출했는데, 이러면 배치 중간에 실패해도 이미 커밋된 행은 되돌릴 수
없다. 여기서는 "의미있는 작업 단위"를 트랜잭션으로 묶을 수 있게
transaction() 컨텍스트 매니저를 제공한다.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import mysql.connector
from mysql.connector.pooling import MySQLConnectionPool

from lawtrack.config import DbSettings

log = logging.getLogger(__name__)


class Database:
    """MySQL 커넥션 풀 래퍼."""

    def __init__(self, settings: DbSettings, *, pool_size: int = 5):
        self._settings = settings
        try:
            self._pool = MySQLConnectionPool(
                pool_name="lawtrack",
                pool_size=pool_size,
                **settings.as_connect_kwargs(),
            )
        except mysql.connector.Error as exc:
            raise ConnectionError(f"MySQL 연결 풀 생성 실패: {exc}") from exc

    @contextmanager
    def connection(self) -> Iterator["mysql.connector.MySQLConnection"]:
        conn = self._pool.get_connection()
        try:
            yield conn
        finally:
            conn.close()  # 풀에 반환됨 (실제 연결 종료 아님)

    @contextmanager
    def cursor(self, *, dictionary: bool = True) -> Iterator[tuple]:
        """읽기 전용 커서. 자동 commit 하지 않는다 (SELECT 용)."""
        with self.connection() as conn:
            cur = conn.cursor(dictionary=dictionary)
            try:
                yield conn, cur
            finally:
                cur.close()

    @contextmanager
    def transaction(self, *, dictionary: bool = True) -> Iterator[tuple]:
        """쓰기 작업용. 블록이 정상 종료되면 commit, 예외가 나면 rollback.

        사용:
            with db.transaction() as (conn, cur):
                cur.execute("INSERT INTO watchlist …", params)
                cur.execute("INSERT INTO change_log …", params)
            # 여기까지 오면 두 INSERT 가 함께 commit 됨.
            # 둘 중 하나라도 예외가 나면 둘 다 rollback 됨.
        """
        with self.connection() as conn:
            cur = conn.cursor(dictionary=dictionary)
            try:
                yield conn, cur
                conn.commit()
            except Exception:
                conn.rollback()
                log.exception("트랜잭션 실패 — rollback 수행됨")
                raise
            finally:
                cur.close()

    def ping(self) -> bool:
        try:
            with self.connection() as conn:
                conn.ping(reconnect=True, attempts=1, delay=0)
            return True
        except mysql.connector.Error:
            return False

    def close(self) -> None:
        # MySQLConnectionPool 자체를 닫는 공식 API는 없다.
        # 개별 연결은 각 컨텍스트 매니저가 반환 시점에 풀로 돌려놓는다.
        pass