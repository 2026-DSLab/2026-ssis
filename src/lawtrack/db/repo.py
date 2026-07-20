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
from lawtrack.text.split import split_to_item_level, strip_annotations, strip_article_head

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

    def insert_law(
        self, law_name: str, law_id: str, serial_no: str, full_text: dict,
        *, parsed_articles: list | None = None,
    ) -> None:
        """새 버전 INSERT. 기존 load_full_text.py 의 UPDATE 와 달리, 매주
        배치에서는 행 자체가 없을 수 있으므로 INSERT 를 쓴다.

        ★ parsed_articles: 요구사항("파싱된 것도 DB에 담아달라")에 따라
        law_full_text(원본, 진실의 원천)와 별도로 조/항/호/목 구조로 파싱한
        결과도 함께 저장한다. full_text는 절대 가공하지 않고 그대로
        보존하는 이유는 오늘 세션에서만 파서 버그를 5건 넘게 찾아 고쳤기
        때문 — 원본이 남아있어야 파서를 고친 뒤 재처리해서 검증할 수
        있다. parsed_articles는 그 원본에서 파생된 "조회 편의용 캐시"일
        뿐이라, None이면 이 컬럼은 갱신하지 않는다(호출부가 굳이 매번
        다시 계산해서 넘길 필요 없게).
        """
        if parsed_articles is None:
            with self._db.transaction() as (_, cur):
                cur.execute(
                    "INSERT INTO laws (law_name, law_id, law_serial_no, law_full_text) "
                    "VALUES (%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE law_full_text=VALUES(law_full_text)",
                    (law_name, law_id, serial_no, json.dumps(full_text, ensure_ascii=False)),
                )
            return
        with self._db.transaction() as (_, cur):
            cur.execute(
                "INSERT INTO laws (law_name, law_id, law_serial_no, law_full_text, law_articles_parsed) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE law_full_text=VALUES(law_full_text), "
                "law_articles_parsed=VALUES(law_articles_parsed)",
                (
                    law_name, law_id, serial_no,
                    json.dumps(full_text, ensure_ascii=False),
                    json.dumps(parsed_articles, ensure_ascii=False),
                ),
            )

    def insert_admrul(
        self, rule_name: str, rule_id: str, serial_no: str, full_text: dict,
        *, parsed_units: list | None = None,
    ) -> None:
        """★ parsed_units: insert_law 의 parsed_articles 와 동일한 취지 —
        administrative_rule_full_text(원본)에서 파생된 위치별 파싱 결과
        캐시. 행정규칙은 원문이 평문이라(parse_admrul_units 참고) 법령처럼
        조/항/호/목 트리가 아니라 "위치+텍스트"의 평평한 목록 형태다."""
        if parsed_units is None:
            with self._db.transaction() as (_, cur):
                cur.execute(
                    "INSERT INTO administrative_rules "
                    "(administrative_rule_name, administrative_rule_id, "
                    " administrative_rule_serial_no, administrative_rule_full_text) "
                    "VALUES (%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE administrative_rule_full_text=VALUES(administrative_rule_full_text)",
                    (rule_name, rule_id, serial_no, json.dumps(full_text, ensure_ascii=False)),
                )
            return
        with self._db.transaction() as (_, cur):
            cur.execute(
                "INSERT INTO administrative_rules "
                "(administrative_rule_name, administrative_rule_id, "
                " administrative_rule_serial_no, administrative_rule_full_text, "
                " administrative_rule_articles_parsed) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE "
                "administrative_rule_full_text=VALUES(administrative_rule_full_text), "
                "administrative_rule_articles_parsed=VALUES(administrative_rule_articles_parsed)",
                (
                    rule_name, rule_id, serial_no,
                    json.dumps(full_text, ensure_ascii=False),
                    json.dumps(parsed_units, ensure_ascii=False),
                ),
            )


# ---------------------------------------------------------------------------
# 개정 이벤트 로그
# ---------------------------------------------------------------------------

