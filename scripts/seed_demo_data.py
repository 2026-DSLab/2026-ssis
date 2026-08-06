"""시연용 고정 데이터 심기.

database/demo_seed.json 에 미리 골라둔 법 3건(신설/개정/삭제/이동/
이동후개정 5가지 구분이 전부 나오도록 고른 실제 요약 데이터)을 DB에
넣는다. 실행 시점의 날짜로 batch_date/created_at을 새로 찍으므로,
언제 어느 컴퓨터에서 돌려도 "이번주" 탭과 "최근 5일/2주/1개월" 탭에
항상 이 3건이 그대로 나온다 — 실 국가법령정보 API나 LLM을 부르지
않는다(이미 만들어진 요약을 그대로 재사용).

사용법:
    python scripts/seed_demo_data.py

전제 조건: database/schema.sql 이 이미 적용돼 있어야 한다(테이블만
있으면 되고, watchlist는 몰라도 무방 — 웹페이지는 law_summary만 읽는다).
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lawtrack.config import PROJECT_ROOT, load_settings
from lawtrack.db.conn import Database
from lawtrack.db.repo import LawSummaryRepo
from summarizer.models import ArticleSummary, ArticleUnit, ContractSummary, LawSummary
from summarizer.sinks import HwpxSink

SEED_PATH = PROJECT_ROOT / "database" / "demo_seed.json"
DEMO_SOURCE_FILE = "demo_batch.json"


def _load_law_summaries() -> list[LawSummary]:
    """database/demo_seed.json 은 실제 law_summary 행에서 그대로 뽑아 왔으므로
    new_serial_no 가 그 법의 진짜(운영) 개정 일련번호와 같다. 그 값을 그대로
    쓰면 upsert()가 "이미 있는 행 갱신"으로 처리돼 created_at 이 안 바뀐다
    (law_summary.upsert()는 이미 있는 키를 갱신할 때 created_at 을 일부러
    건드리지 않는다 — "처음 발견한 날"이라는 의미를 유지하기 위해서다).
    그 결과 created_at 이 옛날 값 그대로 남아 "최근 5일/2주/1개월" 창을
    벗어나 버린다(실측: 이 스크립트를 실제로 돌려서 확인함 — 이번주 탭엔
    떴지만 기간별 탭엔 3건 중 일부가 안 떴었다).

    운영 데이터의 진짜 개정 번호와 절대 충돌하지 않도록 "-DEMO" 를 붙여
    완전히 별도의 키로 만든다 — 그러면 매번 새 INSERT 라 created_at 이
    "지금"으로 항상 새로 찍힌다.
    """
    data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    laws = []
    for d in data:
        articles = [
            ArticleSummary(
                unit=ArticleUnit(**a["unit"]),
                summary=a["summary"], caveats=a.get("caveats") or [], error=a.get("error"),
            )
            for a in d["article_summaries"]
        ]
        laws.append(LawSummary(
            law_id=d["law_id"], law_name=d["law_name"], law_type=d["law_type"],
            enforce_date=d.get("enforce_date") or "", revision_type=d.get("revision_type") or "",
            source_url=d.get("source_url") or "", new_serial_no=f"{d['new_serial_no']}-DEMO",
            headline=d["headline"], body=d["body"], overview=d.get("overview") or "",
            caveats=d.get("caveats") or [], article_summaries=articles,
        ))
    return laws


def main() -> None:
    settings = load_settings()
    db = Database(settings.db)
    repo = LawSummaryRepo(db)

    laws = _load_law_summaries()
    today = date.today().isoformat()
    contract = ContractSummary(source_file=DEMO_SOURCE_FILE, batch_date=today, laws=laws)

    for law in laws:
        repo.upsert(
            law_id=law.law_id, new_serial_no=law.new_serial_no, law_name=law.law_name,
            law_type=law.law_type, enforce_date=law.enforce_date, revision_type=law.revision_type,
            source_url=law.source_url, headline=law.headline, overview=law.overview, body=law.body,
            caveats=law.caveats, article_summaries=[asdict(a) for a in law.article_summaries],
            batch_date=today, source_file=DEMO_SOURCE_FILE,
        )
    print(f"law_summary에 데모 법 {len(laws)}건 적재 (batch_date={today})")

    hwpx_path = PROJECT_ROOT / "out" / "reports" / f"{Path(DEMO_SOURCE_FILE).stem}.hwpx"
    HwpxSink(hwpx_path.parent).write([contract])
    print(f"이번주 다운로드용 보고서 생성: {hwpx_path}")

    print("완료 — python -m webapp.app 으로 서버를 켜면 이번주 탭에 데모 데이터가 뜬다.")
    print("최근 5일/2주/1개월 탭도 함께 고정하려면 LAWTRACK_DEMO_MODE=1 로 서버를 켜라.")


if __name__ == "__main__":
    main()
