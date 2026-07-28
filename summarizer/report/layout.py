"""HWPX 지면 배치 — 페이지 규격, 글꼴 계층, '지면을 꽉 채우는' 표.

★ 이 모듈이 존재하는 이유 — 표가 왼쪽으로 쏠리던 원인:

    python-hwpx 의 ``add_table(rows, cols)`` 는 width 를 주지 않으면
    ``cols × 7200 HWPUNIT`` (열당 1인치) 로 표를 만든다. 3열이면 21600,
    즉 76mm 다. A4 본문 폭은 여백 20mm 기준 48190 HWPUNIT(170mm) 이므로
    표가 지면의 45% 만 쓰고 왼쪽에 붙어버린다. ``hp:pos`` 의
    ``horzAlign="LEFT"`` 때문에 남는 공간은 전부 오른쪽에 생긴다.

    그래서 여기서는
      1) 섹션의 ``pagePr`` 에서 실제 본문 폭을 읽어(하드코딩 금지),
      2) 표를 그 폭 그대로 만들고(``width=text_width``),
      3) 열 너비를 비율로 배분하고(``set_column_widths``),
      4) 칸 여백·행 높이·머리행 음영·머리행 반복까지 채워 넣는다.

    행 높이는 hwpx 의 문자폭 추정기(``form_fit.measure``)로 줄 수를 계산해
    잡는다. 한글이 열 때 자동으로 늘려주긴 하지만, 미리 맞춰두면 다른
    뷰어나 PDF 변환에서도 글자가 잘리지 않는다.

★ 표가 쪽 경계를 못 넘던 문제 — ``treatAsChar``:

    hwpx 가 만드는 표는 ``hp:pos treatAsChar="1"`` (글자처럼 취급) 이다.
    글자처럼 취급된 표는 한글에서 '한 글자' 취급이라 쪽 경계에서 쪼개지지
    않는다. 그래서 한 쪽에 안 들어가는 긴 표는 통째로 다음 쪽으로 밀리고
    (앞 쪽은 텅 비고), 그러고도 안 들어가면 종이 밖으로 흘러넘쳐 꼬리말
    위에 겹쳐 찍힌다. 실제로 50개 조문 표에서 두 쪽이 잘려 나갔다.

    ``treatAsChar="0"`` 으로 두면 표가 문단 흐름을 따라가되 쪽 단위로
    나뉜다(``pageBreak="CELL"``). 제목 줄 반복은 ``repeatHeader="1"`` 만으로는
    안 되고 머리행 칸에 ``header="1"`` 도 있어야 한글이 알아본다.

HWPUNIT = 1/7200 inch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from hwpx import HwpxDocument
from hwpx.form_fit.measure import estimate_lines

MM = 7200 / 25.4
"""1mm 당 HWPUNIT."""


def mm(value: float) -> int:
    return round(value * MM)


# 지면 — 공문서 관례(위 20 / 아래 15 / 좌우 20mm)에 맞춘다.
PAGE = {
    "paper_size": "A4",
    "margin_top_mm": 20,
    "margin_bottom_mm": 15,
    "margin_left_mm": 20,
    "margin_right_mm": 20,
    "header_margin_mm": 12,
    "footer_margin_mm": 12,
}

CELL_MARGIN = {"left": mm(1.5), "right": mm(1.5), "top": mm(0.6), "bottom": mm(0.6)}
"""칸 안쪽 여백. 0 이면 글자가 괘선에 붙어 읽기 나쁘다."""

TABLE_MARGIN_BOTTOM = mm(6)
"""표 바깥 아래 여백.

    글자처럼 취급을 끄면(위 설명 참고) 한글이 표 아래 공간을 한 줄쯤
    덜 잡아, 바로 다음 문단이 표 아래 괘선 위에 겹쳐 찍힌다. 그 한 줄을
    여기서 되돌려 준다."""

LINE_FACTOR = 1.65
"""줄 높이 = 글자 크기 × 이 값. 한글 기본 줄간격(160%)에 약간의 여유."""

MERGE_HEIGHT_LIMIT = 0.45
"""세로 병합 한 덩어리의 최대 높이(본문 높이 대비).

    병합된 칸은 쪽이 넘어가도 글자를 되풀이해 주지 않는다. 한 조문이
    한 쪽을 통째로 차지하면 다음 쪽 '조문' 칸이 통째로 비어, 무슨 조문
    이야기인지 알 수 없게 된다. 그래서 덩어리를 반 쪽 이하로 끊고
    조문명을 다시 적는다."""

WRAP_SAFETY = 0.94
"""줄 수 추정에 쓰는 안전 계수 — 추정기가 낙관적일 때 대비."""

HEADER_SHADE = "D9E2F3"
"""머리행 음영(연한 청회색). 흑백 인쇄에서도 옅은 회색으로 구분된다."""


@dataclass(frozen=True)
class Col:
    """표의 열 하나. ``weight`` 는 본문 폭을 나눠 갖는 비율이다."""

    title: str
    weight: float
    align: str = "LEFT"
    vertical: str = "CENTER"


@dataclass
class Styles:
    """문서에서 한 번만 만들어 재사용하는 글꼴/문단 스타일."""

    title: str = ""
    subtitle: str = ""
    section: str = ""
    law: str = ""
    meta: str = ""
    headline: str = ""
    body: str = ""
    note: str = ""
    warn: str = ""
    th: str = ""
    td: str = ""

    p_left: str = ""
    p_center: str = ""
    p_justify: str = ""
    p_body: str = ""
    p_keep: str = ""
    p_section: str = ""
    p_law: str = ""
    p_law_first: str = ""

    sizes: dict[str, float] = field(default_factory=dict)

    @classmethod
    def build(cls, doc: HwpxDocument) -> "Styles":
        head = doc.headers[0]
        # ★ 색을 반드시 명시한다. ensure_run_style 은 굵기·크기만 맞으면
        #   기존 글꼴을 재사용하므로, 색을 비워 두면 먼저 만들어진 빨간
        #   경고 글꼴(굵게 9.5pt)이 표 머리행에 그대로 딸려 온다.
        black = "#000000"
        st = cls(
            title=doc.ensure_run_style(bold=True, size=18, color=black),
            subtitle=doc.ensure_run_style(size=10.5, color="#595959"),
            section=doc.ensure_run_style(bold=True, size=13, color=black),
            law=doc.ensure_run_style(bold=True, size=12, color=black),
            meta=doc.ensure_run_style(size=9, color="#595959"),
            headline=doc.ensure_run_style(bold=True, size=11, color=black),
            body=doc.ensure_run_style(size=10.5, color=black),
            note=doc.ensure_run_style(size=9.5, color="#595959"),
            th=doc.ensure_run_style(bold=True, size=9.5, color=black),
            td=doc.ensure_run_style(size=9.5, color=black),
            warn=doc.ensure_run_style(bold=True, size=9.5, color="#C00000"),
        )
        st.p_left = head.ensure_paragraph_format(alignment="LEFT")
        st.p_center = head.ensure_paragraph_format(alignment="CENTER")
        st.p_justify = head.ensure_paragraph_format(alignment="JUSTIFY")
        st.p_body = head.ensure_paragraph_format(
            alignment="JUSTIFY", line_spacing_percent=160
        )
        # 표·소제목이 쪽 끝에 홀로 떨어지지 않게 다음 문단과 붙여 둔다.
        keep = {"keep_with_next": True}
        st.p_keep = head.ensure_paragraph_format(alignment="LEFT", break_setting=keep)
        st.p_section = head.ensure_paragraph_format(
            alignment="LEFT", break_setting=keep, margins={"prev": mm(4), "next": mm(1)}
        )
        st.p_law = head.ensure_paragraph_format(
            alignment="LEFT", break_setting=keep, margins={"prev": mm(5)}
        )
        # 장 제목 바로 밑에 오는 첫 법령 — 위 여백을 줄인다. 5mm 는 법령끼리
        # 떼어놓기 위한 값이라, 제목 다음 줄에 그대로 쓰면 제목이 내용에서
        # 떨어져 나온 것처럼 보인다(제목 쪽 아래 여백 1mm 와 더해져 6mm).
        st.p_law_first = head.ensure_paragraph_format(
            alignment="LEFT", break_setting=keep, margins={"prev": mm(0.5)}
        )
        st.sizes = {"th": 9.5, "td": 9.5}
        return st


class Page:
    """섹션 지면 정보 — 본문 폭을 계산해 들고 있는다."""

    def __init__(self, doc: HwpxDocument):
        doc.set_page_setup(**PAGE)  # orientation 은 건드리지 않는다(스켈레톤 값 유지).
        props = doc.sections[0].properties
        size, margin = props.page_size, props.page_margins
        self.text_width: int = size.width - margin.left - margin.right
        """본문 폭(HWPUNIT). 표는 정확히 이 폭으로 만든다."""
        self.text_height: int = size.height - margin.top - margin.bottom
        """본문 높이(HWPUNIT). 병합 덩어리가 한 쪽을 넘지 않게 하는 기준."""


def _distribute(total: int, weights: Sequence[float]) -> list[int]:
    """비율대로 나누되 합이 total 과 정확히 같게 맞춘다(반올림 오차 흡수)."""
    unit = sum(weights)
    widths: list[int] = []
    used = 0
    for i, w in enumerate(weights):
        if i == len(weights) - 1:
            widths.append(total - used)
        else:
            v = round(total * w / unit)
            widths.append(v)
            used += v
    return widths


def _line_count(cells: Sequence[Sequence[str]], widths: Sequence[int], pt: float) -> int:
    """행 하나가 차지할 줄 수 — 열마다 계산해 가장 긴 것."""
    lines = 1
    for text, width in zip(cells, widths):
        inner = (width - CELL_MARGIN["left"] - CELL_MARGIN["right"]) * WRAP_SAFETY
        if inner <= 0:
            continue
        used = sum(estimate_lines(part, inner, pt) for part in text.split("\n")) or 1
        lines = max(lines, used)
    return lines


def _row_height(lines: int, pt: float) -> int:
    body = round(lines * pt * 100 * LINE_FACTOR)
    return body + CELL_MARGIN["top"] + CELL_MARGIN["bottom"]


class TableWriter:
    """본문 폭을 꽉 채우는 표를 만든다.

    셀 값은 문자열이며 ``\\n`` 으로 문단을 나눈다. 각 문단은 열 정의의
    정렬을 따르고, 표 전체가 ``Page.text_width`` 폭을 갖는다.
    """

    def __init__(self, doc: HwpxDocument, page: Page, st: Styles):
        self._doc = doc
        self._page = page
        self._st = st

    def write(
        self,
        cols: Sequence[Col],
        rows: Sequence[Sequence[str]],
        *,
        merge_first_column: bool = False,
    ) -> None:
        widths = _distribute(self._page.text_width, [c.weight for c in cols])
        pt = self._st.sizes["td"]

        head_lines = _line_count([c.title for c in cols], widths, self._st.sizes["th"])
        heights = [_row_height(head_lines, self._st.sizes["th"])]
        for row in rows:
            heights.append(_row_height(_line_count(row, widths, pt), pt))

        table = self._doc.add_table(
            len(rows) + 1,
            len(cols),
            width=self._page.text_width,
            height=sum(heights),
        )
        self._make_splittable(table)

        for c, col in enumerate(cols):
            self._fill(table, 0, c, col.title, self._st.th, "CENTER", "CENTER")
            table.cell(0, c).element.set("header", "1")  # 제목 줄 반복의 조건
        for r, row in enumerate(rows, start=1):
            for c, col in enumerate(cols):
                value = row[c] if c < len(row) else ""
                self._fill(table, r, c, value, self._st.td, col.align, col.vertical)

        # 크기는 내용을 넣은 뒤에 잡는다 — set_cell_text 가 칸을 다시 쓰기 때문.
        table.set_column_widths([c.weight for c in cols])
        self._set_heights(table, heights)
        self._set_margins(table)
        for c in range(len(cols)):
            table.set_cell_shading(0, c, HEADER_SHADE)

        if merge_first_column:
            self._merge_runs(table, rows, widths, heights)

    # -- 내부 ---------------------------------------------------------------
    def _make_splittable(self, table) -> None:
        """쪽 경계에서 표가 나뉘도록 만든다(모듈 설명의 treatAsChar 참고)."""
        hp = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
        table.element.set("repeatHeader", "1")
        table.element.set("pageBreak", "CELL")
        pos = table.element.find(f"{hp}pos")
        if pos is not None:
            pos.set("treatAsChar", "0")
            pos.set("flowWithText", "1")
        out = table.element.find(f"{hp}outMargin")
        if out is not None:
            out.set("bottom", str(TABLE_MARGIN_BOTTOM))

    def _fill(
        self,
        table,
        row: int,
        col: int,
        text: str,
        char_pr: str,
        align: str,
        vertical: str,
    ) -> None:
        cell = table.cell(row, col)
        cell.set_text(text or "", split_paragraphs=True)
        sublist = cell.element.find(
            "{http://www.hancom.co.kr/hwpml/2011/paragraph}subList"
        )
        if sublist is not None:
            sublist.set("vertAlign", vertical)
        para_pr = {
            "CENTER": self._st.p_center,
            "JUSTIFY": self._st.p_justify,
        }.get(align, self._st.p_left)
        for paragraph in cell.paragraphs:
            paragraph.para_pr_id_ref = para_pr
            for run in paragraph.runs:
                run.char_pr_id_ref = char_pr

    def _set_heights(self, table, heights: Sequence[int]) -> None:
        for r, height in enumerate(heights):
            for c in range(table.column_count):
                cell = table.cell(r, c)
                if cell.address[0] == r:  # 병합된 칸은 시작 행에서만 손댄다.
                    cell.set_size(height=height)

    def _set_margins(self, table) -> None:
        hp = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
        for name in ("inMargin",):
            node = table.element.find(f"{hp}{name}")
            if node is not None:
                for key, value in CELL_MARGIN.items():
                    node.set(key, str(value))
        seen: set[int] = set()
        for entry in table.iter_grid():
            marker = id(entry.cell.element)
            if marker in seen:
                continue
            seen.add(marker)
            node = entry.cell.element.find(f"{hp}cellMargin")
            if node is None:
                continue
            entry.cell.element.set("hasMargin", "1")
            for key, value in CELL_MARGIN.items():
                node.set(key, str(value))

    def _merge_runs(self, table, rows, widths, heights) -> None:
        """첫 열에서 같은 값이 이어지면 세로로 병합한다.

        조문 표에서 '제60조의2' 가 아홉 줄 반복되는 것을 한 칸으로 묶어
        어느 조문의 이야기인지 한눈에 보이게 한다. 다만 한 덩어리가
        반 쪽을 넘으면 끊는다(MERGE_HEIGHT_LIMIT 설명 참고).
        """
        limit = self._page.text_height * MERGE_HEIGHT_LIMIT
        for first, last in reversed(self._spans(rows, heights, limit)):
            merged = table.merge_cells(first + 1, 0, last + 1, 0)
            merged.set_size(width=widths[0], height=sum(heights[first + 1: last + 2]))

    @staticmethod
    def _spans(rows, heights, limit: float) -> list[tuple[int, int]]:
        """병합할 행 구간들 — 값이 같고, 높이 합이 limit 을 넘지 않는 범위."""
        spans: list[tuple[int, int]] = []
        start = 0
        used = heights[1] if heights else 0
        for i in range(1, len(rows) + 1):
            height = heights[i + 1] if i + 1 < len(heights) else 0
            same = i < len(rows) and bool(rows[start][0]) and rows[i][0] == rows[start][0]
            if same and used + height <= limit:
                used += height
                continue
            if i - start > 1:
                spans.append((start, i - 1))
            start, used = i, height
        return spans
