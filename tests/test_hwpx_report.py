"""WeeklyContract JSON → HWPX 보고서 생성 테스트."""

import zipfile
from datetime import datetime
from xml.etree import ElementTree as ET

from hwpx import HwpxDocument, validate_editor_open_safety

from lawtrack.contract.schema import (
    AmendmentGroup,
    ArticleDiffItem,
    LawChange,
    LawLLMSummary,
    LLMSummary,
    NoComparisonItem,
    Period,
    UnresolvedItem,
    WeeklyContract,
)
from lawtrack.report.hwpx import (
    _manual_review_rows,
    build_weekly_report_plan,
    write_weekly_hwpx,
)


def _contract() -> WeeklyContract:
    return WeeklyContract(
        batch_date="2026-07-21",
        period=Period(from_date="2026-07-14", to_date="2026-07-21"),
        amendment_groups=[
            AmendmentGroup(
                group_id="single-001973-276653",
                promulgation_no="21065",
                promulgation_date="2026-07-15",
                revision_type="타법개정",
                affected_law_ids=["001973"],
                laws=[
                    LawChange(
                        law_id="001973",
                        law_type="법률",
                        law_name="국민기초생활보장법",
                        internal_name="국민기초생활보장법",
                        new_serial_no="276653",
                        enforce_date="2026-07-18",
                        revision_type="타법개정",
                        revision_reason="정부조직 개편에 따른 기관 명칭을 정비함.",
                        source_url="https://example.invalid/law",
                        articles=[
                            ArticleDiffItem(
                                article_label="제1조",
                                clause_no="①",
                                change_type="개정",
                                old_text="종전 문장",
                                new_text="개정 문장",
                                match_status="성공",
                            )
                        ],
                    )
                ],
            )
        ],
        unresolved=[
            UnresolvedItem(
                law_id="001973",
                law_name="국민기초생활보장법",
                new_serial_no="276653",
                reason="0건실패",
                detail="원문 수동 대조 필요",
            )
        ],
        no_comparison=[
            NoComparisonItem(
                law_id="42430",
                law_name="행정업무용 표준 관리규정",
                new_serial_no="2100000254490",
                reason="일부개정",
            )
        ],
    )


def test_plan_contains_required_report_sections_and_facts():
    plan = build_weekly_report_plan(
        _contract(),
        author="김용현",
        department="법인사이트팀",
        manager="검토자",
        generated_at=datetime(2026, 7, 21, 9, 0),
    )

    text = str(plan)
    assert "보고 개요" in text
    assert "개정 현황" in text
    assert "국민기초생활보장법" in text
    assert "종전 문장" in text
    assert "개정 문장" in text
    assert "행정업무용 표준 관리규정" in text
    assert "김용현" in text

    tables = [block for block in plan["blocks"] if block.get("type") == "table"]
    assert max(len(table["columns"]) for table in tables) <= 6
    before_after = [
        table for table in tables
        if [column["label"] for column in table["columns"]] == ["구분", "내용"]
        and any(row.get("side") == "개정 전" for row in table["rows"])
    ]
    assert before_after

    # 첫 장은 표지·핵심 요약 전용이며, 개요는 새 페이지에서 시작한다.
    overview_index = next(
        index for index, block in enumerate(plan["blocks"])
        if block.get("runs", [{}])[0].get("text") == "1.  보고 개요"
    )
    assert plan["blocks"][overview_index - 1] == {"type": "page_break"}

    # 절 제목과 중복되는 table caption은 한글에서 고아 문단을 만들므로 쓰지 않는다.
    assert "유형별 개정 법령" not in [table.get("caption") for table in tables]
    assert "개정 법령 목록" not in [table.get("caption") for table in tables]


def test_plan_renders_openai_summary_and_model_metadata():
    contract = _contract()
    contract.llm_summary = LLMSummary(
        model="gpt-test",
        generated_at="2026-07-21T09:10:00",
        executive_summary="이번 주 핵심 개정 요약입니다.",
        law_summaries=[
            LawLLMSummary(
                law_id="001973",
                new_serial_no="276653",
                headline="기관 명칭 정비",
                summary="조문에 사용된 기관 명칭을 정비했습니다.",
                key_changes=["제1조 기관 명칭 변경"],
                operational_impact="내부 서식 확인 필요",
                review_points=["시행일 확인"],
            )
        ],
    )

    plan = build_weekly_report_plan(contract)
    text = str(plan)

    assert "이번 주 핵심 개정 요약입니다." in text
    assert "AI 업무 요약" in text
    assert "기관 명칭 정비" in text
    assert "gpt-test" in text


