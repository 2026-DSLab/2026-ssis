"""오케스트레이션 — 1단계 팬아웃 → 2단계 취합.

    계약 JSON
       └─ 법령별로
            ├─ 1단계: 조문 N개 → ArticleAgent N회 (병렬)
            └─ 2단계: 조문 요약 N개 → LawAgent 1회
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Sequence

from lawtrack.contract.schema import LawChange

from dataclasses import replace

from summarizer.agents import ArticleAgent, LawAgent, MappingAgent
from summarizer.verifier import verify_summaries
from summarizer.config import Settings
from summarizer.llm import LLMClient
from summarizer.loader import (
    apply_mappings,
    build_article_units,
    iter_laws,
    load_contract,
)
from summarizer.models import ArticleSummary, ContractSummary, LawSummary

log = logging.getLogger(__name__)


class SummaryPipeline:
    def __init__(self, client: LLMClient, settings: Settings):
        self._settings = settings
        self._mapping_agent = MappingAgent(client, settings.llm)
        self._article_agent = ArticleAgent(client, settings.llm)
        self._law_agent = LawAgent(client, settings.llm)
        # 검증은 LLM 이 아니라 코드(verify_summaries)가 한다.

    def run_law(self, law: LawChange) -> LawSummary:
        """법령 1건 — 매핑 → 1단계 팬아웃 → 2단계 취합."""
        units = build_article_units(law)
        log.info("[%s] %s — 조문 %d건", law.law_id, law.law_name, len(units))

        # 0단계 — 요약보다 먼저. 라벨이 틀린 채로 요약하면
        # "④항이 신설되었습니다" 같은 잘못된 문장이 만들어진다.
        mappings = self._mapping_agent.run_law(law)
        for m in mappings:
            if m.shifted or m.needs_review:
                log.info("  매핑: %s%s", m.describe(), " [검토필요]" if m.needs_review else "")

        # ★ 교정을 1단계 '앞'에서 한다. 뒤에서 하면 1단계 요약은 계약의
        #   틀린 라벨대로 만들어지고, 2단계가 서로 모순된 정보를 받는다.
        #   실측(2026-07-21): 그 상태에서 모델이 틀린 쪽을 택해 "①8이
        #   신설되었다"는 잘못된 요약이 나왔다.
        units = apply_mappings(units, mappings)
        for u in units:
            if u.label_corrected:
                log.info("  라벨 교정: %s → %s", u.location_label, u.change_type)

        if not units:
            # 조문 변경이 없어도 2단계는 부른다 — 공식 개정이유만으로도
            # 요약할 내용이 있고, 빈 결과를 내보내는 것보다 낫다.
            summaries: list[ArticleSummary] = []
        else:
            with ThreadPoolExecutor(max_workers=self._settings.pipeline.max_workers) as pool:
                summaries = list(pool.map(self._article_agent.run, units))

        summary = self._law_agent.run(law, summaries, mappings)

        # 마지막 단계 — 감수. 조문 요약을 원문과 '코드로' 대조해 사실 오류만
        # 잡는다. LLM 판단을 쓰지 않으므로 오탐(취향·분량 트집)이 없다.
        #
        # ★★★ 설계(2026-07-31, 사용자 최종 결정): 감수에서 걸린 내용을
        # caveats 문장으로 만들어 내보내던 걸 그만둔다 — caveats_for()를
        # 완전히 제거했던(webapp "확인이 필요한 항목" 박스 삭제) 것과
        # 같은 이유다: 검증 결과가 정답이라 해도, 이걸 "※ 확인 필요"
        # 형태로 사용자에게 노출하면 그 자체가 불안감을 조성한다는 게
        # 이번 세션 내내 반복된 결론이었다. verifier_issues 는 계속
        # 저장한다(DB/내부 점검용 원자료로서의 가치는 남아 있다) — 다만
        # 그걸 다시 caveats 문장으로 바꿔 HWPX/웹페이지에 노출하는
        # 마지막 남은 경로를 여기서 끊는다.
        if self._settings.llm.enable_verifier and not summary.error:
            issues = verify_summaries(summaries)
            if issues:
                summary = replace(summary, verifier_issues=issues)

        return summary

    def run_file(self, path: str | Path) -> ContractSummary:
        """계약 파일 1개 — single 이든 weekly 든 동일하게 처리한다."""
        path = Path(path)
        contract = load_contract(path)
        log.info("%s — %s", path.name, contract.summary())

        laws = iter_laws(contract)
        summaries = [self.run_law(law) for law in laws]

        return ContractSummary(
            source_file=path.name,
            batch_date=contract.batch_date,
            laws=summaries,
            # unresolved / no_comparison 은 요약하지 않고 그대로 통과시킨다.
            # 위치 확정에 실패한 것을 문장으로 만들면 그게 환각이다.
            unresolved=[item.model_dump() for item in contract.unresolved],
            no_comparison=[item.model_dump() for item in contract.no_comparison],
        )

    def run(self, paths: Sequence[str | Path]) -> list[ContractSummary]:
        return [self.run_file(path) for path in paths]
