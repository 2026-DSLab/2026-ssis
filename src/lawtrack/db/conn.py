"""PostgreSQL 커넥션 관리.

repo.py 는 이 모듈을 통해서만 DB에 접근함. 커넥션 풀링과 트랜잭션
경계(commit/rollback)를 여기 한 곳에 모아, 호출부마다 각자 commit/
rollback을 챙기다 빠뜨리는 실수를 막음.

UPDATE 마다 즉시 commit 하면 배치 중간에 실패해도 이미 커밋된 행은
되돌릴 수 없음. 여기서는 "의미있는 작업 단위"를 트랜잭션으로 묶을 수 있게
transaction() 컨텍스트 매니저를 제공함.

주의 — psycopg2 커넥션은 기본이 autocommit=False라, SELECT 하나만 실행해도
트랜잭션이 열린 채로 남음. 읽기 전용 경로가 커밋/롤백 없이 커넥션을 풀에
반환하면 그 커넥션은 "idle in transaction" 상태로 남고, 다음 대여자가 그
열린 트랜잭션을 그대로 물려받음. 그래서 cursor() 는 autocommit=True로,
transaction() 은 autocommit=False로 매 대여마다 명시적으로 모드를 맞춤.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from lawtrack.config import DbSettings

log = logging.getLogger(__name__)


class Database:
    """PostgreSQL 커넥션 풀 래퍼."""

    def __init__(self, settings: DbSettings, *, pool_size: int = 5):
        self._settings = settings
        try:
            self._pool = ThreadedConnectionPool(
                1, pool_size, **settings.as_connect_kwargs(),
            )
        except psycopg2.Error as exc:
            raise ConnectionError(f"PostgreSQL 연결 풀 생성 실패: {exc}") from exc

    @contextmanager
    def connection(self) -> Iterator["psycopg2.extensions.connection"]:
        conn = self._pool.getconn()
        try:
            yield conn
        finally:
            self._pool.putconn(conn)  # 풀에 반환됨 (실제 연결 종료 아님)

    @contextmanager
    def cursor(self, *, dictionary: bool = True) -> Iterator[tuple]:
        """읽기 전용 커서. 자동 commit 하지 않음 (SELECT 용)."""
        with self.connection() as conn:
            conn.autocommit = True
            cur_factory = psycopg2.extras.RealDictCursor if dictionary else None
            cur = conn.cursor(cursor_factory=cur_factory)
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
            conn.autocommit = False
            cur_factory = psycopg2.extras.RealDictCursor if dictionary else None
            cur = conn.cursor(cursor_factory=cur_factory)
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
            with self.cursor(dictionary=False) as (_, cur):
                cur.execute("SELECT 1")
            return True
        except psycopg2.Error:
            return False

    def close(self) -> None:
        self._pool.closeall()
