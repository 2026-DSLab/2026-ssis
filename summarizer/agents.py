"""요약 멀티 에이전트 — 네 에이전트를 한 곳에 모은다.

파이프라인 순서:
    MappingAgent   구(舊) 위치가 신(新) 어디로 갔는지 판정 (요약보다 먼저)
    ArticleAgent   개별 조문 요약 (조문당 1회, 병렬)
    LawAgent       법령 단위 종합 요약 (법당 1회)
    VerifierAgent  감수 — 요약이 사실과 맞는지 재검토 (마지막)

각 에이전트는 LLMClient 프로토콜만 보고, 어떤 구현체(OpenAI/QWEN 등)가
꽂혔는지 모른다. 프롬프트는 summarizer/prompts/ 에 따로 둔다 — 가장 자주
고치는 부분이라 로직과 섞으면 위험하기 때문이다.
"""

from __future__ import annotations

import logging
import re
from typing import Sequence

from lawtrack.contract.schema import ArticleDiffItem, LawChange

from summarizer.config import LLMSettings
from summarizer.llm import LLMClient, LLMError
from summarizer.loader import caveats_for
from summarizer.matching import (
    canon_key,
    depth,
    group_by_article,
    is_shift_possible,
    position_label,
    resolve_article,
    trivial_mappings,
)
from summarizer.models import (
    ArticleMapping,
    ArticleSummary,
    ArticleUnit,
    LawSummary,
    PositionMapping,
    VerifierIssue,
)
from summarizer.postprocess import clean_summary
from summarizer.render import build_change_section
from summarizer.prompts import (
    LAW_SUMMARY_SCHEMA,
    MAPPING_SCHEMA,
    VERIFIER_SCHEMA,
    build_article_prompt,
    build_law_prompt,
    build_mapping_prompt,
    build_verifier_prompt,
)
from summarizer.prompts.verifier import source_text_for_check

log = logging.getLogger(__name__)


# ===========================================================================
# 0단계: 매핑 — 구↔신 위치 대응 판정
# ===========================================================================


