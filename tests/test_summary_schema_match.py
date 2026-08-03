"""law_summary: DDL(schema.sql)과 INSERT 문(repo.py)이 서로 맞는가.

★ 왜 이 테스트가 따로 필요한가 (2026-07-28):
    law_summary 의 INSERT 는 컬럼 19개를 손으로 나열하고, 값 19개를
    위치로 맞춰 넣는다. 여기서 나는 실수(컬럼 하나 오타, 순서 뒤바뀜,
    자리표시자 개수 불일치)는 파이썬 문법으로는 멀쩡해서 단위 테스트를
    다 통과하고, 실제 PostgreSQL 에 연결되는 순간에야 터진다. 그게 주간
    배치 한밤중이면 아무도 안 보고 있다.

    실 DB 없이도 이건 확인할 수 있다 — DDL 과 SQL 문자열을 둘 다 텍스트로
    읽어 대조하면 된다. 실제 PostgreSQL 연결이 필요한 것은 이 테스트가
    잡을 수 없는 것(권한, 인코딩, 인덱스 길이 한계)뿐이다.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from lawtrack.db import repo as repo_module
from lawtrack.db.repo import LawSummaryRepo

SCHEMA = Path(__file__).resolve().parents[1] / "database" / "schema.sql"


# ---------------------------------------------------------------------------
# DDL / SQL 파싱
# ---------------------------------------------------------------------------

def ddl_columns() -> list[str]:
    """schema.sql 의 law_summary 정의에서 컬럼 이름을 순서대로 뽑는다."""
    sql = SCHEMA.read_text(encoding="utf-8")
    m = re.search(
        r"CREATE TABLE IF NOT EXISTS law_summary\s*\((.*?)\n\);",
        sql,
        re.DOTALL,
    )
    assert m, "schema.sql 에서 law_summary 정의를 찾지 못했습니다."

    columns = []
    for line in m.group(1).splitlines():
        line = line.strip()
        # 컬럼 정의는 '이름 타입 ...' 형태. 제약조건 줄은 건너뛴다.
        if not line or line.upper().startswith(
            ("PRIMARY KEY", "UNIQUE", "KEY ", "INDEX ", "CONSTRAINT", "FOREIGN KEY")
        ):
            continue
        name = line.split()[0]
        if name.isidentifier():
            columns.append(name)
    return columns


def insert_sql() -> str:
    source = inspect.getsource(LawSummaryRepo.upsert)
    m = re.search(r"INSERT INTO law_summary.*?(?=\"\"\",)", source, re.DOTALL)
    assert m, "upsert() 에서 INSERT 문을 찾지 못했습니다."
    return m.group(0)


def insert_columns() -> list[str]:
    m = re.search(r"INSERT INTO law_summary\s*\((.*?)\)\s*VALUES", insert_sql(), re.DOTALL)
    assert m, "INSERT 의 컬럼 목록을 찾지 못했습니다."
    return [c.strip() for c in m.group(1).split(",") if c.strip()]


def placeholder_count() -> int:
    m = re.search(r"VALUES\s*\(([^)]*)\)", insert_sql(), re.DOTALL)
    assert m, "VALUES 자리표시자를 찾지 못했습니다."
    return m.group(1).count("%s")


def updated_columns() -> list[str]:
    """ON CONFLICT ... DO UPDATE SET 절이 갱신하는 컬럼."""
    m = re.search(r"DO UPDATE SET(.*)", insert_sql(), re.DOTALL)
    assert m, "ON CONFLICT ... DO UPDATE SET 절을 찾지 못했습니다."
    return re.findall(r"(\w+)\s*=\s*EXCLUDED\.", m.group(1))


# ---------------------------------------------------------------------------
# 대조
# ---------------------------------------------------------------------------

def test_every_inserted_column_exists_in_ddl():
    """INSERT 가 DDL 에 없는 컬럼을 쓰면 실행 즉시 1054 Unknown column."""
    missing = [c for c in insert_columns() if c not in ddl_columns()]
    assert not missing, f"schema.sql 에 없는 컬럼을 INSERT 합니다: {missing}"


def test_placeholder_count_matches_column_count():
    """개수가 어긋나면 실행 시 'Column count doesn't match value count'."""
    assert placeholder_count() == len(insert_columns())


def test_param_tuple_length_matches_columns(monkeypatch):
    """실제로 넘기는 파라미터 개수까지 확인한다.

    앞의 두 테스트는 SQL 문자열 안쪽만 본다. 정작 틀리기 쉬운 것은
    파이썬 쪽 튜플이라 실행해서 세어 본다.
    """
    captured = {}

    class Cur:
        def execute(self, sql, params=None):
            captured["params"] = params

        def close(self):
            pass

    from contextlib import contextmanager

    class Db:
        @contextmanager
        def transaction(self, *, dictionary: bool = True):
            yield None, Cur()

    LawSummaryRepo(Db()).upsert(law_id="A", new_serial_no="1", law_name="법")

    assert len(captured["params"]) == len(insert_columns())


def test_update_clause_covers_everything_except_the_key():
    """재요약이 덮어써야 할 컬럼을 빠뜨리면, 값이 조용히 옛것으로 남는다.

    키(law_id, new_serial_no)와 DB 가 스스로 채우는 시각 컬럼은 제외한다.
    """
    auto = {"law_id", "new_serial_no", "created_at", "updated_at"}
    expected = [c for c in insert_columns() if c not in auto]
    missing = [c for c in expected if c not in updated_columns()]

    assert not missing, (
        f"재요약 시 갱신되지 않는 컬럼이 있습니다: {missing} — "
        "옛 요약의 값이 그대로 남습니다."
    )


def test_primary_key_is_law_id_and_serial_no():
    """키가 바뀌면 '같은 개정분은 덮어쓴다'는 설계 자체가 깨진다."""
    sql = SCHEMA.read_text(encoding="utf-8")
    m = re.search(
        r"CREATE TABLE IF NOT EXISTS law_summary.*?PRIMARY KEY\s*\(([^)]*)\)",
        sql,
        re.DOTALL,
    )
    assert m, "law_summary 의 PRIMARY KEY 를 찾지 못했습니다."

    key = [c.strip() for c in m.group(1).split(",")]
    assert key == ["law_id", "new_serial_no"]


@pytest.mark.parametrize("column", ["caveats", "article_summaries", "mappings", "verifier_issues"])
def test_json_columns_declared_as_json(column):
    """JSONB 타입이어야 조회 시 드라이버가 풀어 준다. TEXT 면 문자열로만 온다."""
    sql = SCHEMA.read_text(encoding="utf-8")
    m = re.search(
        r"CREATE TABLE IF NOT EXISTS law_summary\s*\((.*?)\n\);", sql, re.DOTALL
    )
    body = m.group(1)
    assert re.search(rf"^\s*{column}\s+JSONB\b", body, re.MULTILINE | re.IGNORECASE)


def test_decoder_covers_all_json_columns():
    """_decode_summary_row 가 JSON 컬럼 하나를 빠뜨리면, 그 컬럼만 문자열로
    돌아와 호출부에서 뒤늦게 터진다."""
    sql = SCHEMA.read_text(encoding="utf-8")
    m = re.search(
        r"CREATE TABLE IF NOT EXISTS law_summary\s*\((.*?)\n\);", sql, re.DOTALL
    )
    declared = set(re.findall(r"^\s*(\w+)\s+JSONB\b", m.group(1), re.MULTILINE | re.IGNORECASE))

    assert declared == set(repo_module._SUMMARY_JSON_COLUMNS)
