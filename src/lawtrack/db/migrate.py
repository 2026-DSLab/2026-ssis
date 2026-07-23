"""기존 데이터베이스를 현재 코드가 요구하는 구조로 갱신한다.

``schema.sql``의 ``CREATE TABLE IF NOT EXISTS``는 새 DB 구축에는 충분하지만,
이미 존재하는 테이블에 나중에 추가된 컬럼까지 만들어 주지는 않는다. 이
모듈은 ``information_schema``를 먼저 확인한 뒤 실제로 누락된 항목만 추가해
여러 번 실행해도 안전한 작은 마이그레이션 계층을 제공한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from lawtrack.db.conn import Database


@dataclass(frozen=True)
class RequiredColumn:
    table: str
    column: str
    ddl: str


REQUIRED_COLUMNS = (
    RequiredColumn(
        table="laws",
        column="law_articles_parsed",
        ddl=(
            "ALTER TABLE laws "
            "ADD COLUMN law_articles_parsed JSON NULL "
            "COMMENT 'law_full_text에서 파생한 조/항/호/목 파싱 캐시' "
            "AFTER law_full_text"
        ),
    ),
    RequiredColumn(
        table="administrative_rules",
        column="administrative_rule_articles_parsed",
        ddl=(
            "ALTER TABLE administrative_rules "
            "ADD COLUMN administrative_rule_articles_parsed JSON NULL "
            "COMMENT 'administrative_rule_full_text에서 파생한 위치별 파싱 캐시' "
            "AFTER administrative_rule_full_text"
        ),
    ),
    RequiredColumn(
        table="change_log",
        column="promulgation_date",
        ddl=(
            "ALTER TABLE change_log "
            "ADD COLUMN promulgation_date DATE NULL "
            "COMMENT '법령 공포일 또는 행정규칙 발령일' "
            "AFTER promulgation_no"
        ),
    ),
)


def apply_migrations(db: Database) -> list[str]:
    """누락된 스키마와 확정된 워치리스트 교정값을 반영한다.

    MySQL의 DDL은 암묵적으로 commit되므로 ``Database.transaction`` 대신 한
    연결에서 명시적으로 처리한다. 각 DDL 전에 컬럼 존재 여부를 확인하므로
    일부만 반영된 상태에서 다시 실행해도 나머지만 이어서 적용된다.
    """
    applied: list[str] = []

    with db.connection() as conn:
        cur = conn.cursor(dictionary=True)
        try:
            for required in REQUIRED_COLUMNS:
                cur.execute(
                    "SELECT 1 FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
                    "AND COLUMN_NAME=%s LIMIT 1",
                    (required.table, required.column),
                )
                if cur.fetchone() is None:
                    cur.execute(required.ddl)
                    applied.append(f"{required.table}.{required.column} 추가")

            # law_id는 불변 식별자이므로 이름 검색보다 안전하다. 과거 명칭은
            # internal_name에 그대로 보존하고 API 검색용 official_name만 교정한다.
            cur.execute(
                "UPDATE watchlist SET "
                "official_name=%s, "
                "internal_name=COALESCE(internal_name, %s), "
                "dept_codes=COALESCE(dept_codes, %s) "
                "WHERE law_id=%s AND ("
                "official_name<>%s OR internal_name IS NULL OR dept_codes IS NULL)",
                (
                    "전자정부 웹사이트 품질관리 지침",
                    "전자정부서비스 호환성 준수지침",
                    "행정안전부",
                    "42433",
                    "전자정부 웹사이트 품질관리 지침",
                ),
            )
            if cur.rowcount:
                applied.append("watchlist 42433 공식 명칭 교정")

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    return applied
