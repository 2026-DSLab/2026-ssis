"""ContractSummary → HWPX 보고서.

정해진 양식이 없어 표준적인 법령 개정 보고서 구조로 만든다:

    표제 → Ⅰ.개요(집계 표) → Ⅱ.개정 법령 목록(한눈에 보기)
         → Ⅲ.법령별 상세(취지 + 조문별 변경표) → Ⅳ.미확정 → Ⅴ.비교 불가

★ 조문별 변경은 body 문자열을 파싱하지 않고 article_summaries(구조화
  데이터)에서 직접 뽑아 표로 만든다. body 파싱은 형식이 바뀌면 깨지지만,
  구조화 데이터는 안정적이다. overview(취지 문단)는 LawSummary.overview
  에 따로 있으므로 그것을 쓴다.

★ 조문 열은 같은 조문끼리 세로 병합한다. '제60조의2' 가 아홉 줄 반복되는
  대신 한 칸으로 묶이므로, 어느 조문 이야기인지가 눈에 바로 들어온다.
  이를 위해 location_label 을 조문(제60조의2)과 위치(①1.가.)로 쪼갠다.

지면·표 배치(폭·열 너비·칸 여백·머리행)는 layout.py 가 맡는다. 표가
지면 왼쪽으로 쏠리던 문제의 원인과 해법은 그쪽 모듈 설명에 적어 두었다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Sequence

from hwpx import HwpxDocument

from summarizer.models import ArticleSummary, ContractSummary, LawSummary
from summarizer.report.layout import Col, Page, Styles, TableWriter

_ARTICLE = re.compile(r"^제\d+조(?:의\d+)?")

_TAG = {
    "신설": "신설",
    "개정": "개정",
    "삭제": "삭제",
    "이동": "이동",
    "이동후개정": "이동개정",
    "미상": "변경",
}
"""표의 '구분' 칸에 쓸 짧은 이름. 칸 폭이 좁아 4자를 넘기지 않는다."""


def _article_of(location_label: str) -> str:
    m = _ARTICLE.match(location_label)
    return m.group(0) if m else location_label


def _position_of(location_label: str) -> str:
    art = _article_of(location_label)
    return location_label[len(art):] if location_label.startswith(art) else ""


def _text(s: ArticleSummary) -> str:
    if s.error:
        return "[요약 생성 실패 — 원문 확인 필요]"
    return (s.summary or "").strip() or "[내용 없음]"


def _kind_of(s: ArticleSummary) -> str:
    if s.unit.no_change:
        return "변경없음"
    return _TAG.get(s.unit.change_type, s.unit.change_type)


def _content_of(s: ArticleSummary) -> str:
    """변경 내용 칸 — 요약문 + 따로 알아야 할 사항.

    요약에 있는 건 하나도 빼지 않는다(검증이 그 전제로 대조한다). 이동
    전 위치나 신뢰도 경고는 아랫줄에 덧붙인다.
    """
    parts = [_text(s)]
    if s.unit.moved_from:
        parts.append(f"※ 이동 전 위치: {_article_of(s.unit.location_label)}{s.unit.moved_from}")
    parts.extend(f"※ {c}" for c in s.caveats)
    return "\n".join(parts)


class _Report:
    def __init__(self) -> None:
        self.doc = HwpxDocument.new()
        self.page = Page(self.doc)
        self.st = Styles.build(self.doc)
        self.table = TableWriter(self.doc, self.page, self.st)

    # -- 문단 ---------------------------------------------------------------
    def p(self, text: str = "", style: str | None = None, para: str | None = None):
        paragraph = self.doc.add_paragraph(
            text, char_pr_id_ref=style, inherit_style=False
        )
        if para:
            paragraph.para_pr_id_ref = para
        return paragraph

    def gap(self) -> None:
        self.p("", self.st.note)

    # -- 구역 ---------------------------------------------------------------
    def cover(self, contract: ContractSummary) -> None:
        self.p("법령·행정규칙 개정 요약 보고서", self.st.title, self.st.p_center)
        self.p(f"{contract.batch_date} 기준", self.st.subtitle, self.st.p_center)
        self.gap()

    def heading(self, text: str) -> None:
        self.p(text, self.st.section, self.st.p_section)

    def overview_table(self, contract: ContractSummary) -> None:
        articles = sum(len(law.article_summaries) for law in contract.laws)
        cols = [
            Col("배치 기준일", 22, "CENTER"),
            Col("개정 확인", 18, "CENTER"),
            Col("변경 조문", 18, "CENTER"),
            Col("위치 미확정", 21, "CENTER"),
            Col("비교 불가", 21, "CENTER"),
        ]
        row = [
            contract.batch_date,
            f"{len(contract.laws)}건",
            f"{articles}건",
            f"{len(contract.unresolved)}건",
            f"{len(contract.no_comparison)}건",
        ]
        self.table.write(cols, [row])

    def law_index(self, laws: Sequence[LawSummary]) -> None:
        cols = [
            Col("연번", 7, "CENTER"),
            Col("구분", 12, "CENTER"),
            Col("법령명", 41, "LEFT"),
            Col("시행일", 14, "CENTER"),
            Col("개정 구분", 14, "CENTER"),
            Col("변경 조문", 12, "CENTER"),
        ]
        rows = [
            [
                str(i),
                law.law_type or "-",
                law.law_name,
                law.enforce_date or "-",
                law.revision_type or "-",
                f"{len(law.article_summaries)}건",
            ]
            for i, law in enumerate(laws, start=1)
        ]
        self.table.write(cols, rows)

    def law_detail(self, index: int, law: LawSummary) -> None:
        # 시행일·개정 구분·조문 수는 Ⅱ장 목록 표에 이미 있다. 여기서 되풀이하지
        # 않는다 — 상세는 '무엇이 어떻게 바뀌었나'만 다룬다.
        # 첫 법령은 바로 위 장 제목과 붙여 둔다(제목-내용 사이가 벌어지지 않게).
        para = self.st.p_law_first if index == 1 else self.st.p_law
        self.p(f"{index}. [{law.law_type}] {law.law_name}", self.st.law, para)

        if law.headline:
            self.p(f"□ {law.headline}", self.st.headline, self.st.p_keep)
        if law.overview:
            self.p(law.overview, self.st.body, self.st.p_body)

        for caveat in law.caveats:
            self.p(f"※ {caveat}", self.st.note, self.st.p_body)
        for issue in law.verifier_issues:
            if issue.severity == "high":
                self.p(
                    f"⚠ 감수 지적({issue.where}): {issue.problem} — 원문 확인 필요",
                    self.st.warn,
                    self.st.p_body,
                )

        self.article_table(law.article_summaries)

    def article_table(self, summaries: Iterable[ArticleSummary]) -> None:
        rows = [
            [
                _article_of(s.unit.location_label),
                _position_of(s.unit.location_label),
                _kind_of(s),
                _content_of(s),
            ]
            for s in summaries
        ]
        if not rows:
            return
        self.p("○ 조문별 변경 내용", self.st.headline, self.st.p_section)
        cols = [
            Col("조문", 16, "CENTER"),
            Col("위치", 12, "CENTER"),
            Col("구분", 10, "CENTER"),
            Col("변경 내용", 62, "LEFT", vertical="TOP"),
        ]
        self.table.write(cols, rows, merge_first_column=True)

    def passthrough(self, title: str, items: Sequence[dict], detail_key: str) -> None:
        self.heading(title)
        if not items:
            self.p("해당 없음.", self.st.body)
            return
        cols = [
            Col("연번", 7, "CENTER"),
            Col("법령명", 33, "LEFT"),
            Col("사유", 15, "CENTER"),
            Col("비고", 45, "LEFT", vertical="TOP"),
        ]
        rows = [
            [
                str(i),
                it.get("law_name", "(이름 미상)"),
                it.get("reason", "-"),
                str(it.get(detail_key) or it.get("note") or it.get("detail") or ""),
            ]
            for i, it in enumerate(items, start=1)
        ]
        self.table.write(cols, rows)


def build_report(contract: ContractSummary, out_path: str | Path) -> Path:
    """ContractSummary 를 HWPX 로 저장하고 경로를 돌려준다."""
    rpt = _Report()

    rpt.cover(contract)
    rpt.heading("Ⅰ. 개요")
    rpt.overview_table(contract)

    if contract.laws:
        rpt.heading("Ⅱ. 개정 법령 목록")
        rpt.law_index(contract.laws)

        rpt.heading("Ⅲ. 법령별 개정 내용")
        for i, law in enumerate(contract.laws, start=1):
            rpt.law_detail(i, law)

    rpt.passthrough("Ⅳ. 위치 미확정 — 원문 확인 필요", contract.unresolved, "detail")
    rpt.passthrough("Ⅴ. 비교 불가 — 원문 참조", contract.no_comparison, "note")

    rpt.doc.set_page_number(target="footer", align="CENTER")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rpt.doc.save_to_path(str(out_path))
    return out_path
