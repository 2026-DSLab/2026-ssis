"""문서 구조·서식 검사 — 결정론적.

생성된 HWPX 를 다시 열어 눈으로 보기 전에 기계가 먼저 훑음.

--------------------------------------------------------------------------
[이 검사가 실제로 잡았어야 했던 버그 두 개]

    1) 표가 본문 폭의 30% 로 생성되어 왼쪽에 쏠림
       add_table() 기본 너비 14400 HWPUNIT 을 그대로 쓴 탓이었음.
       → check_table_width

    2) 셀 안 글자 사이가 벌어짐
       기본 paraPr 이 horizontal="JUSTIFY" 라 좁은 셀에서 한글이
       공백을 늘려 폭을 맞췄음. "장해구조금   및   중상해구조금의"
       → check_cell_alignment

    둘 다 사람이 한글로 열어보고서야 발견했음. 서식 문제는 LLM 이
    볼 수 없는 영역(XML 속성)이므로 반드시 코드로 검사함.
--------------------------------------------------------------------------

출처: seongbeen2 브랜치(lawtrack.report.inspect)에서 이식.
  _BODY_WIDTH(170mm)는 summarizer/report/layout.py의 실제 여백 설정
  (좌우 각 20mm, A4 210mm 기준)과 일치함을 확인했음 — 이식 시 값을
  바꾸지 않았음.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from pathlib import Path

warnings.filterwarnings("ignore", message=".*manifest.*")
logging.getLogger("hwpx.opc.package").setLevel(logging.ERROR)

from hwpx.document import HwpxDocument  # noqa: E402

__all__ = ["Finding", "InspectionReport", "inspect_document"]

_HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
_HH = "{http://www.hancom.co.kr/hwpml/2011/head}"

# A4 본문 폭. report/layout.py 와 같은 값이어야 함.
_BODY_WIDTH = int(170 / 25.4 * 7200)
_MIN_TABLE_WIDTH = int(_BODY_WIDTH * 0.8)
"""본문 폭의 80% 미만이면 의도치 않게 좁은 표로 봄."""

_LONG_CELL_CHARS = 400
"""이보다 긴 셀은 페이지 넘김에서 깨질 위험이 있어 경고함."""

_HUGE_CELL_CHARS = 2500
"""한 셀이 한 쪽을 넘기면 표가 셀 단위로 나뉘어도 레이아웃이 깨짐."""

_MAX_BLANK_RATIO = 0.45
"""최상위 문단 중 빈 문단 비율 상한.