class ChangeLogRepo:
    def __init__(self, db: Database):
        self._db = db

    def insert(
        self, *, law_id: str, new_serial_no: str, old_serial_no: str | None = None,
        promulgation_no: str = "", revision_type: str = "", revision_reason: str = "",
        enforce_date: date | None = None, unchanged_clauses: dict[str, list[str]] | None = None,
        comparison_available: bool = True,
    ) -> int:
        with self._db.transaction() as (_, cur):
            cur.execute(
                "INSERT INTO change_log "
                "(law_id, old_serial_no, new_serial_no, promulgation_no, revision_type, "
                " revision_reason, enforce_date, unchanged_clauses, comparison_available) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (law_id, old_serial_no, new_serial_no, promulgation_no or None,
                 revision_type or None, revision_reason or None, enforce_date,
                 json.dumps(unchanged_clauses, ensure_ascii=False) if unchanged_clauses else None,
                 comparison_available),
            )
            return cur.lastrowid

    def fetch_latest_for_serial(self, law_id: str, new_serial_no: str) -> dict | None:
        """법령 1건의 (law_id, new_serial_no)에 대한 가장 최근 change_log 행.

        ★ 실측 발견(2026-07-16, contract/export.py 실데이터 검증): 이 조회가
        없어서 export.py 가 article_diff 행에서 promulgation_no 를 읽으려
        했는데, article_diff 스키마엔 그 컬럼 자체가 없어(law_id,
        law_serial_no, article_code, … 뿐) 항상 빈 문자열이 되고, 그 결과
        연쇄개정 그룹핑(link.py)이 절대 작동하지 않는 버그가 있었다
        (같은 공포번호로 개정된 사회보장기본법/국민기초생활보장법이 각각
        별도 그룹으로 쪼개져 나옴). 같은 (law_id, new_serial_no)에 대해
        재처리로 여러 행이 쌓일 수 있으므로(재실행 시 change_log 는 append-
        only) detected_at 기준 최신 1건만 쓴다.
        """
        with self._db.cursor() as (_, cur):
            cur.execute(
                "SELECT law_id, old_serial_no, new_serial_no, promulgation_no, "
                "revision_type, revision_reason, enforce_date, unchanged_clauses "
                "FROM change_log WHERE law_id=%s AND new_serial_no=%s "
                "ORDER BY detected_at DESC LIMIT 1",
                (law_id, new_serial_no),
            )
            row = cur.fetchone()
            if row and row.get("unchanged_clauses"):
                uc = row["unchanged_clauses"]
                row["unchanged_clauses"] = json.loads(uc) if isinstance(uc, str) else uc
            return row

    def fetch_no_comparison_in_period(self, from_date: date, to_date: date) -> list[dict]:
        """★★ 실측 발견(2026-07-18, contract/export.py 실데이터 검증): 이 조회가
        없어서 build_contract()가 NO_COMPARISON(신구법 대비 불가) 건을 단 하나도
        보고하지 못하는 심각한 버그가 있었다. build_contract()는 article_diff
        (조문 단위 diff)에서 시작해 (law_id, serial_no) 집합을 만드는데, 신구법
        대비 자체가 불가능한 건은 정의상 article_diff 행이 0개라 애초에 그
        집합에 들어가지도 못했다 — NoComparisonItem 스키마 자체는 이미
        존재했지만 실제로 채워질 경로가 원천적으로 없는 죽은 코드였다(제정/
        폐지제정된 법이 매번 산출물에서 통째로 사라짐). article_diff가 아니라
        change_log를 직접 조회해, comparison_available=FALSE인 (law_id,
        new_serial_no) 조합을 기간 내에서 찾는다. change_log는 append-only라
        재처리 시 같은 (law_id, new_serial_no)에 중복 행이 쌓일 수 있으므로
        DISTINCT로 조합만 뽑고, 실제 필드값은 호출부가 fetch_latest_for_serial로
        각각 최신 1건을 다시 조회해 채운다(기존 change_rows_in_period와 동일한
        패턴).
        """
        with self._db.cursor() as (_, cur):
            cur.execute(
                "SELECT DISTINCT law_id, new_serial_no FROM change_log "
                "WHERE comparison_available = FALSE AND enforce_date BETWEEN %s AND %s",
                (from_date, to_date),
            )
            return cur.fetchall()

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