def test_empty_contract_still_builds_a_valid_plan():
    contract = WeeklyContract(
        batch_date="2026-07-21",
        period=Period(from_date="2026-07-14", to_date="2026-07-21"),
    )

    plan = build_weekly_report_plan(contract)

    assert "이번 배치에서 신규 감지된 법령 버전이 없습니다." in str(plan)


def test_suspected_relocation_is_listed_for_manual_review():
    contract = _contract()
    law = contract.amendment_groups[0].laws[0]
    law.articles[0].match_status = "위치재배치의심"

    rows = _manual_review_rows(contract)
    plan_text = str(build_weekly_report_plan(contract))

    assert rows[0]["name"] == "국민기초생활보장법"
    assert "제1조①: 위치재배치의심" in rows[0]["detail"]
    assert "자동판정 원문 대조" in plan_text
    assert "원문 대조 필요" in plan_text


def test_writes_editor_open_safe_hwpx(tmp_path):
    output = tmp_path / "weekly-law-report.hwpx"

    result = write_weekly_hwpx(_contract(), output)

    assert output.exists()
    assert result["safety"]["ok"] is True
    with zipfile.ZipFile(output) as archive:
        assert archive.read("mimetype") == b"application/hwp+zip"
        assert "Contents/section0.xml" in archive.namelist()

    reopened = HwpxDocument.open(output)
    try:
        markdown = reopened.export_markdown()
    finally:
        reopened.close()
    assert "국민기초생활보장법" in markdown
    assert "개정 문장" in markdown
    assert validate_editor_open_safety(output).to_dict()["ok"] is True


