"""요약 멀티 에이전트 — 세 에이전트를 한 곳에 모은다.

파이프라인 순서:
    MappingAgent   구(舊) 위치가 신(新) 어디로 갔는지 판정 (요약보다 먼저)
    ArticleAgent   개별 조문 요약 (조문당 1회, 병렬)
    LawAgent       법령 단위 종합 요약 (법당 1회)

★ 감수(사실 대조)는 별도 LLM 에이전트가 아니라 코드로 한다
  (summarizer/verifier.py 의 verify_summaries) — 2026-07-25 재설계.
  LLM 에게 "요약이 맞는지 봐줘"라고 시키면 정확한 요약에도 트집을 잡거나
  (분량·표현 문제를 사실 오류로 오인), 자기모순되는 지적을 낸다. 한때
  이 파일에 VerifierAgent(LLM 기반 감수)가 있었지만 실제 파이프라인에서
  쓰이지 않는 죽은 코드였다가 삭제됐다(2026-07-30) — 대체재인 코드 기반
  검증이 이미 pipeline.py 에 연결돼 있었기 때문이다.

각 에이전트는 LLMClient 프로토콜만 보고, 어떤 구현체(OpenAI/QWEN 등)가
꽂혔는지 모른다. 프롬프트는 summarizer/prompts/ 에 따로 둔다 — 가장 자주
고치는 부분이라 로직과 섞으면 위험하기 때문이다.
"""

from __future__ import annotations

import logging
from typing import Sequence

from lawtrack.contract.schema import ArticleDiffItem, LawChange

from summarizer.config import LLMSettings
from summarizer.llm import LLMClient, LLMError
from summarizer.loader import caveats_for
from summarizer.locfmt import format_location
from summarizer.matching import (
    canon_key,
    depth,
    group_by_article,
    is_shift_possible,
    position_label,
    resolve_article,
    trivial_mappings,
)
from summarizer.triage import ChangeClass, triage
from summarizer.models import (
    ArticleMapping,
    ArticleSummary,
    ArticleUnit,
    LawSummary,
    PositionMapping,
)
from summarizer.postprocess import clean_summary
from summarizer.render import build_change_section
from summarizer.prompts import (
    LAW_SUMMARY_SCHEMA,
    MAPPING_SCHEMA,
    build_article_prompt,
    build_law_prompt,
    build_mapping_prompt,
)

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


def _has_batchim(word: str) -> bool:
    """마지막 글자가 받침으로 끝나는가 — 조사(이/가, 으로/로) 선택용."""
    if not word:
        return False
    code = ord(word[-1])
    if 0xAC00 <= code <= 0xD7A3:
        return (code - 0xAC00) % 28 != 0
    return False


_TRAILING_JOSA = ("은", "는", "이", "가", "로", "의", "을", "를")
"""_strip_trailing_josa()가 떼어낼 후보 조사(길이 1). "으로"는 별도로
2글자째 처리한다.

★★ 실측(2026-07-31, 환경개선비용 부담법 제22조 "환경부장관의 권한은" →
"기후에너지환경부장관의 권한은"): 소유격 "의"가 붙은 채로 diff에 뽑히면
"환경부장관의" + "가" = "환경부장관의가"라는 조사 두 개가 겹친 비문이
나왔다. "은는이가로" 뿐 아니라 diff 어절 끝에 올 수 있는 다른 조사도
먼저 떼어내야 한다."""