class MappingAgent:
    """구(舊) 위치가 신(新) 어디로 갔는지 판정.

    2층 구조:
        1층 (계산)  resolve_article() 이 대부분을 확정한다. 번호가 양쪽에
                    있으면 제자리 개정, 사라진 내용이 다른 자리에 보존되면 이동.
        2층 (LLM)   계산으로 못 푼 '풀'(사라진 old / 출처 없는 new)만 대조.
                    실측(2026-07-22): 12개 그룹 중 11개는 1층에서 끝나고,
                    전자정부법 제56조의2 하나만 여기로 온다.
    """

    def __init__(self, client: LLMClient, settings: LLMSettings):
        self._client = client
        self._settings = settings

    def run_law(self, law: LawChange) -> list[ArticleMapping]:
        return [
            self.run_article(law.law_name, label, items)
            for label, items in group_by_article(law).items()
        ]

    def run_article(
        self, law_name: str, article_label: str, items: list[ArticleDiffItem]
    ) -> ArticleMapping:
        # 밀림이 불가능한 조문 — 계약 라벨을 그대로 믿는다. 판정 불필요.
        if not is_shift_possible(items):
            return ArticleMapping(article_label, trivial_mappings(items))

        resolved, pool_old, pool_new = resolve_article(items)

        # 계산으로 다 풀렸으면 LLM 을 부르지 않는다.
        if not pool_old and not pool_new:
            return ArticleMapping(article_label, resolved)

        # 남은 것만 LLM 에 넘긴다.
        llm_maps, needs_review = self._resolve_pool(
            law_name, article_label, pool_old, pool_new
        )
        return ArticleMapping(article_label, resolved + llm_maps, needs_review=needs_review)

    def _resolve_pool(
        self,
        law_name: str,
        article_label: str,
        pool_old: list[ArticleDiffItem],
        pool_new: list[ArticleDiffItem],
    ) -> tuple[list[PositionMapping], bool]:
        """계산으로 못 푼 old/new 를 LLM 으로 대조한다."""
        valid_old = {canon_key(position_label(i)): position_label(i) for i in pool_old}
        valid_new = {canon_key(position_label(i)): position_label(i) for i in pool_new}
        depth_old = {canon_key(position_label(i)): depth(i) for i in pool_old}
        depth_new = {canon_key(position_label(i)): depth(i) for i in pool_new}

        system, user = build_mapping_prompt(law_name, article_label, pool_old, pool_new)
        try:
            payload = self._client.complete_json(
                system=system,
                user=user,
                schema=MAPPING_SCHEMA,
                max_tokens=self._settings.mapping_max_tokens,
                thinking=self._settings.mapping_thinking,
            )
        except LLMError as exc:
            log.warning("[%s] %s 매핑 실패: %s", law_name, article_label, exc)
            # 실패하면 안전하게: old 는 삭제, new 는 신설로 두고 검토 표시.
            fallback = [PositionMapping(position_label(o), None, "삭제", "폴백") for o in pool_old]
            fallback += [PositionMapping(None, position_label(n), "신설", "폴백") for n in pool_new]
            return fallback, True

        maps: list[PositionMapping] = []
        used_old: set[str] = set()
        used_new: set[str] = set()
        for m in payload.get("mappings", []):
            ok = canon_key(m.get("old_position"))
            nk = canon_key(m.get("new_position"))
            rel = m.get("relation", "신설")
            # 계약에 없는 위치를 지어냈으면 버린다.
            if ok and ok not in valid_old:
                ok = ""
            if nk and nk not in valid_new:
                continue
            # 이동인데 출처가 없거나, 이미 쓰인 출처거나, 층위가 다르면 →
            # 논리 모순. 항(①)이 호(①1.)로 이동하는 일은 없다. 신설로 정정.
            if rel in ("이동", "이동후개정") and (
                not ok or ok in used_old or depth_old.get(ok) != depth_new.get(nk)
            ):
                rel = "신설"
                ok = ""
            # 같은 번호끼리 짝지었으면 이동이 아니라 제자리 개정이다.
            elif ok and nk and ok == nk and rel in ("이동", "이동후개정"):
                rel = "개정"
            if nk in used_new:
                continue
            maps.append(
                PositionMapping(
                    old_position=valid_old.get(ok),
                    new_position=valid_new.get(nk),
                    relation=rel,
                    basis="LLM판정",
                    note=m.get("note", ""),
                )
            )
            if ok:
                used_old.add(ok)
            if nk:
                used_new.add(nk)

        # 남은 것 정리. 순서가 중요하다:
        #   ① 대응 못 받은 old 와 new 중 '같은 번호'로 남은 것은 제자리
        #      개정으로 잇는다. LLM 이 층위 위반 이동을 냈다가 정정되면
        #      구①·신① 이 둘 다 미매칭으로 남는데, 이걸 삭제+신설로 흩으면
        #      안 된다 — 통짜 내용이 골격으로 개정된 것이다.
        #      (실측 2026-07-23, 개인정보 보호법 시행령 제60조의2 구①)
        #   ② 그래도 남는 new 는 신설, old 는 삭제.
        for o in pool_old:
            ok = canon_key(position_label(o))
            if ok in used_old:
                continue
            same = [
                n for n in pool_new
                if canon_key(position_label(n)) == ok
                and canon_key(position_label(n)) not in used_new
            ]
            if same:
                np = position_label(same[0])
                maps.append(PositionMapping(position_label(o), np, "개정", "번호일치"))
                used_old.add(ok)
                used_new.add(canon_key(np))

        for n in pool_new:
            if canon_key(position_label(n)) not in used_new:
                maps.append(PositionMapping(None, position_label(n), "신설", "소거법"))
        for o in pool_old:
            if canon_key(position_label(o)) not in used_old:
                maps.append(PositionMapping(position_label(o), None, "삭제", "소거법"))

        return maps, payload.get("confidence") == "low"


# ===========================================================================
# 1단계: 조문 요약
# ===========================================================================