#: ★ 설계(2026-07-20): match_status="위치재배치의심"일 때 old_text 앞에
#: "[※...]" 안내문을 붙이던 로직은 제거했다 — match_status 필드 자체가
#: 이미 "위치재배치의심"이라는 값으로 신뢰도를 명시하므로, old_text에
#: 텍스트를 덧붙이면 DB에 저장되는 old_text가 원문 그대로가 아니게 되는
#: 부작용이 있었다. DB에 저장되는 old_text는 항상 순수 원문이어야 한다.


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
        reshuffled = self._reshuffled_articles(results)
        rows = []
        for change, locate_results in results:
            # ★★ 실측 발견(2026-07-19, 전자정부법 제2조11호 가~바):
            # _reshuffled_articles()는 "같은 조문에 신설(NEWLY_CREATED)이
            # 섞였는가"만 보는데, 이 케이스는 신설이 전혀 없는 순수
            # '개정'인데도 old_text가 여러 새 위치(가.나.다.라.마.바.)에
            # 동일하게 재사용된다(구법엔 목 구조 자체가 없던 통짜 문단이
            # 신법에서 목 6개로 쪼개짐). 그래서 조문 단위 신설 여부와
            # 무관하게, "이 change 하나가 성공적으로 위치확정된 결과가
            # 2개 이상"이면 그 자체로 old_text가 각 행에 정밀 대응하지
            # 않는다는 직접적인 신호다 — 위 휴리스틱보다 더 좁고 정확하다.
            #
            # ★★★ 설계(2026-07-20): 다만 "2개 이상"이 전부 old_text가
            # 진짜로 대응 안 되는 건 아니다 — oldAndNew가 이미 호 단위로
            # 나뉜 구법 문장을 어쩌다 한 change 블록에 같이 담았을 뿐,
            # 구법에도 신법과 똑같은 호 마커가 있어 1:1로 정밀하게 짝지을
            # 수 있는 경우도 있다. 그런 경우까지 뭉뚱그려 구조확장으로
            # 보내면 과잉분류다. _fragment_old_text_by_item()이 "이 change의
            # 성공 위치 전부가 old_clean을 호 단위로 쪼갠 결과와 애매함 없이
            # 1:1로 맞아떨어지는가"를 확인해, 맞으면 위치별 정밀 old_text를
            # 주고(old_text_shared=False, 정상 성공 처리), 하나라도 안 맞으면
            # None을 돌려줘 기존 통짜 재사용 + 구조확장 판정으로 그대로
            # 폴백한다(all-or-nothing — 일부만 정밀 매칭하는 애매한 상태는
            # 만들지 않는다).
            success_count = sum(1 for lr in locate_results if lr.status.value == "성공")
            fragment_old_lookup = None
            if change.change_type is ChangeType.AMENDED and success_count > 1:
                fragment_old_lookup = self._fragment_old_text_by_item(change, locate_results)
            old_text_shared = (
                change.change_type is ChangeType.AMENDED
                and success_count > 1
                and fragment_old_lookup is None
            )
            for frag_idx, lr in enumerate(locate_results):
                rows.append(
                    self._to_row(
                        law_id, law_serial_no, change, lr, default_enforce_date, frag_idx,
                        reshuffled, old_text_shared, fragment_old_lookup,
                    )
                )

        # ★★ 실측 발견(2026-07-16, 행정규칙 재처리 검증): 실패 건의 key가
        # __unresolved__{change.index}_{frag_idx} 처럼 "이번 계산 결과"에
        # 따라 달라질 수 있다(예: 예전엔 실패해서 unresolved 키로 들어갔다가,
        # 파서를 고친 뒤 재처리하면 같은 조각이 성공해서 실제 article_code
        # 키로 들어감). ON DUPLICATE KEY UPDATE는 "키가 같을 때"만 덮어쓰므로,
        # 키 자체가 바뀌면 예전 행은 절대 지워지지 않고 DB에 고아로 영원히
        # 남는다 — 재처리할 때마다 실패 기록이 누적되는 버그였다. 이번 계산
        # 결과가 그 (law_id, law_serial_no)의 유일한 진실이므로, 매번 먼저
        # 완전히 비우고 다시 채운다(같은 트랜잭션 안이라 원자적).
        with self._db.transaction() as (_, cur):
            cur.execute(
                "DELETE FROM article_diff WHERE law_id=%s AND law_serial_no=%s",
                (law_id, law_serial_no),
            )
            if not rows:
                return 0
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
    def _fragment_old_text_by_item(
        change: ArticleChange, locate_results: list[LocateResult],
    ) -> dict[tuple[str, str], str] | None:
        """old_clean을 호 단위까지 분해해(text.split.split_to_item_level),
        이 change의 성공 위치 전부를 (clause_no, item_label) 키로 애매함
        없이 1:1 매칭할 수 있으면 그 매핑을 돌려준다 — 대응이 하나라도
        안 되거나 old쪽에 같은 키가 중복되면(애매함) None을 돌려줘
        호출부가 기존 통짜-재사용 동작으로 폴백하게 한다. all-or-nothing:
        일부 위치만 정밀 매칭되는 상태는 만들지 않는다(그 change의 여러
        위치가 서로 다른 신뢰도의 old_text를 갖게 되는 혼란을 피하기
        위해서다).
        """
        succeeded = [
            lr for lr in locate_results
            if lr.status.value == "성공" and lr.unit is not None
        ]
        if len(succeeded) <= 1:
            return None

        # ★★ 실측 발견(2026-07-20, 전자정부법 제2조11호 가~바 재확인):
        # 신법 쪽 여러 위치(목 가.나.다...)가 (항,호)까지만 놓고 보면 전부
        # 같은 키로 겹칠 수 있다 — 이 경우 old_lookup에 그 키가 "존재"는
        # 하므로(중복 없이 딱 1개) 아래 존재확인만으로는 통과해버려, 결국
        # 목 6개 전부가 같은 old_text를 공유한 채 match_status=성공으로
        # 새어나가는 회귀가 생겼다(구조확장으로 잡혔어야 할 케이스가 오히려
        # 안내 없이 더 조용히 틀리게 됨). 신법 쪽 키들이 서로 겹치지 않고
        # 전부 달라야만("호 단위로 구분 가능") 정밀 매칭을 시도한다.
        new_keys = [(lr.unit.clause_no or "", lr.unit.item_label or "") for lr in succeeded]
        if len(set(new_keys)) != len(new_keys):
            return None

        old_search_text = strip_annotations(strip_article_head(change.old_clean))
        old_lookup: dict[tuple[str, str], str] = {}
        for clause_marker, item_marker, frag in split_to_item_level(old_search_text):
            key = (clause_marker, item_marker)
            if key in old_lookup:
                return None  # old쪽에 같은 (항,호) 키가 중복 — 애매하니 폴백
            old_lookup[key] = strip_annotations(frag.raw).strip()

        for lr in succeeded:
            key = (lr.unit.clause_no or "", lr.unit.item_label or "")
            if key not in old_lookup:
                return None  # 이 위치에 대응하는 old 조각이 없음 — 폴백
        return old_lookup

    @staticmethod
    def _reshuffled_articles(
        results: list[tuple[ArticleChange, list[LocateResult]]],
    ) -> set[str]:
        """★★ 실측 발견(2026-07-16, (계약예규) 정부 입찰ㆍ계약 집행기준
        제34조): 항이 여러 개 신설(NEWLY_CREATED)되어 뒤의 항 번호가
        밀리면, 법제처 신구조문대비표 원본 자체가 "구법 N번째 항"과
        "신법 N번째 항"을 내용이 아니라 순서(위치)로만 짝지어 제공한다
        (구③="기존 지급기한 규정" vs 신③="완전히 새로운 규정" — 같은
        조항이 개정된 게 아니라 그 자리의 내용이 통째로 교체된 것). 실측
        확인: old② "하수급인 선금지급계획 제출"은 의미상 new③에 대응하고
        old⑤는 new⑧에 대응하는 등 밀리는 폭도 조각마다 달라(+1, +2, +3
        등) 일반 규칙으로 재정렬할 수 없다. 그렇다고 "성공"으로 조용히
        내보내면 서로 무관한 구/신 문장을 마치 같은 조항의 전후인 것처럼
        LLM팀에게 확정 사실로 전달하게 된다. 내용 기반 재정렬은 시도하지
        않고(오탐 위험이 더 크다는 사용자 판단), 대신 순수 신설이 섞인
        조문 안의 '개정' 항목은 전부 의심 대상으로 표시만 한다(over-flag
        가 false-confirm 보다 안전하다는 원칙).
        """
        reshuffled: set[str] = set()
        for change, locate_results in results:
            if change.change_type is not ChangeType.NEWLY_CREATED:
                continue
            for lr in locate_results:
                if lr.status.value == "성공" and lr.unit is not None:
                    reshuffled.add(lr.unit.article_label)
        return reshuffled

    @staticmethod
    def _to_row(
        law_id: str, law_serial_no: str, change: ArticleChange,
        lr: LocateResult, default_enforce_date: date, frag_idx: int,
        reshuffled_articles: set[str] = frozenset(),
        old_text_shared: bool = False,
        fragment_old_lookup: dict[tuple[str, str], str] | None = None,
    ) -> tuple:
        unit = lr.unit
        # ✅ 실측 확인된 "조문시행일자"(unit.enforce_date)를 우선 사용.
        # 조문 단위 시행일이 없으면(빈 문자열 등) 개정 전체 시행일로 보강한다.
        enforce_date = _parse_yyyymmdd(unit.enforce_date) if unit else None
        enforce_date = enforce_date or default_enforce_date

        if unit:
            article_code = unit.article_code
            article_label = unit.article_label
            clause_no = unit.clause_no
            item_label = unit.item_label
            subitem_label = unit.subitem_label
        else:
            # ★ 실측 발견(2026-07-16): 위치확정 실패(0건실패/중복실패)나
            # 삭제 스킵(DELETED_SKIP)은 unit이 없어 article_code 등이
            # 전부 빈 문자열이 된다. UNIQUE KEY가 (law_id, law_serial_no,
            # article_code, clause_no, item_label, subitem_label)이므로,
            # 같은 법령·버전 안에서 실패/삭제가 2건 이상이면 전부 같은
            # 빈 키로 수렴해 ON DUPLICATE KEY UPDATE로 서로 덮어써
            # 마지막 1건만 DB에 남는 버그가 있었다(실측: 51건 저장 시도 →
            # 25건만 남음). change.index(원본 <P> 쌍 순번)와 frag_idx(그
            # change 안에서 몇 번째 조각인지)를 묶어 실패/삭제 건마다
            # 고유한 article_code를 부여해 해소한다.
            article_code = f"__unresolved__{change.index}_{frag_idx}"
            article_label = f"(위치미상#{change.index}-{frag_idx})"
            clause_no = ""
            item_label = ""
            subitem_label = ""

        # ★ 실측 발견(2026-07-16, contract/export.py 실데이터 검증): 한 change가
        # 여러 조각(항①②③…)으로 쪼개지면(6가드 가드④), 그동안 모든 조각의
        # new_text 에 change.new_clean(쪼개지기 전 원본 통짜 블록)을 그대로
        # 써왔다 — 그 결과 제56조의2 항①~⑤ 등 서로 다른 위치의 행들이 전부
        # 똑같이 거대한 원문 덩어리를 new_text 로 갖게 되어, "이 위치에서
        # 정확히 무엇이 바뀌었는지"를 각 행만 보고는 알 수 없었다(LLM팀에게
        # "이미 확정된 사실만 준다"는 설계 원칙 위반). new_text 는 실제로
        # 위치가 확정된 조각(lr.fragment)의 텍스트를 쓴다.
        #
        # ★★ 실측 발견(2026-07-16, (계약예규) 예정가격작성기준 제40조② 등):
        # new_text 쪽은 locate/locator.py 가 검색 직전에 이미
        # strip_annotations 를 적용한 텍스트(search_text)로 조각을 만들어서
        # "<img id="...">" 나 "<개정 2014.1.10.>" 같은 각주가 안 섞여
        # 나가지만, old_text 는 이 처리를 거치지 않은 change.old_clean을
        # 그대로 써서 이런 태그가 LLM팀에게 그대로 노출되고 있었다. 같은
        # 함수를 old_text 에도 적용한다. "<신  설>" 처럼 각주 자체가 전체
        # 내용인 경우는 strip 후 공백만 남는데, change_type 필드가 이미
        # "신설"을 명시하므로 정보 손실이 아니다(오히려 "old_text=<신설>"
        # 이라는 내부 마커 텍스트를 그대로 노출하는 것보다 빈 문자열이
        # "개정 전엔 없었다"는 뜻을 더 명확히 전달한다).
        #
        # ★★★ 설계(2026-07-20): old_text도 fragment_old_lookup이 있으면
        # (호 단위까지 구/신이 정밀하게 1:1 대응된 경우) 통짜 change.old_clean
        # 대신 이 위치에 해당하는 호 단위 조각만 쓴다 — insert_results()가
        # 이 change의 모든 성공 위치를 old_clean 분해 결과와 매칭할 수
        # 있을 때만 채워주므로(_fragment_old_text_by_item 참고), 대응이
        # 애매하면 fragment_old_lookup 자체가 None이라 기존 통짜 재사용
        # 동작으로 자동 폴백한다.
        new_text = lr.fragment.raw if lr.fragment else change.new_clean
        fragment_old_text = (
            fragment_old_lookup.get((clause_no, item_label))
            if fragment_old_lookup is not None and unit is not None
            else None
        )
        if fragment_old_text is not None:
            old_text = fragment_old_text
        else:
            old_text = strip_annotations(strip_article_head(change.old_clean)).strip()

        match_status = lr.status.value
        if match_status == "성공" and change.change_type is ChangeType.AMENDED:
            # ★ 실측(2026-07-19, LLM팀 산출물 리뷰): "위치재배치의심" 하나로
            # 두 가지 서로 다른 원인을 뭉뚱그려 표시하면 헷갈린다는 지적.
            #   1) old_text_shared: 구법엔 없던 호/목 구조가 신법에서 새로
            #      생겨(구조확장) old_text가 여러 행에 같은 통짜 문장으로
            #      복제됨 — "재배치"가 아니라 "대응 자체가 없음"이 정확한
            #      원인이라 값 이름을 분리한다.
            #   2) reshuffled_articles: 같은 조문에 항이 신설되며 뒤 항
            #      번호가 밀려, 원본 대비표가 순서만으로 신/구를 잘못
            #      짝지음 — 이건 진짜 "재배치 의심"이 맞는 표현.
            if old_text_shared:
                match_status = "구조확장(구법미분리)"
            elif article_label in reshuffled_articles:
                match_status = "위치재배치의심"

        return (
            law_id,
            law_serial_no,
            article_code,
            article_label,
            clause_no,
            item_label,
            subitem_label,
            enforce_date,
            change.change_type.value,
            old_text,
            new_text,
            match_status,
            json.dumps(list(lr.tried), ensure_ascii=False),
        )