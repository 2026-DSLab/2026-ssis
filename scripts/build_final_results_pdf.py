"""중간점검 보고서의 'IV 주요 결과' 형식을 따른 최종 발표용 PDF 생성기."""

from __future__ import annotations

from pathlib import Path

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
FINAL = ROOT / "final"
OUTPUT = FINAL / "(법인사이트)주요결과_최종발표본.pdf"

FONT_REGULAR = Path(r"C:\Windows\Fonts\malgun.ttf")
FONT_BOLD = Path(r"C:\Windows\Fonts\malgunbd.ttf")

NAVY = colors.HexColor("#234F7D")
TEXT = colors.HexColor("#171717")
MUTED = colors.HexColor("#5F6368")
LIGHT_BLUE = colors.HexColor("#EEF3F9")
LINE = colors.HexColor("#D8DCE2")


class SectionTitle(Flowable):
    """원본 보고서의 남색 로마숫자 상자 + 밑줄 제목을 재현한다."""

    def __init__(self, roman: str, title: str):
        super().__init__()
        self.roman = roman
        self.title = title
        self.width = 135 * mm
        self.height = 13 * mm

    def draw(self) -> None:
        canvas = self.canv
        box = 10 * mm
        y = 1.2 * mm
        canvas.setFillColor(NAVY)
        canvas.rect(0, y, box, box, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont("Malgun", 14.5)
        canvas.drawCentredString(box / 2, y + 2.2 * mm, self.roman)

        tx = box + 4 * mm
        canvas.setFillColor(TEXT)
        canvas.setFont("Malgun-Bold", 16.5)
        canvas.drawString(tx, y + 2.2 * mm, self.title)
        canvas.setStrokeColor(NAVY)
        canvas.setLineWidth(1.2)
        canvas.line(tx, y, tx + 58 * mm, y)


def register_fonts() -> None:
    pdfmetrics.registerFont(TTFont("Malgun", str(FONT_REGULAR)))
    pdfmetrics.registerFont(TTFont("Malgun-Bold", str(FONT_BOLD)))
    pdfmetrics.registerFontFamily(
        "Malgun", normal="Malgun", bold="Malgun-Bold",
        italic="Malgun", boldItalic="Malgun-Bold",
    )


def styles() -> dict[str, ParagraphStyle]:
    return {
        "h2": ParagraphStyle(
            "h2", fontName="Malgun-Bold", fontSize=13.2, leading=18,
            textColor=TEXT, spaceAfter=8,
        ),
        "h3": ParagraphStyle(
            "h3", fontName="Malgun-Bold", fontSize=11.2, leading=16,
            textColor=TEXT, spaceBefore=7, spaceAfter=4,
        ),
        "bullet": ParagraphStyle(
            "bullet", fontName="Malgun", fontSize=9.55, leading=15.1,
            textColor=TEXT, leftIndent=13, firstLineIndent=-13,
            bulletIndent=0, bulletFontName="Malgun", bulletFontSize=9.55,
            spaceAfter=4.5, wordWrap="CJK",
        ),
        "caption": ParagraphStyle(
            "caption", fontName="Malgun", fontSize=8.6, leading=12,
            textColor=TEXT, alignment=TA_CENTER, spaceBefore=5,
        ),
        "small": ParagraphStyle(
            "small", fontName="Malgun", fontSize=8.8, leading=13.5,
            textColor=TEXT, wordWrap="CJK",
        ),
        "metric_num": ParagraphStyle(
            "metric_num", fontName="Malgun-Bold", fontSize=15.5, leading=18,
            textColor=NAVY, alignment=TA_CENTER,
        ),
        "metric_label": ParagraphStyle(
            "metric_label", fontName="Malgun", fontSize=8.2, leading=11,
            textColor=MUTED, alignment=TA_CENTER,
        ),
        "step": ParagraphStyle(
            "step", fontName="Malgun-Bold", fontSize=9.2, leading=13,
            textColor=colors.white, alignment=TA_CENTER,
        ),
        "conclusion": ParagraphStyle(
            "conclusion", fontName="Malgun-Bold", fontSize=10.4, leading=16,
            textColor=NAVY, alignment=TA_CENTER, wordWrap="CJK",
        ),
    }


def bullet(text: str, st: dict[str, ParagraphStyle]) -> Paragraph:
    return Paragraph(text, st["bullet"], bulletText="ㅇ")


def screenshot(path: Path, caption: str, st: dict[str, ParagraphStyle], *, max_h: float) -> KeepTogether:
    max_w = 174 * mm
    with PILImage.open(path) as im:
        px_w, px_h = im.size
    scale = min(max_w / px_w, max_h / px_h)
    image = Image(str(path), width=px_w * scale, height=px_h * scale)
    frame = Table([[image]], colWidths=[image.drawWidth + 2], rowHeights=[image.drawHeight + 2])
    frame.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#A9ADB4")),
        ("LEFTPADDING", (0, 0), (-1, -1), 1),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    return KeepTogether([frame, Paragraph(caption, st["caption"])])


def metrics(st: dict[str, ParagraphStyle]) -> Table:
    data = [
        [
            Paragraph("102건", st["metric_num"]),
            Paragraph("2건", st["metric_num"]),
            Paragraph("28건", st["metric_num"]),
            Paragraph("4개 유형", st["metric_num"]),
        ],
        [
            Paragraph("감시 대상", st["metric_label"]),
            Paragraph("이번 주 개정 법령", st["metric_label"]),
            Paragraph("이번 주 변경 조문", st["metric_label"]),
            Paragraph("법령·시행령·시행규칙·행정규칙", st["metric_label"]),
        ],
    ]
    table = Table(data, colWidths=[43.5 * mm] * 4, rowHeights=[8 * mm, 9 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BLUE),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CAD5E3")),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D7E0EA")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return table


def process_flow(st: dict[str, ParagraphStyle]) -> Table:
    cells = [
        Paragraph("① 개정 감지", st["step"]),
        Paragraph("② 조문 구조화·비교", st["step"]),
        Paragraph("③ AI 요약·규칙 검증", st["step"]),
        Paragraph("④ 웹·HWPX 제공", st["step"]),
    ]
    table = Table([cells], colWidths=[43.5 * mm] * 4, rowHeights=[12 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("BOX", (0, 0), (-1, -1), 0.6, NAVY),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def page_number(canvas, doc) -> None:
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.4)
    canvas.line(20 * mm, 15 * mm, 190 * mm, 15 * mm)
    canvas.setFillColor(MUTED)
    canvas.setFont("Malgun", 7.5)
    canvas.drawString(20 * mm, 10.5 * mm, "[법인사이트] 주요 결과 최종 발표본")
    canvas.drawRightString(190 * mm, 10.5 * mm, str(doc.page))
    canvas.restoreState()


def build() -> Path:
    register_fonts()
    st = styles()
    doc = SimpleDocTemplate(
        str(OUTPUT), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=17 * mm, bottomMargin=20 * mm,
        title="[법인사이트] 주요 결과 최종 발표본",
        author="법인사이트 프로젝트",
        subject="법령·행정규칙 개정 모니터링 서비스 주요 결과",
    )

    story = [SectionTitle("IV", "주요 결과"), Spacer(1, 4 * mm)]

    # 1쪽 — 개정 감지 및 요약
    story += [
        Paragraph("1. 법령 개정 자동 감지 및 요약 결과", st["h2"]),
        bullet("국가법령정보 Open API와 감시 목록을 연계하여 <b>법령·행정규칙 102건</b>의 최신 일련번호와 시행상태를 확인하고, 실제로 변경된 항목만 선별하도록 구현", st),
        bullet("개정 전·후 원문을 조·항·호·목 단위로 구조화하고 <b>개정·신설·삭제·이동·이동개정</b>으로 분류하여 위치 정보와 함께 데이터베이스에 저장", st),
        bullet("탐지된 변경 조문은 AI가 핵심 내용을 요약하고, 별도의 규칙 기반 검증기가 원문 불일치와 개정 전·후 방향 오류를 재검증하는 절차를 적용", st),
        bullet("이번 주·최근 5일·최근 2주·최근 1개월 단위로 결과를 조회하고, 법령명 검색·유형별 분류·원문 연결 및 <b>HWPX 보고서 다운로드</b> 기능을 제공", st),
        Spacer(1, 2 * mm),
        metrics(st),
        Spacer(1, 5 * mm),
        screenshot(
            FINAL / "개정요약.png",
            "[그림 1] 주간 개정 요약 화면(2026. 8. 17. 기준: 개정 법령 2건, 변경 조문 28건)",
            st, max_h=83 * mm,
        ),
        PageBreak(),
    ]

    # 2쪽 — 전문 목록 및 전후 비교
    story += [
        Paragraph("2. 감시 대상 전문 조회 및 개정 전·후 비교", st["h2"]),
        bullet("감시 대상 102건을 법률·시행령·시행규칙·행정규칙으로 구분하고, 법령명 검색과 유형 필터를 통해 필요한 전문을 즉시 조회하도록 구성", st),
        bullet("선택한 법령의 현재 버전과 직전 버전을 나란히 배치하고, 어절 단위 비교를 통해 삭제·변경·신설 부분을 색상으로 구분하여 개정 범위를 한 화면에서 확인", st),
        bullet("조문 내용 검색, 전전 버전 조회, 시행일 표시 및 최근 개정 배지를 함께 제공하여 개정 이력과 현행 조문을 연속적으로 검토할 수 있도록 구현", st),
        Spacer(1, 2 * mm),
        screenshot(
            FINAL / "전문보기.png",
            "[그림 2] 감시 대상 법령·행정규칙 102건의 전문 조회 목록",
            st, max_h=72 * mm,
        ),
        Spacer(1, 5 * mm),
        screenshot(
            FINAL / "전문보기(전문).png",
            "[그림 3] 개정 전·후 전문 나란히 보기 및 변경 부분 강조 화면",
            st, max_h=72 * mm,
        ),
        PageBreak(),
    ]

    # 3쪽 — 업로드 문서 인용 점검
    story += [
        Paragraph("3. 업무 문서 내 법령 인용 점검", st["h2"]),
        bullet("PDF 또는 HWPX 문서에서 텍스트를 추출한 뒤, 감시 대상 102건의 법령명 사전과 결정론적 문자열 방식으로 대조하여 인용 법령을 자동 식별", st),
        bullet("법령별 인용 횟수와 인용 페이지를 집계하고 해당 전문으로 연결하여, 대용량 업무 문서에서도 검토 대상과 위치를 빠르게 추적할 수 있도록 구현", st),
        bullet("최근 90일 이내 개정된 법령에는 개정 유형·시행일 배지를 표시하고 요약 문구를 함께 제공하여 최신성 재검토가 필요한 항목을 우선 확인", st),
        bullet("업로드 파일은 최대 50MB까지 처리하며 분석 후 저장하지 않고, 외부 API나 AI로 전송하지 않도록 구성. 텍스트가 부족한 스캔형 PDF에는 별도 경고를 표시", st),
        Spacer(1, 4 * mm),
        screenshot(
            FINAL / "PDF 확인.png",
            "[그림 4] 294쪽 업무 문서에서 감시 대상 102건 중 인용 법령 18건을 확인한 결과",
            st, max_h=91 * mm,
        ),
        Paragraph("서비스 구현 결과", st["h3"]),
        bullet("개정 요약·전문 보기·문서 인용 확인의 세 기능을 공통 화면과 메뉴로 통합하고, PostgreSQL에 축적된 개정 데이터와 원문 버전을 일관된 기준으로 활용", st),
        bullet("주간 배치 한 번으로 감지→비교→요약→검증→DB 적재→HWPX 생성을 수행하며, 기간별 즉석 조회는 백그라운드 처리·진행상태 표시·15분 캐시를 적용", st),
        PageBreak(),
    ]

    # 4쪽 — 종합 성과 및 활용 방안
    story += [
        Paragraph("4. 종합 성과 및 활용 가능성", st["h2"]),
        process_flow(st),
        Spacer(1, 5 * mm),
        Paragraph("가. 주요 성과", st["h3"]),
        bullet("개정 여부 확인부터 조문 위치 확정, 전·후 비교, AI 요약과 규칙 검증, 웹 조회 및 HWPX 보고서 생성까지의 전 과정을 하나의 파이프라인으로 연결", st),
        bullet("개정 현황 요약, 전체 전문 비교, 업무 문서 인용 점검이라는 세 가지 사용자 흐름을 구현하여 ‘무엇이 바뀌었는지’와 ‘내 문서에 어떤 영향을 주는지’를 함께 확인", st),
        bullet("일련번호·원문 버전·조문 위치·요약 결과를 데이터베이스에 연결해 결과의 출처와 개정 이력을 다시 추적할 수 있는 구조를 확보", st),
        Paragraph("나. 실무 활용 효과", st["h3"]),
        bullet("담당자가 매주 102건을 개별 검색하는 반복 업무를 줄이고, 실제 변경이 발생한 법령과 조문 중심으로 검토 범위를 축소", st),
        bullet("계약서·운영 매뉴얼·내부 지침 등 분량이 큰 문서의 법령 인용 위치를 자동 집계하여 누락 및 구버전 법령 참조 위험을 사전에 점검", st),
        bullet("웹 화면에서 원문과 개정 전·후를 확인하고 동일 결과를 HWPX로 내려받을 수 있어 내부 검토, 보고 및 근거 보관에 활용 가능", st),
        Paragraph("다. 운영 적용 및 향후 고도화", st["h3"]),
        bullet("Windows 작업 스케줄러에 주간 배치를 등록하고 실행 로그·캐시·DB 적재 상태를 점검하는 방식으로 정기 운영 가능", st),
        bullet("운영 환경에서는 API 키와 DB 비밀번호를 별도로 관리하고, 개발용 Flask 서버 대신 WSGI·HTTPS·DB 백업 체계를 적용하여 안정성과 보안을 강화", st),
        bullet("감시 목록 최신화, 국가법령정보 API 응답 형식 변화에 대한 회귀 테스트, 스캔 PDF의 OCR 연계 및 사용자별 알림 기능을 후속 고도화 과제로 추진", st),
        Spacer(1, 7 * mm),
    ]

    conclusion = Table(
        [[Paragraph(
            "법인사이트는 법령 개정 정보를 ‘발견–해석–검증–활용’의 흐름으로 연결하여,<br/>"
            "담당자의 정기 모니터링과 업무 문서 점검을 지원하는 실무형 서비스로 구현되었습니다.",
            st["conclusion"],
        )]],
        colWidths=[174 * mm],
    )
    conclusion.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BLUE),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#B7C8DA")),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(conclusion)

    doc.build(story, onFirstPage=page_number, onLaterPages=page_number)
    return OUTPUT


if __name__ == "__main__":
    print(build())
