"""데이터 접근 계층 (Repository).

이 파일이 SQL 을 아는 유일한 곳이다. 파이프라인 코드는 SQL 을 직접
쓰지 않고 이 레포지토리들을 통해서만 DB 를 만진다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime

from lawtrack.db.conn import Database
from lawtrack.locate.locator import LocateResult, LocateStatus
from lawtrack.parse.oldnew import ArticleChange, ChangeType

log = logging.getLogger(__name__)

def _parse_yyyymmdd(s: str) -> date | None:
    """'20251001' 형태의 조문시행일자를 date로. 형식이 아니면 None."""
    s = (s or "").strip()
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None



# ---------------------------------------------------------------------------
# 워치리스트
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WatchlistEntry:
    law_id: str
    law_type: str
    official_name: str
    internal_name: str = ""
    previous_names: tuple[str, ...] = ()
    dept_codes: tuple[str, ...] = ()
    status: str = "현행"
    successor_law_id: str | None = None
    scheduled_date: date | None = None
    last_serial_no: str | None = None


class WatchlistRepo:
    def __init__(self, db: Database):
        self._db = db

    def active(self) -> list[WatchlistEntry]:
        """status='현행' 인 감시 대상만. 매주 배치의 조회 대상."""
        with self._db.cursor() as (_, cur):
            cur.execute(
                "SELECT law_id, law_type, official_name, internal_name, "
                "previous_names, dept_codes, status, successor_law_id, "
                "scheduled_date, last_serial_no "
                "FROM watchlist WHERE status = '현행'"
            )
            return [self._to_entry(row) for row in cur.fetchall()]

    def due_for_activation(self, as_of: date | None = None) -> list[WatchlistEntry]:
        """시행전 → 현행 전환 대상. 시행예정일이 도래한 것.

        실측: 장애인지역사회자립법(2027.03.19 시행예정) 같은 케이스가
        시행일에 자동으로 감시 대상에 편입되도록 하는 용도.
        """
        as_of = as_of or date.today()
        with self._db.cursor() as (_, cur):
            cur.execute(
                "SELECT law_id, law_type, official_name, internal_name, "
                "previous_names, dept_codes, status, successor_law_id, "
                "scheduled_date, last_serial_no "
                "FROM watchlist WHERE status = '시행전' AND scheduled_date <= %s",
                (as_of,),
            )
            return [self._to_entry(row) for row in cur.fetchall()]

    def get(self, law_id: str) -> WatchlistEntry | None:
        with self._db.cursor() as (_, cur):
            cur.execute(
                "SELECT law_id, law_type, official_name, internal_name, "
                "previous_names, dept_codes, status, successor_law_id, "
                "scheduled_date, last_serial_no "
                "FROM watchlist WHERE law_id = %s",
                (law_id,),
            )
            row = cur.fetchone()
            return self._to_entry(row) if row else None

    def upsert(self, entry: WatchlistEntry) -> None:
        """최초 등록 또는 정보 갱신(제명변경 반영 등)."""
        with self._db.transaction() as (_, cur):
            cur.execute(
                """
                INSERT INTO watchlist (
                    law_id, law_type, official_name, internal_name,
                    previous_names, dept_codes, status, successor_law_id,
                    scheduled_date, last_serial_no
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    law_type=VALUES(law_type),
                    official_name=VALUES(official_name),
                    internal_name=VALUES(internal_name),
                    previous_names=VALUES(previous_names),
                    dept_codes=VALUES(dept_codes),
                    status=VALUES(status),
                    successor_law_id=VALUES(successor_law_id),
                    scheduled_date=VALUES(scheduled_date)
                """,
                (
                    entry.law_id, entry.law_type, entry.official_name, entry.internal_name,
                    json.dumps(list(entry.previous_names), ensure_ascii=False),
                    ",".join(entry.dept_codes), entry.status, entry.successor_law_id,
                    entry.scheduled_date, entry.last_serial_no,
                ),
            )

    def update_last_seen(self, law_id: str, serial_no: str, *, checked_at: datetime | None = None) -> None:
        """개정 감지 후 last_serial_no 갱신.

        ★ 이걸 빠뜨리면 다음 주에 같은 개정을 또 감지해 중복 보고한다.
        """
        with self._db.transaction() as (_, cur):
            cur.execute(
                "UPDATE watchlist SET last_serial_no=%s, last_checked_at=%s WHERE law_id=%s",
                (serial_no, checked_at or datetime.now(), law_id),
            )

    def mark_status(
        self, law_id: str, status: str, *,
        successor_law_id: str | None = None, scheduled_date: date | None = None,
    ) -> None:
        """0건 3분기(폐지/통합/시행전) 반영."""
        with self._db.transaction() as (_, cur):
            cur.execute(
                "UPDATE watchlist SET status=%s, successor_law_id=%s, scheduled_date=%s WHERE law_id=%s",
                (status, successor_law_id, scheduled_date, law_id),
            )

    @staticmethod
    def _to_entry(row: dict) -> WatchlistEntry:
        prev = row.get("previous_names")
        prev_list = json.loads(prev) if isinstance(prev, str) else (prev or [])
        dept = row.get("dept_codes") or ""
        return WatchlistEntry(
            law_id=row["law_id"],
            law_type=row["law_type"],
            official_name=row["official_name"],
            internal_name=row.get("internal_name") or "",
            previous_names=tuple(prev_list),
            dept_codes=tuple(d for d in dept.split(",") if d),
            status=row["status"],
            successor_law_id=row.get("successor_law_id"),
            scheduled_date=row.get("scheduled_date"),
            last_serial_no=row.get("last_serial_no"),
        )


# ---------------------------------------------------------------------------
# 버전 아카이브 (laws / administrative_rules)
# ---------------------------------------------------------------------------

class VersionRepo:
    """laws / administrative_rules 테이블. PK 존재 여부 = 개정 감지의 핵심."""

    def __init__(self, db: Database):
        self._db = db

    def law_exists(self, law_id: str, serial_no: str) -> bool:
        """SELECT 1 로 존재 여부만 확인 — 이게 곧 '개정 감지' 판정이다."""
        with self._db.cursor() as (_, cur):
            cur.execute(
                "SELECT 1 FROM laws WHERE law_id=%s AND law_serial_no=%s LIMIT 1",
                (law_id, serial_no),
            )
            return cur.fetchone() is not None

    def admrul_exists(self, rule_id: str, serial_no: str) -> bool:
        with self._db.cursor() as (_, cur):
            cur.execute(
                "SELECT 1 FROM administrative_rules "
                "WHERE administrative_rule_id=%s AND administrative_rule_serial_no=%s LIMIT 1",
                (rule_id, serial_no),
            )
            return cur.fetchone() is not None

    def insert_law(self, law_name: str, law_id: str, serial_no: str, full_text: dict) -> None:
        """새 버전 INSERT. 기존 load_full_text.py 의 UPDATE 와 달리, 매주
        배치에서는 행 자체가 없을 수 있으므로 INSERT 를 쓴다."""
        with self._db.transaction() as (_, cur):
            cur.execute(
                "INSERT INTO laws (law_name, law_id, law_serial_no, law_full_text) "
                "VALUES (%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE law_full_text=VALUES(law_full_text)",
                (law_name, law_id, serial_no, json.dumps(full_text, ensure_ascii=False)),
            )

    def insert_admrul(self, rule_name: str, rule_id: str, serial_no: str, full_text: dict) -> None:
        with self._db.transaction() as (_, cur):
            cur.execute(
                "INSERT INTO administrative_rules "
                "(administrative_rule_name, administrative_rule_id, "
                " administrative_rule_serial_no, administrative_rule_full_text) "
                "VALUES (%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE administrative_rule_full_text=VALUES(administrative_rule_full_text)",
                (rule_name, rule_id, serial_no, json.dumps(full_text, ensure_ascii=False)),
            )


# ---------------------------------------------------------------------------
# 개정 이벤트 로그
# ---------------------------------------------------------------------------

class ChangeLogRepo:
    def __init__(self, db: Database):
        self._db = db

    def insert(
        self, *, law_id: str, new_serial_no: str, old_serial_no: str | None = None,
        promulgation_no: str = "", revision_type: str = "", enforce_date: date | None = None,
    ) -> int:
        with self._db.transaction() as (_, cur):
            cur.execute(
                "INSERT INTO change_log "
                "(law_id, old_serial_no, new_serial_no, promulgation_no, revision_type, enforce_date) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (law_id, old_serial_no, new_serial_no, promulgation_no or None,
                 revision_type or None, enforce_date),
            )
            return cur.lastrowid

    def find_by_promulgation(self, promulgation_no: str) -> list[dict]:
        """연쇄개정 매칭 — 같은 공포번호를 가진 다른 감지 이벤트 조회.

        실측: 공포번호 하나로 5개+ 법이 동시 개정되는 경우가 있으므로,
        이 조회 결과가 여러 건이어도 이상하지 않다.
        """
        with self._db.cursor() as (_, cur):
            cur.execute(
                "SELECT law_id, new_serial_no, revision_type, enforce_date, detected_at "
                "FROM change_log WHERE promulgation_no = %s",
                (promulgation_no,),
            )
            return cur.fetchall()


# ---------------------------------------------------------------------------
# 조문 단위 diff (6가드 출력)
# ---------------------------------------------------------------------------

class ArticleDiffRepo:
    def __init__(self, db: Database):
        self._db = db

    def insert_results(
        self, law_id: str, law_serial_no: str,
        results: list[tuple[ArticleChange, list[LocateResult]]],
        *, default_enforce_date: date,
    ) -> int:
        """locate.locate_all() 의 출력을 그대로 저장.

        ArticleChange 에는 조문시행일이 없을 수 있으므로(오는 경로에
        따라 다름), 호출부가 이번 개정의 시행일(default_enforce_date)을
        넘긴다. 조문별로 다른 시행일이 확인되면 그 값을 우선한다 — 이건
        상위 파이프라인이 lsJoHstInf 등으로 보강해 change 객체에
        채워 넣는 것을 전제로 한 확장 지점이다.
        """
        rows = []
        for change, locate_results in results:
            for lr in locate_results:
                rows.append(self._to_row(law_id, law_serial_no, change, lr, default_enforce_date))

        if not rows:
            return 0

        with self._db.transaction() as (_, cur):
            cur.executemany(
                """
                INSERT INTO article_diff (
                    law_id, law_serial_no, article_code, article_label,
                    clause_no, item_label, subitem_label, enforce_date,
                    change_type, old_text, new_text, match_status, match_detail
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    change_type=VALUES(change_type),
                    old_text=VALUES(old_text),
                    new_text=VALUES(new_text),
                    match_status=VALUES(match_status),
                    match_detail=VALUES(match_detail)
                """,
                rows,
            )
            return cur.rowcount

    def fetch_period(self, from_date: date, to_date: date) -> list[dict]:
        """LLM 팀 산출물(contract) 조립 시 사용할 조회.

        기간은 감지일(created_at) 기준이 아니라 시행일(enforce_date)
        기준으로 둘 다 열어 둔다 — 호출부가 목적에 맞게 고른다.
        """
        with self._db.cursor() as (_, cur):
            cur.execute(
                "SELECT * FROM article_diff WHERE enforce_date BETWEEN %s AND %s "
                "ORDER BY law_id, article_code",
                (from_date, to_date),
            )
            return cur.fetchall()

    def fetch_failures(self, *, limit: int = 200) -> list[dict]:
        """0건실패/중복실패만 — 운영 모니터링·가드 튜닝용."""
        with self._db.cursor() as (_, cur):
            cur.execute(
                "SELECT * FROM article_diff WHERE match_status IN ('0건실패','중복실패') "
                "ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            return cur.fetchall()

    @staticmethod
    def _to_row(
        law_id: str, law_serial_no: str, change: ArticleChange,
        lr: LocateResult, default_enforce_date: date,
    ) -> tuple:
        unit = lr.unit
        # ✅ 실측 확인된 "조문시행일자"(unit.enforce_date)를 우선 사용.
        # 조문 단위 시행일이 없으면(빈 문자열 등) 개정 전체 시행일로 보강한다.
        enforce_date = _parse_yyyymmdd(unit.enforce_date) if unit else None
        enforce_date = enforce_date or default_enforce_date
        return (
            law_id,
            law_serial_no,
            unit.article_code if unit else "",
            unit.article_label if unit else "",
            unit.clause_no if unit else "",
            unit.item_label if unit else "",
            unit.subitem_label if unit else "",
            enforce_date,
            change.change_type.value,
            change.old_clean,
            change.new_clean,
            lr.status.value,
            json.dumps(list(lr.tried), ensure_ascii=False),
        )