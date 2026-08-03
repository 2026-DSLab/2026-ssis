"""기간 지정 즉석 조회 — "최근 N일 이내에 뭐가 바뀌었나"를 그 자리에서
실제 감지 파이프라인을 돌려 보여준다.

run_weekly.py와 로직은 같다(watchlist 전체 순회 → process_entry() →
build_contract() → 필요하면 LLM 요약). 다른 점은 실행 주체(작업
스케줄러가 아니라 웹 요청)와 대상 기간(고정 7일이 아니라 프리셋
5일/2주/1개월)뿐이다.

★ 비용/시간: watchlist 전체(현재 102건)에 실제 API를 순차 호출한다.
대부분은 이미 최신 버전이 저장돼 있어 즉시 "변경없음"으로 끝나지만
(VersionRepo.law_exists가 이미 True라 API 호출 자체는 피할 수 없다 —
"바뀌었는지"를 확인하려면 매번 최신 버전을 조회해야 한다), 개정이 실제로
발견된 항목만 LLM 요약까지 추가로 돈다. 그래서 요청마다 걸리는 시간이
다르다 — 새 개정이 없으면 API 호출 시간만(수십 초), 있으면 + LLM 요약
시간이 더해진다("대기가 있더라도 괜찮다"는 사용자 결정을 전제로 한다).

★ 캐싱: 같은 기간(window_key)을 짧은 시간 안에 다시 요청하면(새로고침 등)
매번 API 102번 + LLM을 다시 부르지 않도록 TTL 캐시를 둔다.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from lawtrack.api.client import LawApiClient, LawApiError
from lawtrack.config import PROJECT_ROOT, Settings
from lawtrack.contract.export import build_contract
from lawtrack.contract.schema import WeeklyContract
from lawtrack.db.conn import Database
from lawtrack.db.repo import (
    ArticleDiffRepo,
    ChangeLogRepo,
    LawSummaryRepo,
    VersionRepo,
    WatchlistRepo,
)
from lawtrack.detect import DetectStatus, process_entry

log = logging.getLogger(__name__)

#: 프리셋 기간 — 쿼리파라미터 값 → 오늘 기준 며칠 전부터인지.
#: 자유 날짜 선택 대신 프리셋으로 고정한 이유(2026-08-03, 사용자 결정):
#: 요청마다 실 API+LLM 비용이 드는 기능이라, 기간을 몇 개로 한정해야
#: 캐시가 의미 있게 재사용된다(자유 날짜면 사람마다 범위가 미묘하게
#: 달라 캐시 적중률이 거의 0에 수렴한다).
PERIOD_WINDOWS: dict[str, int] = {"5d": 5, "2w": 14, "1m": 30}

#: 같은 기간을 반복 요청할 때 재사용할 최대 시간(초).
CACHE_TTL_SECONDS = 15 * 60


@dataclass
class PeriodResult:
    window_key: str
    from_date: date
    to_date: date
    laws: list[dict]
    hwpx_path: Path | None
    computed_at: float
    newly_detected: int
    errors: list[str] = field(default_factory=list)


_cache: dict[str, PeriodResult] = {}

#: 지금 이 순간 각 기간이 어느 단계인지("국가법령정보 API 확인 중 42/102건"
#: 등). 웹페이지 로딩 화면이 폴링해서 보여준다 — 사용자 요청(2026-08-03):
#: "어떤 단계인지를 보여주고 싶어".
_progress: dict[str, dict] = {}
_progress_lock = threading.Lock()

#: 같은 기간에 대해 스윕이 이미 백그라운드로 돌고 있으면 새로 또 시작하지
#: 않기 위한 추적 — 여러 방문자가 동시에 같은 기간을 눌러도 실 API 스윕은
#: 한 번만 돈다.
_running_sweeps: dict[str, threading.Thread] = {}


def _set_progress(window_key: str, stage: str, detail: str = "") -> None:
    with _progress_lock:
        _progress[window_key] = {"stage": stage, "detail": detail}


def get_progress(window_key: str) -> dict:
    """웹 라우트(/period-status)가 그대로 JSON으로 내려주는 현재 단계."""
    with _progress_lock:
        return dict(_progress.get(window_key, {"stage": "", "detail": ""}))


def is_cache_fresh(window_key: str) -> bool:
    cached = _cache.get(window_key)
    return cached is not None and (time.time() - cached.computed_at) < CACHE_TTL_SECONDS


def get_period_result(settings: Settings, window_key: str, *, force: bool = False) -> PeriodResult:
    """캐시에 있고 안 만료됐으면 재사용, 아니면 실제로 감지 스윕을 돈다.

    (동기 버전 — 백그라운드 미리캐싱 루프가 쓴다. 웹 요청 경로는 폴링
    가능한 ensure_sweep_started()/get_progress()를 대신 쓴다.)
    """
    if window_key not in PERIOD_WINDOWS:
        raise ValueError(f"알 수 없는 기간 키: {window_key!r} (허용: {list(PERIOD_WINDOWS)})")

    if not force and is_cache_fresh(window_key):
        return _cache[window_key]

    result = _run_live_sweep(settings, window_key)
    _cache[window_key] = result
    return result


def ensure_sweep_started(settings: Settings, window_key: str) -> bool:
    """웹 요청이 부르는 논블로킹 진입점.

    캐시가 신선하면 아무것도 안 하고 False(= 그냥 바로 결과 페이지로
    가도 됨)를 돌려준다. 아니면 백그라운드 스레드로 실 스윕을 시작하고
    True(= /period-status를 폴링하며 기다려야 함)를 돌려준다. 이미 같은
    기간의 스윕이 돌고 있으면 새로 시작하지 않고 그냥 True만 돌려준다
    (동시 방문자 여러 명이 같은 기간을 눌러도 실 API 스윕은 한 번만).
    """
    if window_key not in PERIOD_WINDOWS:
        raise ValueError(f"알 수 없는 기간 키: {window_key!r} (허용: {list(PERIOD_WINDOWS)})")

    if is_cache_fresh(window_key):
        return False

    with _progress_lock:
        existing = _running_sweeps.get(window_key)
        if existing is not None and existing.is_alive():
            return True

        def _work() -> None:
            try:
                result = _run_live_sweep(settings, window_key)
                _cache[window_key] = result
            except Exception:  # noqa: BLE001 — 폴링 쪽에 "실패" 상태를 남기고 스레드는 조용히 끝낸다
                log.exception("기간조회 백그라운드 스윕 실패: window=%s", window_key)
                _set_progress(window_key, "오류", "다시 시도해 주세요")

        thread = threading.Thread(target=_work, name=f"lawtrack-sweep-{window_key}", daemon=True)
        _running_sweeps[window_key] = thread
        thread.start()
    return True


def clear_cache() -> None:
    """테스트/강제 재계산용."""
    _cache.clear()
    with _progress_lock:
        _progress.clear()
        _running_sweeps.clear()


#: 감지 스윕 동시 실행 수. 순차 호출은 102건 × 왕복시간(약 0.3~0.9초)이
#: 그대로 더해져 90초~4분씩 걸렸다 — 캐싱/LLM 스킵은 "얼마나 자주/헛되이
#: 도느냐"만 줄일 뿐 이 스윕 자체의 소요시간은 못 줄인다.
#: ★ 실측(2026-08-03, 실 국가법령정보 API 대상): 5=17.5초, 10=10.2초,
#: 20=5.8초, 30=4.4초 — 전 구간 에러 0건. 20을 넘어가면 수익이 급격히
#: 줄어든다(가장 느린 개별 요청 하나가 병목이 되는 지점) — 정부 서버에
#: 필요 이상의 동시 요청을 보내지 않으면서도 체감 속도(90초→6초대,
#: 약 15배)를 얻을 수 있는 지점으로 20을 골랐다(사용자 결정).
SWEEP_CONCURRENCY = 20


def _process_one_entry(
    settings: Settings, version_repo: VersionRepo, watchlist_repo: WatchlistRepo,
    change_log_repo: ChangeLogRepo, article_diff_repo: ArticleDiffRepo, entry,
):
    """스레드 풀 워커 하나가 실행하는 단위 작업. 엔트리마다 독립된
    LawApiClient를 쓰므로(공유 안 함) 스레드 간 상태 충돌이 없다."""
    client = LawApiClient(settings.api)
    try:
        outcome = process_entry(
            client, version_repo, watchlist_repo, change_log_repo, article_diff_repo, entry,
        )
        return entry, outcome, None
    except LawApiError as exc:
        return entry, None, exc
    except Exception as exc:  # noqa: BLE001 — 한 건 실패를 호출부에 결과로 돌려줄 뿐, 여기서 죽지 않는다
        log.exception("기간조회 처리 중 예외: law_id=%s", entry.law_id)
        return entry, None, exc
    finally:
        client.close()


def _run_live_sweep(settings: Settings, window_key: str) -> PeriodResult:
    import concurrent.futures

    days = PERIOD_WINDOWS[window_key]
    to_date = date.today()
    from_date = to_date - timedelta(days=days)

    # pool_size 기본값(5)만으로는 동시 워커 수와 딱 맞아떨어져 여유가
    # 없다 — 커넥션을 오래 붙들고 있는 경우를 대비해 조금 더 넉넉히 둔다.
    db = Database(settings.db, pool_size=max(SWEEP_CONCURRENCY * 2, 5))
    watchlist_repo = WatchlistRepo(db)
    version_repo = VersionRepo(db)
    change_log_repo = ChangeLogRepo(db)
    article_diff_repo = ArticleDiffRepo(db)
    law_summary_repo = LawSummaryRepo(db)

    entries = watchlist_repo.active()
    newly_detected = 0
    errors: list[str] = []
    total = len(entries)

    _set_progress(window_key, "국가법령정보 API 확인 중", f"0/{total}건")
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=SWEEP_CONCURRENCY) as executor:
        futures = [
            executor.submit(
                _process_one_entry, settings, version_repo, watchlist_repo,
                change_log_repo, article_diff_repo, entry,
            )
            for entry in entries
        ]
        for future in concurrent.futures.as_completed(futures):
            entry, outcome, exc = future.result()
            completed += 1
            if exc is not None:
                log.warning("기간조회 처리 중 예외: law_id=%s %r", entry.law_id, exc)
                errors.append(f"{entry.official_name}({entry.law_id}): {exc}")
            elif outcome.detect.status is DetectStatus.CHANGED:
                newly_detected += 1
            _set_progress(window_key, "국가법령정보 API 확인 중", f"{completed}/{total}건")

    _set_progress(window_key, "변경 내용 정리 중")
    contract = build_contract(
        watchlist_repo, article_diff_repo, change_log_repo,
        from_date=from_date, to_date=to_date,
    )

    # ★★★★ 설계(2026-08-03, 사용자 지적): "이 기간에 걸리는 개정"은
    # 기간이 넓을수록(1개월 창) 여러 번의 재조회에 걸쳐 계속 같은 걸로
    # 다시 잡힌다 — 5분/10분마다 미리 캐싱을 돌리면 이미 요약해 둔 같은
    # 개정 건을 계속 다시 LLM에 넘기게 된다(30일 창이면 한 건이 최대
    # 30일 동안 매번 재요약될 수 있음 — 완전히 낭비). law_summary에
    # (law_id, new_serial_no)가 이미 있으면 그 요약은 "완결된 사실"이라
    # 다시 부를 이유가 없다 — 진짜 처음 보는 것만 골라 LLM을 부른다.
    unsummarized_keys = {
        (law.law_id, law.new_serial_no)
        for group in contract.amendment_groups
        for law in group.laws
        if law_summary_repo.fetch(law.law_id, law.new_serial_no) is None
    }

    if unsummarized_keys:
        from summarizer.config import load_settings as load_summary_settings
        from summarizer.llm import build_client
        from summarizer.pipeline import SummaryPipeline
        from summarizer.sinks import DbSink, HwpxSink

        _set_progress(window_key, "AI 요약 작성 중", f"{len(unsummarized_keys)}건")
        to_summarize = _filter_contract(contract, unsummarized_keys)

        # ★★ 실측 발견(2026-08-03): write_contract()는 파일명을 항상
        # "weekly_contract_{batch_date}.json"로 고정한다. 기간(window_key)이
        # 달라도 batch_date(오늘 날짜)는 같으므로, 같은 날 "5일"과 "2주"가
        # 둘 다 새 개정을 찾으면 서로 같은 파일명을 놓고 경쟁해 나중 것이
        # 먼저 것을 덮어쓴다 — law_summary.source_file은 "5일" 결과를
        # 가리키는데 실제 파일 내용은 "2주" 것으로 바뀌는 불일치가 생긴다.
        # 파일명에 window_key를 넣어 완전히 분리한다(사용자 요청: "보고서도
        # 최근5일/최근2주가 각각 다르게 나와야 할 것 같고").
        contract_dir = PROJECT_ROOT / "out" / "live" / window_key
        contract_dir.mkdir(parents=True, exist_ok=True)
        contract_path = contract_dir / f"weekly_contract_{to_date.isoformat()}_{window_key}.json"
        contract_path.write_text(
            to_summarize.model_dump_json(indent=2, exclude_none=False), encoding="utf-8",
        )

        summary_settings = load_summary_settings()
        summary_client = build_client(summary_settings.llm)
        summary_results = SummaryPipeline(summary_client, summary_settings).run([contract_path])

        DbSink(
            db, llm_provider=summary_settings.llm.provider, llm_model=summary_settings.llm.model,
        ).write(summary_results)

        # ★★★★★ 실측 발견(2026-08-03, 사용자 리포트 — "이번 주"에는
        # 공공기관 DB표준화 지침만 뜨는데 "최근 5일/2주"엔 아무것도 없고
        # "최근 1개월"엔 완전히 다른 법이 뜨는 게 말이 안 된다는 지적):
        # DbSink.write()는 batch_date를 오늘 날짜로 써버린다. "이번 주"
        # 탭(_repo.latest_batch_date())은 batch_date가 가장 큰(=가장 최근)
        # 행을 진짜 주간 배치로 착각해서 그것만 보여주는데, 즉석 조회가
        # (실제 시행일과 무관하게) 오늘 날짜로 계속 덮어쓰면 "이번 주"가
        # 엉뚱한 1건짜리 배치로 오염된다 — 실측: 시행일 2025-02-24인
        # 공공기관 DB표준화 지침이 오늘 재조회됐다는 이유만으로 "이번 주"
        # 배치 전체(원래 68건)를 밀어내고 혼자 "이번 주"를 차지해버렸다.
        # batch_date는 "run_weekly.py --full이 만든 진짜 주간 배치"만의
        # 것이어야 한다 — 즉석 조회 결과는 batch_date를 NULL로 둬서
        # latest_batch_date()/이번 주 탭이 절대 이걸 주워가지 않게 한다
        # (기간별 화면은 batch_date가 아니라 created_at으로 걸러서
        # 보여주므로 NULL이어도 전혀 문제 없다 — created_at은 INSERT
        # 시점에 자동으로 찍히지 이 UPDATE로 건드리는 값이 아니다).
        with db.transaction() as (_, cur):
            for law_id, new_serial_no in unsummarized_keys:
                cur.execute(
                    "UPDATE law_summary SET batch_date = NULL WHERE law_id=%s AND new_serial_no=%s",
                    (law_id, new_serial_no),
                )

        # ★ 실측 발견(2026-08-03, 사용자 리포트): law_summary.source_file이
        # 가리키는 "weekly_contract_....json"과 짝이 되는 .hwpx가 out/reports/
        # (표준 위치 — "이번 주" 배치 모드 다운로드가 보는 자리)에 실제로는
        # 한 번도 안 만들어져서, "이번 주" 탭에서 이 개정이 요약되어 보여도
        # HWPX 다운로드는 항상 404였다. 여기서 표준 위치에도 만들어 둔다
        # (LLM 재호출 없음 — 방금 만든 summary_results를 그대로 문서화만 함).
        HwpxSink(PROJECT_ROOT / "out" / "reports").write(summary_results)

    laws = law_summary_repo.fetch_by_period(from_date, to_date)

    # HWPX는 항상 law_summary에서 다시 읽어(이미 요약된 것 + 방금 요약한
    # 것 전부 포함) 새로 만든다 — LLM 호출 없이 순수 DB→파일 변환이라
    # 반복 실행해도 비용이 안 든다. 그래서 웹페이지(fetch_by_period)와
    # 다운로드 파일의 내용이 항상 정확히 같다.
    hwpx_path = None
    if laws:
        from summarizer.sinks import HwpxSink

        _set_progress(window_key, "보고서 작성 중")
        report_dir = PROJECT_ROOT / "out" / "reports" / "live" / window_key
        bundle_summary = _bundle_from_rows(laws, window_key=window_key, to_date=to_date)
        HwpxSink(report_dir).write([bundle_summary])
        hwpx_path = report_dir / f"{Path(bundle_summary.source_file).stem}.hwpx"

    db.close()
    _set_progress(window_key, "완료")

    return PeriodResult(
        window_key=window_key, from_date=from_date, to_date=to_date, laws=laws,
        hwpx_path=hwpx_path, computed_at=time.time(), newly_detected=newly_detected,
        errors=errors,
    )


def _filter_contract(contract: WeeklyContract, keys: set[tuple[str, str]]) -> WeeklyContract:
    """LLM 요약 대상을 unsummarized_keys로만 좁힌 계약 사본을 만든다.

    unresolved/no_comparison은 애초에 LLM이 손대지 않는 항목이라 그대로
    들고 간다 — 걸러야 할 건 amendment_groups(=LLM이 실제로 부를 대상)뿐.
    """
    filtered_groups = []
    for group in contract.amendment_groups:
        laws = [law for law in group.laws if (law.law_id, law.new_serial_no) in keys]
        if laws:
            filtered_groups.append(
                group.model_copy(update={"laws": laws, "affected_law_ids": [law.law_id for law in laws]})
            )
    return contract.model_copy(update={"amendment_groups": filtered_groups})


def _row_to_law_summary(row: dict):
    """law_summary 테이블 행(dict) → summarizer.models.LawSummary.

    DbSink가 쓰는 방향(LawSummary → asdict → JSON → DB)의 정반대다.
    HWPX 재구성은 이미 완결된 요약을 다시 파일로 옮기는 것뿐이라 LLM을
    부르지 않는다 — 이 함수가 그 "다시 조립"을 담당한다.
    """
    from summarizer.models import (
        ArticleMapping,
        ArticleSummary,
        ArticleUnit,
        LawSummary,
        PositionMapping,
        VerifierIssue,
    )

    article_summaries = [
        ArticleSummary(
            unit=ArticleUnit(**a["unit"]),
            summary=a.get("summary", ""),
            caveats=a.get("caveats") or [],
            error=a.get("error"),
        )
        for a in (row.get("article_summaries") or [])
    ]
    mappings = [
        ArticleMapping(
            article_label=m["article_label"],
            mappings=[PositionMapping(**p) for p in m.get("mappings", [])],
            needs_review=m.get("needs_review", False),
            error=m.get("error"),
        )
        for m in (row.get("mappings") or [])
    ]
    verifier_issues = [VerifierIssue(**v) for v in (row.get("verifier_issues") or [])]

    enforce_date = row.get("enforce_date")
    return LawSummary(
        law_id=row["law_id"], law_name=row["law_name"], law_type=row.get("law_type") or "",
        enforce_date=str(enforce_date) if enforce_date else "",
        revision_type=row.get("revision_type") or "", source_url=row.get("source_url") or "",
        new_serial_no=row["new_serial_no"], headline=row.get("headline") or "",
        body=row.get("body") or "", overview=row.get("overview") or "",
        caveats=row.get("caveats") or [], article_summaries=article_summaries,
        mappings=mappings, verifier_issues=verifier_issues, error=row.get("error"),
    )


def _bundle_from_rows(rows: list[dict], *, window_key: str, to_date: date):
    """기간 조회 결과(law_summary 행들)를 HwpxSink가 받는 ContractSummary
    하나로 묶는다. source_file 이름에 window_key를 넣어 배치 산출물
    (weekly_contract_*.hwpx)과 겹치지 않게 한다."""
    from summarizer.models import ContractSummary

    return ContractSummary(
        source_file=f"live_{window_key}_{to_date.isoformat()}.json",
        batch_date=to_date.isoformat(),
        laws=[_row_to_law_summary(row) for row in rows],
    )


#: 백그라운드 사전 캐싱 주기(초). CACHE_TTL_SECONDS(15분)보다 확실히
#: 짧게 잡아, 스윕 자체가 걸리는 시간(최대 몇 분)을 감안해도 캐시가
#: 만료되기 전에 항상 다시 채워지게 한다.
BACKGROUND_REFRESH_INTERVAL_SECONDS = 10 * 60

_background_thread: threading.Thread | None = None


def start_background_refresh(
    settings: Settings, *,
    interval_seconds: float = BACKGROUND_REFRESH_INTERVAL_SECONDS,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    """모든 프리셋 기간을 주기적으로 미리 갱신해 캐시를 항상 따뜻하게
    유지한다 — 방문자가 버튼을 눌렀을 때 실 스윕을 기다리지 않게 한다.

    ★ 비용: LLM은 위에서 이미 "처음 보는 개정만" 부르도록 걸러뒀으므로,
    사람이 안 봐도 계속 도는 이 백그라운드 루프가 LLM 비용을 반복
    발생시키지는 않는다 — 늘어나는 건 국가법령정보 API 존재확인 호출
    (10분마다 102건, 무료)뿐이다.

    ★ daemon=True: 메인 프로세스가 죽으면 이 스레드도 함께 정리된다 —
    실 서버 운영에는 별도 종료 처리가 필요 없다. stop_event는 테스트가
    "한 바퀴만 돌리고 깔끔히 멈추기"용으로 주입하는 것— 실 서버 호출에서는
    안 넘긴다(무한히 돈다).
    """
    global _background_thread
    if _background_thread is not None and _background_thread.is_alive():
        return _background_thread  # 이미 돌고 있으면 중복 시작하지 않는다

    def _loop() -> None:
        while stop_event is None or not stop_event.is_set():
            for window_key in PERIOD_WINDOWS:
                # ★★ 실측 발견(2026-08-03): 이 루프가 그냥 get_period_result를
                # 부르기만 하면 _running_sweeps에 아무 흔적도 안 남아,
                # 방문자가 하필 이 스윕이 도는 중에 /period-check를 부르면
                # ensure_sweep_started()가 "안 도는 중"으로 오판해 같은
                # 기간을 또 스윕한다(실측: 서버 재시작 직후 curl로 재현됨 —
                # 백그라운드 예열이 5d를 도는 도중 /period-check?period=5d를
                # 부르니 중복 스윕이 시작될 뻔했다). 이 루프도 자기 자신을
                # 진행 중인 스레드로 등록해, 두 경로가 같은 dedup 표를 본다.
                current = threading.current_thread()
                with _progress_lock:
                    _running_sweeps[window_key] = current
                try:
                    get_period_result(settings, window_key, force=True)
                except Exception:  # noqa: BLE001 — 한 기간 실패로 전체 루프를 죽이지 않는다
                    log.exception("백그라운드 사전 캐싱 실패: window=%s", window_key)
                finally:
                    with _progress_lock:
                        if _running_sweeps.get(window_key) is current:
                            del _running_sweeps[window_key]
            if stop_event is not None:
                if stop_event.wait(interval_seconds):
                    break
            else:
                time.sleep(interval_seconds)

    thread = threading.Thread(target=_loop, name="lawtrack-period-prewarm", daemon=True)
    thread.start()
    _background_thread = thread
    return thread
