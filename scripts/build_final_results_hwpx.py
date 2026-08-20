"""PDF 최종 발표본과 같은 내용·순서의 편집 가능한 HWPX를 생성한다."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from hwpx import HwpxDocument


ROOT = Path(__file__).resolve().parents[1]
FINAL = ROOT / "final"
OUTPUT = FINAL / "(법인사이트)주요결과_최종발표본.hwpx"

HWPUNIT_PER_MM = 7200 / 25.4
NAVY = "#234F7D"
TEXT = "#171717"
MUTED = "#5F6368"
LIGHT_BLUE = "#EEF3F9"
LINE = "#CAD5E3"


def mm(value: float) -> int:
    return round(value * HWPUNIT_PER_MM)


@dataclass
class Styles:
    title: str
    h2: str
    h3: str
    body: str
    body_bold: str
    caption: str
    metric_num: str
    metric_label: str
    white_title: str
    conclusion: str
    p_left: str
    p_center: str
    p_body: str
    p_bullet: str
    p_h2: str
    p_h2_break: str
    p_h3: str

    @classmethod
    def build(cls, doc: HwpxDocument) -> "Styles":
        head = doc.oxml.headers[0]
        title = doc.styles.ensure_run(font="맑은 고딕", bold=True, size=16.5, color=TEXT)
        h2 = doc.styles.ensure_run(font="맑은 고딕", bold=True, size=13.2, color=TEXT)
        h3 = doc.styles.ensure_run(font="맑은 고딕", bold=True, size=11.2, color=TEXT)
        body = doc.styles.ensure_run(font="맑은 고딕", size=9.55, color=TEXT)
        body_bold = doc.styles.ensure_run(font="맑은 고딕", bold=True, size=9.55, color=TEXT)
        caption = doc.styles.ensure_run(font="맑은 고딕", size=8.6, color=TEXT)
        metric_num = doc.styles.ensure_run(font="맑은 고딕", bold=True, size=15.5, color=NAVY)
        metric_label = doc.styles.ensure_run(font="맑은 고딕", size=8.2, color=MUTED)
        white_title = doc.styles.ensure_run(font="맑은 고딕", bold=True, size=10, color="#FFFFFF")
        conclusion = doc.styles.ensure_run(font="맑은 고딕", bold=True, size=10.4, color=NAVY)

        p_left = head.ensure_paragraph_format(alignment="LEFT", line_spacing_percent=150)
        p_center = head.ensure_paragraph_format(alignment="CENTER", line_spacing_percent=140)
        p_body = head.ensure_paragraph_format(
            alignment="JUSTIFY", line_spacing_percent=150,
            margins={"next": mm(1.2)},
        )
        p_bullet = head.ensure_paragraph_format(
            alignment="JUSTIFY", line_spacing_percent=150,
            margins={"left": mm(5), "intent": -mm(5), "next": mm(1.2)},
        )
        p_h2 = head.ensure_paragraph_format(
            alignment="LEFT", line_spacing_percent=140,
            margins={"next": mm(2)},
            break_setting={"keep_with_next": True},
        )
        p_h2_break = head.ensure_paragraph_format(
            alignment="LEFT", line_spacing_percent=140,
            margins={"next": mm(2)},
            break_setting={"keep_with_next": True, "page_break_before": True},
        )
        p_h3 = head.ensure_paragraph_format(
            alignment="LEFT", line_spacing_percent=140,
            margins={"prev": mm(2.5), "next": mm(1)},
            break_setting={"keep_with_next": True},
        )
        return cls(
            title, h2, h3, body, body_bold, caption, metric_num, metric_label,
            white_title, conclusion, p_left, p_center, p_body, p_bullet,
            p_h2, p_h2_break, p_h3,
        )


class Report:
    def __init__(self) -> None:
        self.doc = HwpxDocument.new()
        self.doc.page.setup(
            paper_size="A4",
            margin_top_mm=17,
            margin_bottom_mm=20,
            margin_left_mm=18,
            margin_right_mm=18,
            header_margin_mm=10,
            footer_margin_mm=10,
        )
        self.st = Styles.build(self.doc)
        self.no_border = self.doc.styles.ensure_border_fill(
            border_color="#FFFFFF", border_width="0.1 mm", active_borders=[]
        )
        self.grid_border = self.doc.styles.ensure_border_fill(
            border_color=LINE, border_width="0.12 mm", active_borders=["LEFT", "RIGHT", "TOP", "BOTTOM"]
        )

    def paragraph(
        self,
        text: str,
        *,
        char_style: str | None = None,
        para_style: str | None = None,
        rich: bool = False,
    ):
        if not rich:
            return self.doc.add_paragraph(
                text,
                char_pr_id_ref=char_style or self.st.body,
                para_pr_id_ref=para_style or self.st.p_body,
                inherit_style=False,
            )
        paragraph = self.doc.add_paragraph(
            "",
            para_pr_id_ref=para_style or self.st.p_body,
            include_run=False,
            inherit_style=False,
        )
        for part in re.split(r"(<b>.*?</b>)", text):
            if not part:
                continue
            if part.startswith("<b>") and part.endswith("</b>"):
                paragraph.add_run(part[3:-4], char_pr_id_ref=self.st.body_bold)
            else:
                paragraph.add_run(part, char_pr_id_ref=char_style or self.st.body)
        return paragraph

    def bullet(self, text: str) -> None:
        self.paragraph(f"ㅇ  {text}", para_style=self.st.p_bullet, rich=True)

    def h2(self, text: str, *, page_break: bool = False) -> None:
        self.paragraph(
            text,
            char_style=self.st.h2,
            para_style=self.st.p_h2_break if page_break else self.st.p_h2,
        )

    def h3(self, text: str) -> None:
        self.paragraph(text, char_style=self.st.h3, para_style=self.st.p_h3)

    def gap(self, height_mm: float = 2) -> None:
        head = self.doc.oxml.headers[0]
        p_gap = head.ensure_paragraph_format(
            alignment="LEFT", line_spacing_percent=100,
            margins={"next": mm(height_mm)},
        )
        self.doc.add_paragraph("", char_pr_id_ref=self.st.body, para_pr_id_ref=p_gap)

    def _style_cell(self, table, row: int, col: int, text: str, char_style: str, para_style: str) -> None:
        cell = table.cell(row, col)
        cell.set_text(text)
        sublist = cell.element.find("{http://www.hancom.co.kr/hwpml/2011/paragraph}subList")
        if sublist is not None:
            sublist.set("vertAlign", "CENTER")
        for paragraph in cell.paragraphs:
            paragraph.para_pr_id_ref = para_style
            for run in paragraph.runs:
                run.char_pr_id_ref = char_style

    def section_title(self) -> None:
        bottom_rule = self.doc.styles.ensure_border_fill(
            border_color=NAVY,
            border_width="0.5 mm",
            fill_color="#FFFFFF",
            active_borders=["BOTTOM"],
        )
        navy_fill = self.doc.styles.ensure_border_fill(
            border_color=NAVY,
            border_width="0.1 mm",
            fill_color=NAVY,
            active_borders=["LEFT", "RIGHT", "TOP", "BOTTOM"],
        )
        table = self.doc.add_table(
            1, 2, width=mm(84), height=mm(11), border_fill_id_ref=self.no_border
        )
        table.set_column_widths([12, 72])
        table.set_cell_border_fill(0, 0, navy_fill)
        table.set_cell_border_fill(0, 1, bottom_rule)
        self._style_cell(table, 0, 0, "IV", self.st.white_title, self.st.p_center)
        self._style_cell(table, 0, 1, "주요 결과", self.st.title, self.st.p_left)
        table.cell(0, 0).set_size(height=mm(10))
        table.cell(0, 1).set_size(height=mm(10))
        self.gap(2)

    def metrics(self) -> None:
        values = ["102건", "2건", "28건", "4개 유형"]
        labels = ["감시 대상", "이번 주 개정 법령", "이번 주 변경 조문", "법령·시행령·시행규칙·행정규칙"]
        table = self.doc.add_table(
            2, 4, width=mm(174), height=mm(17), border_fill_id_ref=self.grid_border
        )
        table.set_column_widths([1, 1, 1, 1])
        for col in range(4):
            table.set_cell_shading(0, col, LIGHT_BLUE)
            table.set_cell_shading(1, col, LIGHT_BLUE)
            self._style_cell(table, 0, col, values[col], self.st.metric_num, self.st.p_center)
            self._style_cell(table, 1, col, labels[col], self.st.metric_label, self.st.p_center)
            table.cell(0, col).set_size(height=mm(8))
            table.cell(1, col).set_size(height=mm(9))

    def flow(self) -> None:
        labels = ["① 개정 감지", "② 조문 구조화·비교", "③ AI 요약·규칙 검증", "④ 웹·HWPX 제공"]
        navy_fill = self.doc.styles.ensure_border_fill(
            border_color="#FFFFFF", border_width="0.12 mm", fill_color=NAVY,
            active_borders=["LEFT", "RIGHT", "TOP", "BOTTOM"],
        )
        table = self.doc.add_table(
            1, 4, width=mm(174), height=mm(12), border_fill_id_ref=navy_fill
        )
        table.set_column_widths([1, 1, 1, 1])
        for col, label in enumerate(labels):
            table.set_cell_border_fill(0, col, navy_fill)
            self._style_cell(table, 0, col, label, self.st.white_title, self.st.p_center)
            table.cell(0, col).set_size(height=mm(12))

    def picture(self, filename: str, caption: str, height_mm: float) -> None:
        path = FINAL / filename
        self.doc.add_picture(
            path.read_bytes(), "png", width_mm=174, height_mm=height_mm,
            align="CENTER", para_pr_id_ref=self.st.p_center,
        )
        self.paragraph(caption, char_style=self.st.caption, para_style=self.st.p_center)

    def conclusion(self) -> None:
        fill = self.doc.styles.ensure_border_fill(
            border_color="#B7C8DA", border_width="0.18 mm", fill_color=LIGHT_BLUE,
            active_borders=["LEFT", "RIGHT", "TOP", "BOTTOM"],
        )
        table = self.doc.add_table(
            1, 1, width=mm(174), height=mm(18), border_fill_id_ref=fill
        )
        table.set_cell_border_fill(0, 0, fill)
        self._style_cell(
            table, 0, 0,
            "법인사이트는 법령 개정 정보를 ‘발견–해석–검증–활용’의 흐름으로 연결하여,\n"
            "담당자의 정기 모니터링과 업무 문서 점검을 지원하는 실무형 서비스로 구현되었습니다.",
            self.st.conclusion, self.st.p_center,
        )
        table.cell(0, 0).set_size(height=mm(18))

    def build(self) -> Path:
        self.section_title()
        self.h2("1. 법령 개정 자동 감지 및 요약 결과")
        self.bullet("국가법령정보 Open API와 감시 목록을 연계하여 <b>법령·행정규칙 102건</b>의 최신 일련번호와 시행상태를 확인하고, 실제로 변경된 항목만 선별하도록 구현")
        self.bullet("개정 전·후 원문을 조·항·호·목 단위로 구조화하고 <b>개정·신설·삭제·이동·이동개정</b>으로 분류하여 위치 정보와 함께 데이터베이스에 저장")
        self.bullet("탐지된 변경 조문은 AI가 핵심 내용을 요약하고, 별도의 규칙 기반 검증기가 원문 불일치와 개정 전·후 방향 오류를 재검증하는 절차를 적용")
        self.bullet("이번 주·최근 5일·최근 2주·최근 1개월 단위로 결과를 조회하고, 법령명 검색·유형별 분류·원문 연결 및 <b>HWPX 보고서 다운로드</b> 기능을 제공")
        self.gap(1)
        self.metrics()
        self.gap(3)
        self.picture(
            "개정요약.png",
            "[그림 1] 주간 개정 요약 화면(2026. 8. 17. 기준: 개정 법령 2건, 변경 조문 28건)",
            83,
        )

        self.h2("2. 감시 대상 전문 조회 및 개정 전·후 비교", page_break=True)
        self.bullet("감시 대상 102건을 법률·시행령·시행규칙·행정규칙으로 구분하고, 법령명 검색과 유형 필터를 통해 필요한 전문을 즉시 조회하도록 구성")
        self.bullet("선택한 법령의 현재 버전과 직전 버전을 나란히 배치하고, 어절 단위 비교를 통해 삭제·변경·신설 부분을 색상으로 구분하여 개정 범위를 한 화면에서 확인")
        self.bullet("조문 내용 검색, 전전 버전 조회, 시행일 표시 및 최근 개정 배지를 함께 제공하여 개정 이력과 현행 조문을 연속적으로 검토할 수 있도록 구현")
        self.gap(1)
        self.picture("전문보기.png", "[그림 2] 감시 대상 법령·행정규칙 102건의 전문 조회 목록", 72)
        self.gap(3)
        self.picture(
            "전문보기(전문).png",
            "[그림 3] 개정 전·후 전문 나란히 보기 및 변경 부분 강조 화면",
            72,
        )

        self.h2("3. 업무 문서 내 법령 인용 점검", page_break=True)
        self.bullet("PDF 또는 HWPX 문서에서 텍스트를 추출한 뒤, 감시 대상 102건의 법령명 사전과 결정론적 문자열 방식으로 대조하여 인용 법령을 자동 식별")
        self.bullet("법령별 인용 횟수와 인용 페이지를 집계하고 해당 전문으로 연결하여, 대용량 업무 문서에서도 검토 대상과 위치를 빠르게 추적할 수 있도록 구현")
        self.bullet("최근 90일 이내 개정된 법령에는 개정 유형·시행일 배지를 표시하고 요약 문구를 함께 제공하여 최신성 재검토가 필요한 항목을 우선 확인")
        self.bullet("업로드 파일은 최대 50MB까지 처리하며 분석 후 저장하지 않고, 외부 API나 AI로 전송하지 않도록 구성. 텍스트가 부족한 스캔형 PDF에는 별도 경고를 표시")
        self.gap(2)
        self.picture(
            "PDF 확인.png",
            "[그림 4] 294쪽 업무 문서에서 감시 대상 102건 중 인용 법령 18건을 확인한 결과",
            91,
        )
        self.h3("서비스 구현 결과")
        self.bullet("개정 요약·전문 보기·문서 인용 확인의 세 기능을 공통 화면과 메뉴로 통합하고, PostgreSQL에 축적된 개정 데이터와 원문 버전을 일관된 기준으로 활용")
        self.bullet("주간 배치 한 번으로 감지→비교→요약→검증→DB 적재→HWPX 생성을 수행하며, 기간별 즉석 조회는 백그라운드 처리·진행상태 표시·15분 캐시를 적용")

        self.h2("4. 종합 성과 및 활용 가능성", page_break=True)
        self.flow()
        self.gap(3)
        self.h3("가. 주요 성과")
        self.bullet("개정 여부 확인부터 조문 위치 확정, 전·후 비교, AI 요약과 규칙 검증, 웹 조회 및 HWPX 보고서 생성까지의 전 과정을 하나의 파이프라인으로 연결")
        self.bullet("개정 현황 요약, 전체 전문 비교, 업무 문서 인용 점검이라는 세 가지 사용자 흐름을 구현하여 ‘무엇이 바뀌었는지’와 ‘내 문서에 어떤 영향을 주는지’를 함께 확인")
        self.bullet("일련번호·원문 버전·조문 위치·요약 결과를 데이터베이스에 연결해 결과의 출처와 개정 이력을 다시 추적할 수 있는 구조를 확보")
        self.h3("나. 실무 활용 효과")
        self.bullet("담당자가 매주 102건을 개별 검색하는 반복 업무를 줄이고, 실제 변경이 발생한 법령과 조문 중심으로 검토 범위를 축소")
        self.bullet("계약서·운영 매뉴얼·내부 지침 등 분량이 큰 문서의 법령 인용 위치를 자동 집계하여 누락 및 구버전 법령 참조 위험을 사전에 점검")
        self.bullet("웹 화면에서 원문과 개정 전·후를 확인하고 동일 결과를 HWPX로 내려받을 수 있어 내부 검토, 보고 및 근거 보관에 활용 가능")
        self.h3("다. 운영 적용 및 향후 고도화")
        self.bullet("Windows 작업 스케줄러에 주간 배치를 등록하고 실행 로그·캐시·DB 적재 상태를 점검하는 방식으로 정기 운영 가능")
        self.bullet("운영 환경에서는 API 키와 DB 비밀번호를 별도로 관리하고, 개발용 Flask 서버 대신 WSGI·HTTPS·DB 백업 체계를 적용하여 안정성과 보안을 강화")
        self.bullet("감시 목록 최신화, 국가법령정보 API 응답 형식 변화에 대한 회귀 테스트, 스캔 PDF의 OCR 연계 및 사용자별 알림 기능을 후속 고도화 과제로 추진")
        self.gap(3)
        self.conclusion()

        self.doc.page.set_page_number(target="footer", align="RIGHT", position="BOTTOM_RIGHT")
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save_to_path(str(OUTPUT))
        return OUTPUT


if __name__ == "__main__":
    print(Report().build())