[임계값을 0.25 로 잡았다가 되돌린 이유]
    절 제목 앞 빈 줄 하나는 정상적인 단락 간격임. 그 구조에서는
    빈 문단 비율이 자연스럽게 35% 안팎이 되며, 0.25 로 잡으면
    정상 문서가 전부 오류로 잡힘.

    사람이 실제로 '공백이 크다'고 느낀 원인은 비율이 아니라
    (1) 연속 빈 줄과 (2) 짧은 절에 걸린 쪽 나눔이었음.
    그 둘은 아래 CONSECUTIVE_BLANK / SPARSE_PAGE 가 따로 잡음.
    이 비율 검사는 그 둘을 빠져나간 이상 상태만 걸러내는 최후 그물임."""

_MAX_CONSECUTIVE_BLANK = 1
"""연속 빈 문단 허용 개수. 2개 이상이면 눈에 띄는 공백이 됨."""

_MIN_CONTENT_AFTER_BREAK = 6
"""쪽 나눔 뒤 이만큼도 내용이 없으면 그 장은 사실상 빈 페이지임."""


@dataclass
class Finding:
    level: str          # "error" | "warn"
    code: str
    message: str

    def __str__(self) -> str:
        mark = "✗" if self.level == "error" else "⚠"
        return f"{mark} [{self.code}] {self.message}"


@dataclass
class InspectionReport:
    findings: list[Finding] = field(default_factory=list)
    tables: int = 0
    paragraphs: int = 0
    chars: int = 0

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (f"표 {self.tables}개 · 문단 {self.paragraphs}개 · {self.chars:,}자 | "
                f"오류 {len(self.errors)} · 경고 {len(self.warnings)}")


def _justify_para_pr_ids(doc: HwpxDocument) -> set[str]:
    """양쪽정렬인 paraPr id 집합."""
    out: set[str] = set()
    try:
        header = doc.package.get_xml(doc.package.HEADER_PATH)
    except Exception:  # noqa: BLE001
        return out
    for pr in header.iter(f"{_HH}paraPr"):
        align = pr.find(f"{_HH}align")
        pid = pr.get("id")
        if align is not None and align.get("horizontal") == "JUSTIFY" and pid:
            out.add(pid)
    return out


def inspect_document(
    path: Path,
    *,
    expect_sections: list[str] | None = None,
    forbidden_texts: list[str] | None = None,
) -> InspectionReport:
    """생성된 HWPX 를 검사함.

    expect_sections   : 본문에 반드시 있어야 할 제목 문자열
    forbidden_texts   : 본문에 절대 나오면 안 되는 문자열
                        (검증 미통과 요약이 본문에 새어든 경우를 잡음)
    """
    rep = InspectionReport()
    doc = HwpxDocument.open(str(path))
    justify_ids = _justify_para_pr_ids(doc)

    try:
        section = doc.package.get_xml("Contents/section0.xml")
    except Exception as exc:  # noqa: BLE001
        rep.findings.append(Finding("error", "OPEN", f"section0.xml 을 열 수 없음: {exc}"))
        return rep

    # 전체 텍스트 수집
    all_text: list[str] = []
    for t in section.iter(f"{_HP}t"):
        if t.text:
            all_text.append(t.text)
    body = "".join(all_text)
    rep.chars = len(body)
    rep.paragraphs = sum(1 for _ in section.iter(f"{_HP}p"))

    # ---------- 표 검사 ----------
    empty_cells = 0
    justified_cells = 0
    long_cells = 0
    huge_cells = 0

    for tbl in section.iter(f"{_HP}tbl"):
        rep.tables += 1
        sz = tbl.find(f"{_HP}sz")
        width = int(sz.get("width", "0")) if sz is not None and sz.get("width", "").isdigit() else 0

        if width and width < _MIN_TABLE_WIDTH:
            rep.findings.append(Finding(
                "error", "TABLE_NARROW",
                f"표 너비 {width} 가 본문 폭({_BODY_WIDTH})의 80% 미만 — "
                f"왼쪽으로 쏠려 보인다. add_table(width=...) 를 확인할 것",
            ))

        for cell in tbl.iter(f"{_HP}tc"):
            cell_text = "".join(t.text or "" for t in cell.iter(f"{_HP}t"))
            if not cell_text.strip():
                empty_cells += 1
            if len(cell_text) > _HUGE_CELL_CHARS:
                huge_cells += 1
            elif len(cell_text) > _LONG_CELL_CHARS:
                long_cells += 1
            for p in cell.iter(f"{_HP}p"):
                if p.get("paraPrIDRef") in justify_ids:
                    justified_cells += 1

    if justified_cells:
        rep.findings.append(Finding(
            "error", "CELL_JUSTIFY",
            f"표 셀 문단 {justified_cells}개가 양쪽정렬 — 좁은 폭에서 "
            f"글자 사이가 벌어진다('장해구조금   및   중상해구조금의'). "
            f"셀 문단은 왼쪽정렬 paraPr 을 써야 한다",
        ))
    if empty_cells:
        rep.findings.append(Finding(
            "warn", "CELL_EMPTY", f"빈 셀 {empty_cells}개"))
    if huge_cells:
        rep.findings.append(Finding(
            "error", "CELL_HUGE",
            f"{_HUGE_CELL_CHARS}자를 넘는 셀 {huge_cells}개 — 한 쪽을 넘겨 "
            f"표가 깨진다. 조문을 나누거나 본문으로 빼야 한다"))
    if long_cells:
        rep.findings.append(Finding(
            "warn", "CELL_LONG",
            f"{_LONG_CELL_CHARS}자를 넘는 셀 {long_cells}개 — "
            f"페이지 경계에서 잘릴 수 있으니 육안 확인 권장"))

    # ---------- 필수 섹션 ----------
    for title in expect_sections or []:
        if title not in body:
            rep.findings.append(Finding(
                "error", "SECTION_MISSING", f"필수 항목 누락: {title!r}"))

    # ---------- 본문 오염 ----------
    for text in forbidden_texts or []:
        snippet = text.strip()[:40]
        if snippet and snippet in body:
            # 부록 Ⅵ 에는 있어야 정상이므로 2회 이상일 때만 문제 삼지 않음.
            # 본문·부록 어디에 있는지까지는 이 계층에서 구분하지 않고 경고만 냄.
            if body.count(snippet) > 1:
                rep.findings.append(Finding(
                    "warn", "UNVERIFIED_ECHO",
                    f"검증 미통과 문구가 문서에 2회 이상 등장: {snippet}…"))

    # ---------- 여백·레이아웃 ----------
    #
    # 사람이 한글로 열어보고서야 "공백이 너무 크다"를 발견하는 일을 막음.
    # 서식 수치는 코드로만 볼 수 있으므로 여기서 반드시 검사함.
    top_paras = section.findall(f"{_HP}p")
    kinds: list[str] = []
    for para in top_paras:
        if para.findall(f".//{_HP}tbl"):
            kinds.append("T")
            continue
        txt = "".join(x.text or "" for x in para.iter(f"{_HP}t"))
        kinds.append("." if txt.strip() else "_")

    n_top = len(kinds)
    n_blank = kinds.count("_")
    if n_top:
        ratio = n_blank / n_top
        if ratio > _MAX_BLANK_RATIO:
            rep.findings.append(Finding(
                "warn", "BLANK_RATIO",
                f"빈 문단이 {n_blank}/{n_top} ({ratio:.0%}) — 상한 "
                f"{_MAX_BLANK_RATIO:.0%}. 문서 중간에 공백이 크게 벌어진다",
            ))

    longest = cur = 0
    for k in kinds:
        cur = cur + 1 if k == "_" else 0
        longest = max(longest, cur)
    if longest > _MAX_CONSECUTIVE_BLANK:
        rep.findings.append(Finding(
            "error", "CONSECUTIVE_BLANK",
            f"빈 문단이 연속 {longest}개 — 허용 {_MAX_CONSECUTIVE_BLANK}개. "
            f"para() 의 빈 줄 억제가 동작하지 않았다",
        ))

    # 쪽 나눔 뒤 내용이 거의 없으면 그 장은 빈 페이지가 됨
    breaks = [i for i, para in enumerate(top_paras)
              if para.get("pageBreak") == "1"]
    for i in breaks:
        after = kinds[i + 1:]
        content = sum(1 for k in after if k in (".", "T"))
        nxt = next((j for j in breaks if j > i), None)
        if nxt is not None:
            content = sum(1 for k in kinds[i + 1:nxt] if k in (".", "T"))
        if content < _MIN_CONTENT_AFTER_BREAK:
            rep.findings.append(Finding(
                "warn", "SPARSE_PAGE",
                f"쪽 나눔 뒤 내용이 {content}줄뿐 — 거의 빈 페이지가 된다. "
                f"짧은 절에는 쪽 나눔을 걸지 말 것",
            ))

    # ---------- 기본 위생 ----------
    if rep.tables == 0:
        rep.findings.append(Finding("error", "NO_TABLE", "표가 하나도 없음"))
    if rep.chars < 200:
        rep.findings.append(Finding(
            "error", "TOO_SHORT",
            f"본문이 {rep.chars}자뿐 — 개정 건이 0건이거나 렌더링에 실패했다"))

    return rep
