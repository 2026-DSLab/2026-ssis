"""요약 결과 저장 — 파일(JSON), 보고서(HWPX), DB.

셋 다 같은 Sink 프로토콜을 따르므로 파이프라인은 어디에 저장되는지
모름. 저장처를 늘려도 pipeline.py 는 그대로임.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Protocol, Sequence

from summarizer.models import ContractSummary, LawSummary

log = logging.getLogger(__name__)


class Sink(Protocol):
    def write(self, summaries: Sequence[ContractSummary]) -> None: ...


class JsonSink:
    """계약 파일 하나당 요약 JSON 하나를 씀."""

    def __init__(self, output_dir: Path):
        self._dir = Path(output_dir)

    def write(self, summaries: Sequence[ContractSummary]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        for summary in summaries:
            stem = Path(summary.source_file).stem
            path = self._dir / f"{stem}.summary.json"
            path.write_text(
                json.dumps(summary.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            log.info("저장: %s", path)


class HwpxSink:
    """계약 파일 하나당 HWPX 보고서 하나를 씀.

    생성 후 곧바로 두 가지를 검증함:
      1) 요약 텍스트가 문서에 온전히 들어갔는지(특수문자·긴 문단에서
         누락 없는지) — verify_report, 내용 완전성.
      2) 문서 서식이 깨지지 않았는지(표 너비, 셀 정렬, 빈 문단 비율 등) —
         inspect_document, 구조/레이아웃. 둘 다 사람이 한글로 직접 열어야
         보이는 문제라서 자동화 없이는 매주 놓침. 계획서 4번(HWP 계열
         문서 제공)을 충족함.
    """

    def __init__(self, output_dir: Path):
        self._dir = Path(output_dir)

    def write(self, summaries: Sequence[ContractSummary]) -> None:
        # 무거운 hwpx 임포트는 이 sink 를 쓸 때만.
        from summarizer.report import build_report, inspect_document, verify_report

        self._dir.mkdir(parents=True, exist_ok=True)
        for summary in summaries:
            stem = Path(summary.source_file).stem
            path = self._dir / f"{stem}.hwpx"
            build_report(summary, path)

            missing = verify_report(summary, path)
            if missing:
                log.warning(
                    "HWPX 검증 — %s 에서 텍스트 %d건 누락: %s",
                    path.name, len(missing), "; ".join(m[:30] for m in missing[:5]),
                )

            inspection = inspect_document(path)
            if not inspection.ok:
                log.warning(
                    "HWPX 서식 검사 — %s: %s", path.name,
                    "; ".join(str(f) for f in inspection.errors),
                )
            for w in inspection.warnings:
                log.info("HWPX 서식 경고 — %s: %s", path.name, w)

            if not missing and inspection.ok:
                log.info("보고서: %s (검증 통과, %s)", path, inspection.summary())


class DbSink:
    """요약을 law_summary 테이블에 적재함.

    키는 (law_id, new_serial_no) — "어느 법의 어느 개정분에 대한 요약인가".
    같은 개정분을 다시 요약하면 덮어씀. 판본을 쌓지 않는 이유는
    database/schema.sql 의 law_summary COMMENT 에 적어 두었음.

    한 건이 실패해도 나머지를 계속 넣음. run_weekly.py 가 워치리스트
      한 건의 실패로 배치 전체를 죽이지 않는 것과 같은 이유임 — 10건 중
      1건이 실패했다고 나머지 9건의 요약을 버릴 이유가 없음. 실패는
      로그로 남기고, 몇 건이 들어갔는지 세어 돌려줌.
    """

    def __init__(self, db, *, llm_provider: str = "", llm_model: str = ""):
        # lawtrack.db.conn.Database. 타입을 명시하지 않는 이유는 이 모듈이
        # DB 없이도 import 되어야 하기 때문임(파일 출력만 쓰는 사람도 있음).
        from lawtrack.db.repo import LawSummaryRepo

        self._repo = LawSummaryRepo(db)
        self._provider = llm_provider
        self._model = llm_model

    def write(self, summaries: Sequence[ContractSummary]) -> int:
        saved = 0
        for contract in summaries:
            for law in contract.laws:
                try:
                    self._save(contract, law)
                    saved += 1
                except Exception as exc:  # noqa: BLE001 — 한 건 실패로 전체를 멈추지 않는다
                    log.warning(
                        "요약 DB 적재 실패 — %s(%s): %s", law.law_name, law.law_id, exc
                    )
        log.info("요약 DB 적재: %d건", saved)
        return saved

    def _save(self, contract: ContractSummary, law: LawSummary) -> None:
        self._repo.upsert(
            law_id=law.law_id,
            new_serial_no=law.new_serial_no,
            law_name=law.law_name,
            law_type=law.law_type,
            enforce_date=law.enforce_date,
            revision_type=law.revision_type,
            source_url=law.source_url,
            headline=law.headline,
            overview=law.overview,
            body=law.body,
            caveats=list(law.caveats),
            # 조문 요약·매핑·감수 결과는 원문(old/new)까지 통째로 넣음.
            # 나중에 "이 요약이 왜 이렇게 나왔나"를 추적하려면 그때 본
            # 입력이 남아 있어야 함 — 원문은 API 재조회로 바뀔 수 있음.
            article_summaries=[asdict(s) for s in law.article_summaries],
            mappings=[asdict(m) for m in law.mappings],
            verifier_issues=[asdict(i) for i in law.verifier_issues],
            llm_provider=self._provider,
            llm_model=self._model,
            batch_date=contract.batch_date,
            source_file=contract.source_file,
            error=law.error,
        )