class ArticleAgent:
    """조문 하나를 한두 문장으로 요약한다. 조문끼리 독립이라 병렬 실행된다."""

    def __init__(self, client: LLMClient, settings: LLMSettings):
        self._client = client
        self._settings = settings

    def run(self, unit: ArticleUnit) -> ArticleSummary:
        """조문 하나를 요약한다.

        호출이 실패해도 예외를 위로 던지지 않는다 — 조문 하나가 실패했다고
        법령 전체 요약을 포기할 이유가 없고, 실패를 조용히 빼면 2단계가
        "이게 전부"라고 착각한다. 실패는 error 필드에 담아 전달한다.
        """
        caveats = caveats_for(unit)

        # 번호만 밀렸고 내용이 글자까지 같은 것이 계산으로 확정된 경우.
        # LLM 을 부르지 않는다 — 부를 이유가 없고, 부르면 없던 변경을
        # 지어낼 위험만 생긴다.
        if unit.move_is_identical and unit.moved_from:
            return ArticleSummary(
                unit=unit,
                summary=(
                    f"내용 변경 없이 {unit.moved_from}에서 "
                    f"{unit.location_label}(으)로 번호만 이동했습니다."
                ),
                caveats=caveats,
            )

        # 같은 조문의 다른 항만 바뀌고 이 항은 안 바뀐 경우. LLM 불필요 —
        # 부르면 "통째 교체됐다"고 없던 변경을 지어낸다(2026-07-25 실측).
        if unit.no_change:
            return ArticleSummary(
                unit=unit,
                summary="이 항목은 이번 개정에서 내용이 바뀌지 않았습니다.",
                caveats=caveats,
            )

        system, user = build_article_prompt(unit)
        try:
            summary = self._client.complete_text(
                system=system,
                user=user,
                max_tokens=self._settings.article_max_tokens,
                thinking=self._settings.article_thinking,
            )
        except LLMError as exc:
            log.warning("[%s] %s 요약 실패: %s", unit.law_id, unit.location_label, exc)
            return ArticleSummary(unit=unit, summary="", caveats=caveats, error=str(exc))

        return ArticleSummary(unit=unit, summary=summary, caveats=caveats)


# ===========================================================================
# 2단계: 법령 종합 요약
# ===========================================================================


class LawAgent:
    """법령 1건의 조문별 요약을 종합 요약으로 묶는다."""

    def __init__(self, client: LLMClient, settings: LLMSettings):
        self._client = client
        self._settings = settings

    def run(
        self,
        law: LawChange,
        summaries: Sequence[ArticleSummary],
        mappings: Sequence[ArticleMapping] = (),
    ) -> LawSummary:
        system, user = build_law_prompt(law, summaries, mappings)

        try:
            payload = self._client.complete_json(
                system=system,
                user=user,
                schema=LAW_SUMMARY_SCHEMA,
                max_tokens=self._settings.law_max_tokens,
                thinking=self._settings.law_thinking,
            )
            error = None
        except LLMError as exc:
            log.warning("[%s] %s 종합 요약 실패: %s", law.law_id, law.law_name, exc)
            payload = {"headline": "", "overview": ""}
            error = str(exc)

        # ★ caveats 는 코드로만 만든다. LLM 이 낸 caveats 는 버린다.
        #   실측(2026-07-22, 장애인 고시): 교정 후에도 LLM 이 프롬프트에
        #   남은 흔적을 보고 "신뢰도 경고가 붙어있어 확인이 필요하다"는
        #   문장을 스스로 caveats 에 지어냈다. 경고는 판정 '상태'에서
        #   결정론적으로 나와야지, 모델이 재생산하게 두면 안 된다.
        caveats: list[str] = []
        failed = [s.unit.location_label for s in summaries if s.error]
        if failed:
            caveats.append(f"조문 요약 실패: {', '.join(failed)}")

        # 대응 판정이 불확실한 조문은 담당자가 원문을 봐야 한다.
        review = [m.article_label for m in mappings if m.needs_review]
        if review:
            caveats.append(
                f"조문 구조 변경 판정 확인 필요({', '.join(review)}) — 원문 대조 권장"
            )

        # 후처리: LLM 이 낸 한자 오타 등을 코드로 정리. 병기(과(科))는 보존.
        # LLM 은 headline 과 overview(취지)만 쓴다.
        headline, overview, warns = clean_summary(
            payload.get("headline", ""), payload.get("overview", "")
        )
        if warns:
            log.info("[%s] 후처리 경고: %s", law.law_id, "; ".join(warns))

        # ★ 구조 사실(어느 조가 신설/개정/이동/삭제)은 코드가 붙인다.
        #   LLM 판단에 맡기면 이동을 신설로 뭉뚱그리는 등 확률적으로 틀린다.
        #   매핑(검증 완료)과 조문 요약을 그대로 박으므로 절대 안 틀린다.
        change_section = build_change_section(summaries)
        body = overview.strip()
        if change_section:
            body = (body + "\n\n" + change_section).strip() if body else change_section

        return LawSummary(
            law_id=law.law_id,
            law_name=law.law_name,
            law_type=law.law_type,
            new_serial_no=law.new_serial_no,
            enforce_date=law.enforce_date,
            revision_type=law.revision_type,
            source_url=law.source_url,
            headline=headline,
            body=body,
            overview=overview.strip(),
            caveats=caveats,
            article_summaries=list(summaries),
            mappings=list(mappings),
            error=error,
        )