def test_generated_hwpx_uses_hancom_safe_alignment_and_text_sizes(tmp_path):
    output = tmp_path / "weekly-law-report-layout.hwpx"
    write_weekly_hwpx(_contract(), output)

    with zipfile.ZipFile(output) as archive:
        header = ET.fromstring(archive.read("Contents/header.xml"))
        section = ET.fromstring(archive.read("Contents/section0.xml"))

    para_align = {}
    char_height = {}
    para_spacing = {}
    for node in header.iter():
        local = node.tag.rsplit("}", 1)[-1]
        if local == "paraPr":
            align = next(
                (child for child in node if child.tag.rsplit("}", 1)[-1] == "align"),
                None,
            )
            para_align[node.get("id")] = align.get("horizontal") if align is not None else None
            margin = next(
                (child for child in node.iter() if child.tag.rsplit("}", 1)[-1] == "margin"),
                None,
            )
            para_spacing[node.get("id")] = {
                child.tag.rsplit("}", 1)[-1]: child.get("value")
                for child in margin
            } if margin is not None else {}
        elif local == "charPr":
            char_height[node.get("id")] = int(node.get("height", "0"))

    paragraphs = [node for node in section.iter() if node.tag.rsplit("}", 1)[-1] == "p"]

    def paragraph_text(paragraph):
        return "".join(
            node.text or ""
            for node in paragraph.iter()
            if node.tag.rsplit("}", 1)[-1] == "t"
        )

    for prefix in ("01   국민기초생활보장법", "■  개정 전·후 상세 비교", "변경 01", "법제처 원문"):
        paragraph = next(node for node in paragraphs if paragraph_text(node).startswith(prefix))
        assert para_align[paragraph.get("paraPrIDRef")] == "LEFT"

    # 자동 개요번호를 사용하지 않고 제목과 다음 내용을 같은 페이지에 묶는다.
    para_breaks = {}
    para_heading_types = {}
    for node in header.iter():
        if node.tag.rsplit("}", 1)[-1] != "paraPr":
            continue
        break_setting = next(
            (child for child in node if child.tag.rsplit("}", 1)[-1] == "breakSetting"),
            None,
        )
        heading = next(
            (child for child in node if child.tag.rsplit("}", 1)[-1] == "heading"),
            None,
        )
        para_breaks[node.get("id")] = (
            break_setting.get("keepWithNext") if break_setting is not None else None
        )
        para_heading_types[node.get("id")] = (
            heading.get("type") if heading is not None else None
        )

    section_headings = [
        next(node for node in paragraphs if paragraph_text(node).startswith(prefix))
        for prefix in (
            "1.  보고 개요",
            "2.  개정 현황",
            "3.  법령별 한눈에 보기",
            "4.  법령별 상세",
        )
    ]
    for paragraph in section_headings:
        para_ref = paragraph.get("paraPrIDRef")
        assert para_align[para_ref] == "LEFT"
        assert para_breaks[para_ref] == "1"
        assert para_heading_types[para_ref] != "OUTLINE"
    # 강제 새 페이지의 첫 제목은 추가 위쪽 여백을 두지 않는다.
    assert para_spacing[section_headings[0].get("paraPrIDRef")]["prev"] == "0"
    assert para_spacing[section_headings[3].get("paraPrIDRef")]["prev"] == "0"

    for prefix in ("01   국민기초생활보장법", "■  개정 전·후 상세 비교", "변경 01"):
        paragraph = next(node for node in paragraphs if paragraph_text(node).startswith(prefix))
        assert para_breaks[paragraph.get("paraPrIDRef")] == "1"

    title = next(node for node in paragraphs if paragraph_text(node) == "주간 법령개정 분류·요약 보고서")
    assert para_align[title.get("paraPrIDRef")] == "CENTER"

    law_title = next(node for node in paragraphs if paragraph_text(node).startswith("01   국민기초생활보장법"))
    title_runs = [node for node in law_title if node.tag.rsplit("}", 1)[-1] == "run"]
    assert [char_height[run.get("charPrIDRef")] for run in title_runs] == [1050, 1400]

    tables = [node for node in section.iter() if node.tag.rsplit("}", 1)[-1] == "tbl"]
    assert tables
    for table in tables:
        size = next(
            child for child in table
            if child.tag.rsplit("}", 1)[-1] == "sz"
        )
        assert size.get("width") == "48600"
        assert table.get("repeatHeader") == "1"

    # 키-값 표는 첫 열을 가운데, 긴 내용 열을 왼쪽으로 정렬한다.
    overview_table = next(
        table for table in tables
        if any(paragraph_text(node) == "보고기간" for node in table.iter()
               if node.tag.rsplit("}", 1)[-1] == "p")
    )
    body_cells = {}
    for cell in (node for node in overview_table.iter() if node.tag.rsplit("}", 1)[-1] == "tc"):
        address = next(
            (child for child in cell if child.tag.rsplit("}", 1)[-1] == "cellAddr"),
            None,
        )
        if address is not None and address.get("rowAddr") == "1":
            body_cells[int(address.get("colAddr"))] = cell
    key_para = next(node for node in body_cells[0].iter() if node.tag.rsplit("}", 1)[-1] == "p")
    value_para = next(node for node in body_cells[1].iter() if node.tag.rsplit("}", 1)[-1] == "p")
    assert para_align[key_para.get("paraPrIDRef")] == "CENTER"
    assert para_align[value_para.get("paraPrIDRef")] == "LEFT"

    # 페이지 나눔용 빈 문단이 새 쪽 상단에서 10pt 한 줄을 차지하지 않는다.
    page_break_paragraphs = [
        node for node in paragraphs
        if node.get("pageBreak") == "1" and not paragraph_text(node).strip()
    ]
    assert page_break_paragraphs
    for paragraph in page_break_paragraphs:
        para_ref = paragraph.get("paraPrIDRef")
        assert para_spacing[para_ref]["prev"] == "0"
        assert para_spacing[para_ref]["next"] == "0"
        runs = [node for node in paragraph if node.tag.rsplit("}", 1)[-1] == "run"]
        assert all(char_height[run.get("charPrIDRef")] == 100 for run in runs)

    page_margin = next(
        node for node in section.iter()
        if node.tag.rsplit("}", 1)[-1] == "margin"
        and {"top", "bottom", "header", "footer"}.issubset(node.attrib)
    )
    assert int(page_margin.get("top")) < 5102
    assert int(page_margin.get("header")) < 2268


def test_locked_existing_report_is_saved_with_incremented_name(tmp_path, monkeypatch):
    output = tmp_path / "weekly_law_report_2026-07-22.hwpx"
    original_save = HwpxDocument.save_to_path

    def fail_only_for_requested_name(document, path, *args, **kwargs):
        if path == output:
            raise PermissionError(5, "file is in use", str(path))
        return original_save(document, path, *args, **kwargs)

    monkeypatch.setattr(HwpxDocument, "save_to_path", fail_only_for_requested_name)

    result = write_weekly_hwpx(_contract(), output)
    fallback = tmp_path / "weekly_law_report_2026-07-22_2.hwpx"

    assert result["path"] == str(fallback)
    assert fallback.exists()
    assert validate_editor_open_safety(fallback).to_dict()["ok"] is True