def _strip_trailing_josa(word: str) -> str:
    """단어 끝에 이미 붙어 있는 조사를 뗀다 — 다시 알맞은 조사를 붙이기 전
    깨끗한 어간을 만들기 위해서다.

    ★★ 실측(2026-07-31, 환경개선비용 부담법 제20조① "환경부장관은" →
    "기후에너지환경부장관은", 국민기초생활보장법 제6조의2① "통계청이" →
    "국가데이터처가"): deleted_words/inserted_words 는 원문 문장 안에서
    diff로 뽑힌 어절이라, 이미 그 자리에 맞는 조사(은/는/이/가)가 붙어
    있는 채로 온다. 그런데 아래 _describe_formal_change()는 항상 새
    조사를 덧붙였다 — "환경부장관은"(이미 은 있음) + 배치침 판정으로 뽑은
    "이" = "환경부장관은이", "통계청이" + "가" = "통계청이가" 처럼 조사가
    중복되는 비문이 나왔다. 조사를 새로 계산하기 전에 기존 조사를 먼저
    떼어내면, 원래 조사가 있었든 없었든 항상 올바른 조사 하나만 남는다.
    """
    if word.endswith("으로") and len(word) > 2:
        return word[:-2]
    if len(word) > 1 and word[-1] in _TRAILING_JOSA:
        return word[:-1]
    return word


def _describe_formal_change(deleted_words: list[str], inserted_words: list[str]) -> str:
    """기관명·인용 법령명 정비를 자연스러운 문장으로 서술한다.

    ★ 화살표("'A' → 'B'") 표기 대신 자연스러운 문장으로 바꿨다
    (2026-07-31, 사용자 요청) — "A가 B로 변경되는 등" 형태가 요약 톤을
    조문 요약(ArticleAgent가 LLM으로 쓰는 문장)과 통일해 준다.
    """
    parts = []
    for d, i in zip(deleted_words, inserted_words):
        d_stem, i_stem = _strip_trailing_josa(d), _strip_trailing_josa(i)
        josa_ga = "이" if _has_batchim(d_stem) else "가"
        josa_ro = "으로" if _has_batchim(i_stem) else "로"
        parts.append(f"{d_stem}{josa_ga} {i_stem}{josa_ro}")
    if not parts:
        return "기관명·인용 법령명이 정비되었습니다."
    return ", ".join(parts) + " 변경되는 등 기관명·인용 법령명이 정비되었습니다."


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
            # ★ 실측(2026-08-03, 사용자 리포트): "②5.에서 제8조②12.로"처럼
            # 원본 표기 그대로 문장에 박아 넣으면 읽기 힘들다는 지적 —
            # 웹페이지/HWPX 위치 칸에 이미 쓰는 항/호/목 표기 규칙(locfmt)을
            # 이 규칙 기반 문장(LLM 호출 없음)에도 그대로 적용한다.
            return ArticleSummary(
                unit=unit,
                summary=(
                    f"내용 변경 없이 {format_location(unit.moved_from)}에서 "
                    f"{format_location(unit.location_label)}(으)로 번호만 이동했습니다."
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

        # ★ 이식(2026-07-30, seongbeen2 브랜치): 형식적 정비(기관명·인용
        # 법령명 일괄 교체 등)는 결정론적으로 확신할 수 있을 때만 LLM을
        # 건너뛴다 — 실측(704건 중 52%)으로 확인된 정부조직개편발 기관명
        # 교체가 전형적인 예다. old_text가 이 위치의 실제 개정 전 문장이
        # 아니라 참고 맥락일 뿐인 경우(구조확장/위치재배치의심,
        # unit.old_text_is_context)는 두 문장이 애초에 같은 위치를
        # 가리키지 않으므로 이 판정 자체를 적용하지 않는다.
        if not unit.old_text_is_context:
            triaged = triage(unit.old_text, unit.new_text, change_type=unit.change_type)
            if triaged.change_class is ChangeClass.NO_CHANGE:
                return ArticleSummary(
                    unit=unit,
                    summary="이 항목은 이번 개정에서 내용이 바뀌지 않았습니다.",
                    caveats=caveats,
                )
            if triaged.change_class is ChangeClass.FORMAL:
                summary = _describe_formal_change(triaged.deleted_words, triaged.inserted_words)
                return ArticleSummary(unit=unit, summary=summary, caveats=caveats)

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


__all__ = ["MappingAgent", "ArticleAgent", "LawAgent"]