# ===========================================================================
# 감수: Verifier — 요약이 사실과 맞는지 재검토
# ===========================================================================


class VerifierAgent:
    """조문별 요약을 원문(개정 전/후)과 대조해 '사실 오류'만 잡는다.

    ★ 사실 대조 검증 (2026-07-25 재설계):
        정답이 있는 것만 검증한다 — 요약이 원문 조문과 어긋나는지. 취지·
        표현 같은 주관 판단은 검증하지 않는다. 그래야 검증이 흔들리지 않고,
        지적하면 반드시 맞다(회사가 믿고 쓸 수 있는 조건).

    두 겹 안전장치로 오탐을 막는다:
        1. 자기모순 지적("~라고 썼으나 맞다") 제거
        2. 근거 인용이 실제 원문에 없으면 제거 (지어낸 지적 차단)
    """

    def __init__(self, client: LLMClient, settings: LLMSettings):
        self._client = client
        self._settings = settings

    # 자기모순 지적("~라고 썼으나 그게 맞다")을 거르는 표지.
    _SELF_CONTRADICT = ("맞고", "맞습니다", "맞지만", "올바르", "정확하", "적절하", "문제 없", "언급되지 않")

    # 분량·표현 트집을 거르는 표지 — 이건 사실 오류가 아니다. 요약이 짧고
    # 정확한데도 "덜 다뤘다"고 올리는 걸 막는다(2026-07-25 실측).
    _NOT_FACT_ERROR = (
        "충분히", "자세히", "구체적으로", "상세", "덜 ", "부족",
        "다루지 않", "반영하지 않", "반영되지 않", "다루지 못", "포함되지 않", "언급하지 않",
    )

    # 이 유형만 사실 오류로 인정한다. 스키마가 enum 으로 강제하지만, 코드에서
    # 한 번 더 막는다(모델이 스키마를 어기는 경우 대비).
    _FACT_TYPES = ("환각", "방향오류")

    def run(
        self,
        law: LawChange,
        headline: str,
        overview: str,
        summaries: Sequence[ArticleSummary],
        mappings: Sequence[ArticleMapping],
    ) -> list[VerifierIssue]:
        if not summaries:
            return []
        system, user = build_verifier_prompt(law, headline, overview, summaries, mappings)
        try:
            payload = self._client.complete_json(
                system=system,
                user=user,
                schema=VERIFIER_SCHEMA,
                max_tokens=self._settings.verifier_max_tokens,
                thinking=self._settings.verifier_thinking,
            )
        except LLMError as exc:
            log.warning("[%s] %s 감수 실패: %s", law.law_id, law.law_name, exc)
            return []

        # 검증이 인용한 문장이 실제 조문 원문에 있는지 대조할 기준.
        source_norm = source_text_for_check(law, summaries)

        issues: list[VerifierIssue] = []
        for i in payload.get("issues", []):
            problem = i.get("problem", "")
            quote = i.get("source_quote", "")
            location = i.get("location", "")
            itype = i.get("issue_type", "")

            # ① 유형이 환각/방향오류가 아니면 사실 오류가 아니다 → 제거.
            if itype not in self._FACT_TYPES:
                log.info("[%s] 감수 비사실 유형(%s) 무시: %s", law.law_id, itype, problem[:40])
                continue

            # ② 분량·표현 트집("덜 다뤘다" 류)은 사실 오류가 아니다 → 제거.
            if any(w in problem for w in self._NOT_FACT_ERROR):
                log.info("[%s] 감수 분량트집 무시: %s", law.law_id, problem[:50])
                continue

            # ③ 자기모순 지적 제거.
            if any(w in problem for w in self._SELF_CONTRADICT):
                log.info("[%s] 감수 자기모순 지적 무시: %s", law.law_id, problem[:50])
                continue

            # ④ 근거 인용이 실제 원문에 없으면 지어낸 것 → 제거.
            qn = re.sub(r"\s+", "", quote)
            if not qn or qn not in source_norm:
                log.info("[%s] 감수 근거없는 지적 무시: %s", law.law_id, problem[:50])
                continue

            issues.append(VerifierIssue(severity="high", where=f"{location}({itype})", problem=problem))

        for i in issues:
            log.info("[%s] 감수 사실오류 %s: %s", law.law_id, i.where, i.problem)
        return issues


__all__ = ["MappingAgent", "ArticleAgent", "LawAgent", "VerifierAgent"]
