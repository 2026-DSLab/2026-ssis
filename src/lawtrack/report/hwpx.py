"""WeeklyContract JSON을 읽기 쉬운 주간 법령개정 HWPX 보고서로 변환한다."""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from hwpx import (
    DocumentStylePreset,
    create_document_from_plan,
    inspect_document_authoring_quality,
    validate_document_plan,
    validate_editor_open_safety,
)

from lawtrack.contract.schema import LawChange, LawLLMSummary, WeeklyContract


REPORT_TITLE = "주간 법령개정 분류·요약 보고서"
REPORT_SYSTEM = "법령개정 자동감지 시스템"
TYPE_COLUMNS = ("법률", "시행령", "시행규칙", "행정규칙")
_REPORT_TABLE_WIDTH = 48_600  # 18mm 좌우 여백 A4 본문 폭 안에서 약 171.5mm
REPORT_STYLE = DocumentStylePreset(
    name="lawtrack_weekly_report",
    title_size=22,
    subtitle_size=10,
    heading_size=14,
    body_size=11,
    meta_size=9,
    font="함초롬돋움",
    title_color="#17365D",
    heading_color="#1F4E78",
    subtitle_color="#5B6573",
    meta_color="#5B6573",
    rule_color="#8EA9C1",
    # 한글의 개요 문단 아래쪽 테두리는 다음 문단과 겹쳐 보이는 경우가 있다.
    # 주제목은 번호가 포함된 일반 문단으로 직접 그리므로 자동 테두리를 쓰지 않는다.
    heading_rule=False,
)

# python-hwpx 골격 탐색 과정의 비차단 경고는 숨기고 최종 safety 결과로 판정한다.
logging.getLogger("hwpx.opc.package").setLevel(logging.ERROR)


class ReportBuildError(RuntimeError):
    """보고서 계획 또는 HWPX 패키지 생성 실패."""


def load_contract_json(path: str | Path) -> WeeklyContract:
    """UTF-8 JSON 파일을 읽고 WeeklyContract 스키마로 검증한다."""
    source = Path(path)
    try:
        return WeeklyContract.model_validate_json(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReportBuildError(f"JSON 파일을 읽을 수 없습니다: {source}") from exc
    except ValueError as exc:
        raise ReportBuildError(f"WeeklyContract JSON 형식이 아닙니다: {source}: {exc}") from exc


def build_weekly_report_plan(
    contract: WeeklyContract,
    *,
    title: str = REPORT_TITLE,
    author: str = "(내부 기재)",
    department: str = "(내부 기재)",
    manager: str = "(내부 기재)",
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """긴 본문을 좁은 다열 표에 넣지 않는 세로형 보고서 계획을 만든다."""
    generated_at = generated_at or datetime.now()
    laws = _flatten_laws(contract)
    law_type_counts = Counter(_type_bucket(item["law"].law_type) for item in laws)
    summary_lookup = _llm_summary_lookup(contract)
    changed_position_count = sum(
        len(item["law"].articles)
        + sum(len(group.new_items) for group in item["law"].structural_expansions)
        for item in laws
    )
    review_required_count = (
        len(contract.unresolved)
        + sum(
            sum(
                article.match_status not in ("성공", "삭제(위치탐색제외)")
                for article in item["law"].articles
            )
            + len(item["law"].structural_expansions)
            for item in laws
        )
    )

    blocks: list[dict[str, Any]] = [
        {
            "type": "paragraph",
            "runs": [
                {"text": "WEEKLY LEGAL UPDATE", "bold": True, "color": "#2E75B6"},
                {"text": "  |  자동감지 · 구조화 비교 · AI 요약", "color": "#687386"},
            ],
        },
        {
            "type": "paragraph",
            "style": "subtitle",
            "text": (
                f"보고기간  {_date_text(contract.period.from_date)} ~ "
                f"{_date_text(contract.period.to_date)}"
            ),
        },
        _detail_heading("핵심 요약", size=13),
        {
            "type": "paragraph",
            "text": _executive_summary(contract, laws, changed_position_count),
        },
        _summary_status_table(
            total_laws=len(laws),
            changed_positions=changed_position_count,
            review_required_count=review_required_count,
            no_comparison_count=len(contract.no_comparison),
        ),
    ]

    if contract.llm_summary:
        blocks.append({
            "type": "paragraph",
            "runs": [
                {"text": "AI 요약 정보  ", "bold": True, "color": "#1F4E78"},
                {
                    "text": (
                        f"{_provider_label(contract.llm_summary.provider)} · "
                        f"{contract.llm_summary.model} · "
                        f"{_datetime_text(contract.llm_summary.generated_at)}"
                    ),
                    "color": "#687386",
                },
            ],
        })
    else:
        blocks.append({
            "type": "paragraph",
            "text": "LLM 요약 미사용: 공식 개정이유와 확정된 조문 차이를 규칙 기반으로 정리했습니다.",
        })

    blocks.extend([
        # 1쪽은 표지·핵심 요약만 배치한다. 개요 표를 같은 쪽에 억지로
        # 넣으면 한글의 표 행 단위 페이지 나눔 때문에 다음 절 제목만
        # 쪽 하단에 고립되는 현상이 생긴다.
        {"type": "page_break"},
        _section_heading(1, "보고 개요"),
        _table(
            [("key", "항목", 1), ("value", "내용", 4)],
            [
                {"key": "보고기간", "value": f"{_date_text(contract.period.from_date)} ~ {_date_text(contract.period.to_date)}"},
                {"key": "배치 기준일", "value": _date_text(contract.batch_date)},
                {"key": "생성일시", "value": generated_at.strftime("%Y.%m.%d. %H:%M")},
                {"key": "작성주기", "value": "주 1회"},
                {"key": "관리부서", "value": department},
                {"key": "데이터 기준", "value": "국가법령정보 Open API 및 WeeklyContract JSON"},
            ],
            caption="",
        ),
        _section_heading(2, "개정 현황"),
        _type_summary_table(law_type_counts, len(laws)),
        _section_heading(3, "법령별 한눈에 보기"),
    ])

    if laws:
        blocks.append(_law_index_table(contract, laws))
    else:
        blocks.append({
            "type": "paragraph",
            "text": "이번 배치에서 신규 감지된 법령 버전이 없습니다.",
        })

    blocks.extend([
        {"type": "page_break"},
        _section_heading(4, "법령별 상세"),
    ])
    if laws:
        for index, item in enumerate(laws, 1):
            if index > 1:
                blocks.append({"type": "page_break"})
            blocks.extend(
                _law_detail_blocks(
                    contract,
                    item,
                    index,
                    summary_lookup.get((item["law"].law_id, item["law"].new_serial_no)),
                )
            )
    else:
        blocks.append({"type": "paragraph", "text": "상세 개정 법령이 없습니다."})

    # 마지막 법령의 긴 비교표 뒤에 확인 필요 사항이 어중간하게 붙지 않게
    # 최종 검토·관리 페이지를 별도로 시작한다.
    blocks.append({"type": "page_break"})
    blocks.extend(_review_blocks(contract))
    blocks.extend([
        _section_heading(6, "참고 및 관리 정보"),
        {
            "type": "bullets",
            "items": [
                "보고기간은 배치 실행 주기를 표시하는 메타데이터이며, 목록에는 이번 배치에서 신규 감지된 버전을 담습니다.",
                "시행일이 보고기간 밖이어도 이번 배치에서 신규 감지된 버전이면 보고서에 포함됩니다.",
                "개정 전·후 문장은 국가법령정보 Open API 원문과 신구조문대비표를 기반으로 합니다.",
                "AI 요약은 확정된 JSON 사실을 읽기 쉽게 정리한 참고자료이며 법률 해석을 대신하지 않습니다.",
                "위치재배치의심·구조확장·미확정 항목은 담당자가 법제처 원문과 대조해야 합니다.",
            ],
        },
        _table(
            [("key", "항목", 1), ("value", "내용", 4)],
            [
                {"key": "생성자", "value": author},
                {"key": "검토자", "value": manager},
                {"key": "계약 버전", "value": contract.contract_version},
                {"key": "요약 방식", "value": _summary_method(contract)},
                {"key": "비고", "value": "메일 자동 발송 연계 예정"},
            ],
            caption="",
        ),
    ])

    plan = {
        "schemaVersion": "hwpx.document_plan.v1",
        "title": title,
        "blocks": blocks,
        "qualityGates": {
            "validatePackage": True,
            "validateDocument": True,
            "reopen": True,
            "minTableCount": 3,
            "requiredText": ["핵심 요약", "보고 개요", "법령별 한눈에 보기", "참고 및 관리 정보"],
            "visualReviewRequired": True,
        },
    }
    validation = validate_document_plan(plan)
    if not validation.ok:
        issues = validation.to_dict().get("issues", [])
        detail = "; ".join(issue.get("message", str(issue)) for issue in issues)
        raise ReportBuildError(f"HWPX 문서 계획 검증 실패: {detail}")
    return plan


def write_weekly_hwpx(
    contract: WeeklyContract,
    output_path: str | Path,
    *,
    title: str = REPORT_TITLE,
    author: str = "(내부 기재)",
    department: str = "(내부 기재)",
    manager: str = "(내부 기재)",
) -> dict[str, Any]:
    """보고서를 저장하고 패키지·재열기·구조 품질 결과를 반환한다."""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    plan = build_weekly_report_plan(
        contract,
        title=title,
        author=author,
        department=department,
        manager=manager,
    )
    document = create_document_from_plan(plan, preset=REPORT_STYLE)
    try:
        document.set_page_setup(
            margins_mm={
                "left": 18,
                "right": 18,
                "top": 14,
                "bottom": 16,
                "header": 6,
                "footer": 8,
            }
        )
        document.set_header_text(
            f"주간 법령개정 보고서  |  {_date_text(contract.period.from_date)} ~ {_date_text(contract.period.to_date)}"
        )
        document.set_footer_text(f"{REPORT_SYSTEM}  |  내부 업무 참고용")
        _normalize_hancom_layout(document, title=title)
        try:
            document.save_to_path(target)
        except PermissionError:
            # 한글에서 기존 보고서를 열어둔 경우 Windows가 원자적 교체를 거부한다.
            # 배치 결과를 잃지 않도록 기존 파일은 그대로 두고 새 번호로 저장한다.
            locked_target = target
            target = _next_report_path(locked_target)
            logging.getLogger(__name__).warning(
                "기존 HWPX 파일이 사용 중이므로 새 이름으로 저장합니다: %s -> %s",
                locked_target,
                target,
            )
            try:
                document.save_to_path(target)
            except PermissionError as fallback_exc:
                raise ReportBuildError(
                    "HWPX 저장 권한이 거부되었습니다. 한글에서 열려 있는 보고서를 닫거나 "
                    f"출력 폴더 권한을 확인하세요: {locked_target}"
                ) from fallback_exc
    finally:
        document.close()

    safety = validate_editor_open_safety(target)
    safety_data = safety.to_dict()
    if not safety_data.get("ok", False):
        raise ReportBuildError(f"생성된 HWPX 재열기 검증 실패: {safety_data}")

    quality = inspect_document_authoring_quality(target, plan=plan, verify_render=False)
    return {
        "path": str(target),
        "size": target.stat().st_size,
        "safety": safety_data,
        "quality": quality,
    }


def _next_report_path(path: Path) -> Path:
    """기존 보고서를 덮어쓰지 않는 첫 번째 ``_N`` 경로를 반환한다."""
    for number in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ReportBuildError(f"대체 HWPX 파일명을 만들 수 없습니다: {path.parent}")


def _normalize_hancom_layout(document: Any, *, title: str) -> None:
    """한글에서 짧은 문장이 강제로 벌어지는 HWPX 서식을 교정한다.

    문서 계획 렌더러의 기본 문단 서식은 양쪽 정렬이며, 일부 사용자 지정
    run 크기는 기존 제목 charPr를 재사용한다. 웹 미리보기에서는 티가 나지
    않지만 한글은 이 값을 그대로 적용하므로 저장 직전에 실제 XML 참조를
    왼쪽 정렬 및 보고서용 글자 크기로 정규화한다.
    """
    headers = document.oxml.headers
    if not headers:
        return
    header = headers[0]
    formatted_para_pr: dict[tuple[Any, ...], str] = {}
    resized_char_pr: dict[tuple[str, int], str] = {}

    def formatted_ref(
        base_ref: str | int | None,
        *,
        alignment: str = "left",
        line_spacing: int = 160,
        before_pt: float = 0,
        after_pt: float = 5,
        keep_with_next: bool = False,
        keep_lines: bool = False,
        left_hwpunit: int = 0,
    ) -> str:
        base = str(base_ref or "0")
        key = (
            base,
            alignment,
            line_spacing,
            before_pt,
            after_pt,
            keep_with_next,
            keep_lines,
            left_hwpunit,
        )
        if key not in formatted_para_pr:
            formatted_para_pr[key] = header.ensure_paragraph_format(
                base_para_pr_id=base,
                alignment=alignment,
                line_spacing_percent=line_spacing,
                margins={
                    "left": left_hwpunit,
                    "prev": int(round(before_pt * 100)),
                    "next": int(round(after_pt * 100)),
                },
                break_setting={
                    "keep_with_next": keep_with_next,
                    "keep_lines": keep_lines,
                    "widow_orphan": True,
                },
            )
        return formatted_para_pr[key]

    def resized_ref(base_ref: str | int | None, size_pt: float) -> str:
        base = str(base_ref or "0")
        height = int(round(size_pt * 100))
        key = (base, height)
        if key not in resized_char_pr:
            element = header.ensure_char_property(
                base_char_pr_id=base,
                modifier=lambda char_pr, value=height: char_pr.set("height", str(value)),
            )
            resized_char_pr[key] = str(element.get("id"))
        return resized_char_pr[key]

    for section in document.oxml.sections:
        previous_text = ""
        for paragraph in section.paragraphs:
            text = paragraph.text.strip()
            if text == title:
                paragraph.element.set(
                    "paraPrIDRef",
                    formatted_ref(
                        paragraph.para_pr_id_ref,
                        alignment="center",
                        line_spacing=135,
                        after_pt=9,
                        keep_lines=True,
                    ),
                )
            else:
                paragraph.element.set("paraPrIDRef", _paragraph_layout_ref(
                    text,
                    previous_text=previous_text,
                    base_ref=paragraph.para_pr_id_ref,
                    formatted_ref=formatted_ref,
                ))
                _normalize_hancom_run_sizes(
                    paragraph.element,
                    text=text,
                    previous_text=previous_text,
                    resized_ref=resized_ref,
                    header_element=header.element,
                )
            if text:
                previous_text = text

        _normalize_table_layout(
            section.element,
            formatted_ref=formatted_ref,
            target_width=_REPORT_TABLE_WIDTH,
        )
        _compact_page_break_paragraphs(
            section.element,
            formatted_ref=formatted_ref,
            resized_ref=resized_ref,
        )
        section.mark_dirty()


def _paragraph_layout_ref(
    text: str,
    *,
    previous_text: str,
    base_ref: str | int | None,
    formatted_ref: Any,
) -> str:
    """문단 역할별로 정렬·행간·위아래 간격을 일관되게 적용한다."""
    if text.startswith("WEEKLY LEGAL UPDATE"):
        return formatted_ref(
            base_ref, alignment="center", line_spacing=135, after_pt=2, keep_lines=True,
        )
    if text.startswith("보고기간") and previous_text.startswith("WEEKLY LEGAL UPDATE"):
        return formatted_ref(
            base_ref, alignment="center", line_spacing=140, after_pt=11, keep_lines=True,
        )
    if re.match(r"^[1-6]\.\s{2}", text):
        return formatted_ref(
            base_ref,
            line_spacing=145,
            before_pt=10,
            after_pt=5,
            keep_with_next=True,
            keep_lines=True,
        )
    if re.match(r"^\d{2}\s{3}", text):
        return formatted_ref(
            base_ref,
            line_spacing=145,
            before_pt=2,
            after_pt=7,
            keep_with_next=True,
            keep_lines=True,
        )
    if text.startswith("■"):
        return formatted_ref(
            base_ref,
            line_spacing=145,
            before_pt=8,
            after_pt=4,
            keep_with_next=True,
            keep_lines=True,
        )
    if re.match(r"^변경\s+\d{2}", text):
        return formatted_ref(
            base_ref,
            line_spacing=145,
            before_pt=8,
            after_pt=3,
            keep_with_next=True,
            keep_lines=True,
        )
    if text.startswith("AI 요약 정보"):
        return formatted_ref(base_ref, line_spacing=140, before_pt=5, after_pt=0)
    if text.startswith(("법제처 원문", "업무 영향", "검토 포인트", "현행 유지 조항")):
        return formatted_ref(
            base_ref,
            line_spacing=150,
            before_pt=6,
            after_pt=3,
            keep_with_next=text.startswith(("법제처 원문", "검토 포인트")),
            keep_lines=True,
        )
    if text.startswith(("•", "※")):
        return formatted_ref(
            base_ref,
            line_spacing=155,
            after_pt=3,
            keep_lines=True,
            left_hwpunit=350,
        )
    if text.startswith(("http://", "https://")):
        return formatted_ref(base_ref, line_spacing=140, after_pt=5, keep_lines=True)
    return formatted_ref(base_ref, line_spacing=160, after_pt=5)


def _normalize_table_layout(
    section_element: Any,
    *,
    formatted_ref: Any,
    target_width: int,
) -> None:
    """표를 본문 폭에 맞추고 표 종류별로 데이터 셀 정렬을 적용한다."""
    for table in (
        node for node in section_element.iter()
        if _xml_local_name(node.tag) == "tbl"
    ):
        table.set("repeatHeader", "1")
        size = next(
            (child for child in table if _xml_local_name(child.tag) == "sz"),
            None,
        )
        old_width = int(size.get("width", "0")) if size is not None else 0
        if size is not None and old_width > 0 and old_width != target_width:
            size.set("width", str(target_width))
            scale = target_width / old_width
            for cell_size in (
                node for node in table.iter()
                if _xml_local_name(node.tag) == "cellSz"
            ):
                width = int(cell_size.get("width", "0"))
                if width > 0:
                    cell_size.set("width", str(int(round(width * scale))))

        cells: dict[tuple[int, int], Any] = {}
        for cell in (
            node for node in table.iter()
            if _xml_local_name(node.tag) == "tc"
        ):
            address = next(
                (child for child in cell if _xml_local_name(child.tag) == "cellAddr"),
                None,
            )
            if address is None:
                continue
            cells[(int(address.get("rowAddr", "0")), int(address.get("colAddr", "0")))] = cell

        headers = {
            col: _xml_text(cell).strip()
            for (row, col), cell in cells.items()
            if row == 0
        }
        center_columns = _centered_body_columns(headers)
        for (row, col), cell in cells.items():
            if row == 0:
                alignment = "center"
            else:
                alignment = "center" if col in center_columns else "left"
            for paragraph in (
                node for node in cell.iter()
                if _xml_local_name(node.tag) == "p"
            ):
                paragraph.set(
                    "paraPrIDRef",
                    formatted_ref(
                        paragraph.get("paraPrIDRef"),
                        alignment=alignment,
                        line_spacing=150,
                        after_pt=0,
                        keep_lines=True,
                    ),
                )


def _centered_body_columns(headers: dict[int, str]) -> set[int]:
    """표 머리글을 기준으로 가운데 정렬할 데이터 열을 고른다."""
    labels = [headers[index] for index in sorted(headers)]
    if labels in (
        ["신규 감지 법령", "변경 위치", "원문 대조 필요", "비교불가"],
        ["전체", "법률", "시행령", "시행규칙", "행정규칙"],
    ):
        return set(headers)
    if labels == ["No.", "구분", "법령명", "시행일", "변경 규모", "검토"]:
        return {0, 1, 3, 4, 5}
    if labels in (
        ["항목", "내용"],
        ["구분", "내용"],
        ["검토 상태", "확인 결과"],
        ["법령명", "확인 내용"],
    ):
        return {0}
    return set()


def _compact_page_break_paragraphs(
    section_element: Any,
    *,
    formatted_ref: Any,
    resized_ref: Any,
) -> None:
    """강제 페이지 나눔용 빈 문단이 다음 쪽 첫 줄 높이를 차지하지 않게 한다."""
    paragraphs = [
        node for node in section_element.iter()
        if _xml_local_name(node.tag) == "p"
    ]
    for index, paragraph in enumerate(paragraphs):
        if paragraph.get("pageBreak") != "1" or _xml_text(paragraph).strip():
            continue
        paragraph.set(
            "paraPrIDRef",
            formatted_ref(
                paragraph.get("paraPrIDRef"),
                line_spacing=100,
                before_pt=0,
                after_pt=0,
                keep_lines=False,
            ),
        )
        for run in (
            node for node in paragraph
            if _xml_local_name(node.tag) == "run"
        ):
            run.set("charPrIDRef", resized_ref(run.get("charPrIDRef"), 1.0))

        # 페이지 첫 제목에는 평상시 절 사이에서 쓰는 8~10pt 위쪽 간격이
        # 필요 없다. 빈 나눔 문단 다음의 첫 실제 문단만 위쪽 간격을 0으로 둔다.
        next_paragraph = next(
            (
                node for node in paragraphs[index + 1:]
                if _xml_text(node).strip()
            ),
            None,
        )
        if next_paragraph is None:
            continue
        next_text = _xml_text(next_paragraph).strip()
        if re.match(r"^[1-6]\.\s{2}", next_text):
            next_paragraph.set(
                "paraPrIDRef",
                formatted_ref(
                    next_paragraph.get("paraPrIDRef"),
                    line_spacing=145,
                    before_pt=0,
                    after_pt=5,
                    keep_with_next=True,
                    keep_lines=True,
                ),
            )
        elif re.match(r"^\d{2}\s{3}", next_text):
            next_paragraph.set(
                "paraPrIDRef",
                formatted_ref(
                    next_paragraph.get("paraPrIDRef"),
                    line_spacing=145,
                    before_pt=0,
                    after_pt=7,
                    keep_with_next=True,
                    keep_lines=True,
                ),
            )


def _xml_text(element: Any) -> str:
    return "".join(
        node.text or ""
        for node in element.iter()
        if _xml_local_name(node.tag) == "t"
    )


def _keep_with_next(text: str) -> bool:
    """다음 내용과 같은 페이지에 있어야 하는 보고서 문단인지 판정한다."""
    return bool(
        re.match(r"^[1-6]\.\s{2}", text)
        or re.match(r"^\d{2}\s{3}", text)
        or re.match(r"^변경\s+\d{2}", text)
        or text.startswith("■")
    )


def _normalize_hancom_run_sizes(
    paragraph: Any,
    *,
    text: str,
    previous_text: str,
    resized_ref: Any,
    header_element: Any,
) -> None:
    runs = [node for node in paragraph if _xml_local_name(node.tag) == "run"]
    text_runs = [
        run
        for run in runs
        if "".join(
            (node.text or "")
            for node in run.iter()
            if _xml_local_name(node.tag) == "t"
        )
    ]
    if not text_runs:
        return

    sizes: list[float] | None = None
    if text.startswith("WEEKLY LEGAL UPDATE"):
        sizes = [9.5] * len(text_runs)
    elif text.startswith("AI 요약 정보"):
        sizes = [10.5, 9.5]
    elif re.match(r"^\d{2}\s{3}", text):
        sizes = [10.5, 14.0]
    elif text.startswith("■"):
        sizes = [12.0] * len(text_runs)
    elif re.match(r"^변경\s+\d{2}", text):
        sizes = [10.5] * len(text_runs)
    elif text.startswith("법제처 원문"):
        sizes = [10.5] * len(text_runs)
    elif text.startswith(("http://", "https://")):
        sizes = [8.5] * len(text_runs)
    elif previous_text.startswith("■  AI 업무 요약"):
        sizes = [12.0] * len(text_runs)
    elif text.startswith(("업무 영향", "검토 포인트", "현행 유지 조항")):
        sizes = [10.5] * len(text_runs)

    if sizes is not None:
        for index, run in enumerate(text_runs):
            size = sizes[min(index, len(sizes) - 1)]
            run.set("charPrIDRef", resized_ref(run.get("charPrIDRef"), size))
        return

    # 계획 렌더러가 11pt 본문용 강조 run에 24pt 제목 서식을 재사용하는 경우를 차단한다.
    heights = {
        node.get("id"): int(node.get("height", "0"))
        for node in header_element.iter()
        if _xml_local_name(node.tag) == "charPr"
    }
    for run in text_runs:
        base_ref = run.get("charPrIDRef")
        if heights.get(base_ref, 0) > 1600:
            run.set("charPrIDRef", resized_ref(base_ref, 11.0))


def _xml_local_name(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _flatten_laws(contract: WeeklyContract) -> list[dict[str, Any]]:
    return [
        {
            "law": law,
            "promulgation_no": group.promulgation_no,
            "promulgation_date": group.promulgation_date,
            "group_revision_type": group.revision_type,
        }
        for group in contract.amendment_groups
        for law in group.laws
    ]


def _llm_summary_lookup(contract: WeeklyContract) -> dict[tuple[str, str], LawLLMSummary]:
    if not contract.llm_summary:
        return {}
    return {
        (item.law_id, item.new_serial_no): item
        for item in contract.llm_summary.law_summaries
    }


def _executive_summary(
    contract: WeeklyContract,
    laws: list[dict[str, Any]],
    changed_position_count: int,
) -> str:
    if contract.llm_summary and _clean(contract.llm_summary.executive_summary):
        return _clean(contract.llm_summary.executive_summary)
    if not laws:
        return "이번 배치에서 신규 감지된 법령 버전이 없습니다. 비교불가 또는 미확정 항목은 아래 확인 필요 사항을 참조하십시오."
    return (
        f"이번 배치에서 신규 감지된 법령 버전은 {len(laws)}건이며, 총 {changed_position_count}개 변경 위치가 확인되었습니다. "
        f"위치확정 미완료 {len(contract.unresolved)}건, 신구법 비교불가 {len(contract.no_comparison)}건은 "
        "담당자의 원문 확인이 필요합니다."
    )


def _summary_status_table(
    *,
    total_laws: int,
    changed_positions: int,
    review_required_count: int,
    no_comparison_count: int,
) -> dict[str, Any]:
    return _table(
        [
            ("laws", "신규 감지 법령", 1),
            ("positions", "변경 위치", 1),
            ("review", "원문 대조 필요", 1),
            ("no_comparison", "비교불가", 1),
        ],
        [{
            "laws": f"{total_laws}건",
            "positions": f"{changed_positions}건",
            "review": f"{review_required_count}건",
            "no_comparison": f"{no_comparison_count}건",
        }],
        caption="",
    )


def _type_summary_table(type_counts: Counter, total_laws: int) -> dict[str, Any]:
    return _table(
        [("total", "전체", 1), *[(f"type_{i}", label, 1) for i, label in enumerate(TYPE_COLUMNS)]],
        [{
            "total": f"{total_laws}건",
            **{f"type_{i}": f"{type_counts.get(label, 0)}건" for i, label in enumerate(TYPE_COLUMNS)},
        }],
        # 표 caption은 생성기에서 본문 제목과 같은 크기로 렌더링되고,
        # 표와 함께 묶이지 않아 페이지 하단에 caption만 남을 수 있다.
        # 절 제목이 이미 있으므로 중복 caption을 사용하지 않는다.
        caption="",
    )


def _law_index_table(contract: WeeklyContract, laws: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for index, item in enumerate(laws, 1):
        law: LawChange = item["law"]
        positions = len(law.articles) + sum(len(group.new_items) for group in law.structural_expansions)
        rows.append({
            "no": str(index),
            "type": _type_bucket(law.law_type),
            "name": law.law_name,
            "enforced": _date_text(law.enforce_date),
            "scope": f"변경 {positions}건",
            "review": _review_badge(contract, law),
        })
    return _table(
        [
            ("no", "No.", 1),
            ("type", "구분", 1),
            ("name", "법령명", 4),
            ("enforced", "시행일", 2),
            ("scope", "변경 규모", 2),
            ("review", "검토", 2),
        ],
        rows,
        caption="",
    )


def _law_detail_blocks(
    contract: WeeklyContract,
    item: dict[str, Any],
    index: int,
    ai_summary: LawLLMSummary | None,
) -> list[dict[str, Any]]:
    law: LawChange = item["law"]
    name = law.law_name
    if law.internal_name and law.internal_name != name:
        name = f"{name} (내부명: {law.internal_name})"
    blocks: list[dict[str, Any]] = [
        _law_title(index, name),
        _detail_heading("개정 개요", size=12),
        _table(
            [("key", "항목", 1), ("value", "내용", 4)],
            [
                {"key": "구분", "value": _type_bucket(law.law_type)},
                {"key": "공포·발령일 / 시행일", "value": f"{_date_text(item['promulgation_date'])} / {_date_text(law.enforce_date)}"},
                {"key": "개정유형", "value": law.revision_type or item["group_revision_type"] or "-"},
                {"key": "공포번호", "value": item["promulgation_no"] or "-"},
                {"key": "변경 규모", "value": _change_scope(law)},
            ],
            caption="",
        ),
    ]
    blocks.extend(_summary_blocks(law, ai_summary))

    if law.articles:
        blocks.append(_detail_heading("개정 전·후 상세 비교"))
        for article_index, article in enumerate(law.articles, 1):
            position = _position(
                article.article_label,
                article.clause_no,
                article.item_label,
                article.subitem_label,
            )
            blocks.extend([
                _change_label(article_index, position, article.change_type, article.match_status),
                _before_after_table(article.old_text or "(신설)", article.new_text or "(삭제)"),
            ])

    if law.structural_expansions:
        blocks.append(_detail_heading("구조확장 변경"))
        blocks.append({
            "type": "paragraph",
            "text": "아래 항목은 구법의 한 문장이 여러 새 위치로 확장된 1:N 변경입니다. 개별 행을 정확한 1:1 대응으로 해석하지 마십시오.",
        })
        for expansion in law.structural_expansions:
            blocks.extend([
                {
                    "type": "paragraph",
                    "runs": [
                        {"text": expansion.article_label, "bold": True, "color": "#1F4E78"},
                        {"text": "   구조확장 · 원문 대조 필요", "color": "#C65911"},
                    ],
                },
                _before_after_table(
                    expansion.old_text or "-",
                    "\n".join(_expanded_item_text(new) for new in expansion.new_items) or "-",
                ),
            ])

    if law.unchanged_clauses:
        unchanged = "; ".join(
            f"{article}: {', '.join(labels)}"
            for article, labels in sorted(law.unchanged_clauses.items())
        )
        blocks.append({
            "type": "paragraph",
            "runs": [
                {"text": "현행 유지 조항  ", "bold": True, "color": "#1F4E78"},
                {"text": unchanged},
            ],
        })
    blocks.append({
        "type": "paragraph",
        "runs": [
            {"text": "법제처 원문", "bold": True, "color": "#1F4E78"},
            {"text": f"  |  국가법령정보센터 · 일련번호 {law.new_serial_no}", "color": "#687386"},
        ],
    })
    blocks.append({"type": "paragraph", "text": law.source_url or "-", "style": "meta"})
    return blocks


def _summary_blocks(law: LawChange, ai_summary: LawLLMSummary | None) -> list[dict[str, Any]]:
    if not ai_summary:
        return [
            _detail_heading("주요 개정 내용"),
            {"type": "paragraph", "text": _major_summary(law, limit=900)},
            {
                "type": "paragraph",
                "text": "※ LLM 요약이 없어 공식 개정이유 또는 확정된 조문 차이를 표시했습니다.",
            },
        ]
    blocks: list[dict[str, Any]] = [
        _detail_heading("AI 업무 요약"),
        {
            "type": "paragraph",
            "runs": [{"text": ai_summary.headline, "bold": True, "color": "#1F4E78"}],
        },
        {"type": "paragraph", "text": ai_summary.summary},
    ]
    if ai_summary.key_changes:
        blocks.append({"type": "bullets", "items": ai_summary.key_changes})
    blocks.append({
        "type": "paragraph",
        "runs": [
            {"text": "업무 영향  ", "bold": True, "color": "#1F4E78"},
            {"text": ai_summary.operational_impact or "담당 부서의 원문 검토 필요"},
        ],
    })
    if ai_summary.review_points:
        blocks.extend([
            {"type": "paragraph", "runs": [{"text": "검토 포인트", "bold": True, "color": "#C65911"}]},
            {"type": "bullets", "items": ai_summary.review_points},
        ])
    return blocks


def _law_title(index: int, name: str) -> dict[str, Any]:
    """개별 법령 제목을 개요 번호와 분리해 자동 개요번호 중복을 막는다."""
    return {
        "type": "paragraph",
        "runs": [
            {
                "text": f"{index:02d}",
                "bold": True,
                "color": "#FFFFFF",
                "highlight": "#1F4E78",
                "size": 13,
            },
            {"text": f"   {name}", "bold": True, "color": "#17365D", "size": 14},
        ],
    }


def _section_heading(number: int, text: str) -> dict[str, Any]:
    """한글 자동 개요번호 대신 고정 번호를 사용하는 안정적인 주제목.

    ``개요 1`` 스타일은 한글에서 번호와 제목이 서로 다른 줄이나 페이지로
    갈라지는 경우가 있어 일반 문단으로 렌더링한다.
    """
    return {
        "type": "paragraph",
        "runs": [
            {
                "text": f"{number}.  {text}",
                "bold": True,
                "color": "#1F4E78",
                "size": 14,
            },
        ],
    }


def _detail_heading(text: str, *, size: int = 13) -> dict[str, Any]:
    """문서 개요번호를 소비하지 않는 법령 상세용 소제목."""
    return {
        "type": "paragraph",
        "runs": [
            {"text": "■  ", "bold": True, "color": "#2E75B6", "size": size},
            {"text": text, "bold": True, "color": "#17365D", "size": size},
        ],
    }


def _change_label(index: int, position: str, change_type: str, match_status: str) -> dict[str, Any]:
    """긴 비교표 앞에서 위치와 판정 상태를 한눈에 구분해 보여준다."""
    status_color = "#C65911" if match_status not in ("성공", "삭제(위치탐색제외)") else "#687386"
    return {
        "type": "paragraph",
        "runs": [
            {"text": f"변경 {index:02d}", "bold": True, "color": "#2E75B6"},
            {"text": f"  |  {position}", "bold": True, "color": "#17365D"},
            {"text": f"  |  {change_type} · {match_status}", "color": status_color},
        ],
    }


def _before_after_table(old_text: str, new_text: str) -> dict[str, Any]:
    return _table(
        [("side", "구분", 1), ("text", "내용", 7)],
        [
            {"side": "개정 전", "text": _presentation_text(old_text)},
            {"side": "개정 후", "text": _presentation_text(new_text)},
        ],
        caption="",
    )


def _review_blocks(contract: WeeklyContract) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        _section_heading(5, "확인 필요 사항"),
    ]
    manual_rows = _manual_review_rows(contract)
    if not contract.unresolved and not contract.no_comparison and not manual_rows:
        blocks.append(
            _table(
                [("status", "검토 상태", 1), ("detail", "확인 결과", 4)],
                [{
                    "status": "이상 없음",
                    "detail": "위치재배치의심·구조확장·위치확정 실패·신구법 비교불가 항목이 없습니다.",
                }],
                caption="",
            )
        )
        return blocks
    if contract.unresolved:
        blocks.extend([
            _detail_heading("위치확정 미완료"),
            _table(
                [("name", "법령명", 2), ("detail", "확인 내용", 5)],
                [{
                    "name": item.law_name,
                    "detail": f"[{item.reason}] {item.detail or '; '.join(item.guards_tried) or '원문 수동 대조 필요'}\n{item.source_url or '-'}",
                } for item in contract.unresolved],
                caption="",
            ),
        ])
    if contract.no_comparison:
        blocks.extend([
            _detail_heading("신구법 비교불가"),
            _table(
                [("name", "법령명", 2), ("detail", "확인 내용", 5)],
                [{
                    "name": item.law_name,
                    "detail": f"[{item.reason}] {item.note}\n{item.source_url or '-'}",
                } for item in contract.no_comparison],
                caption="",
            ),
        ])
    if manual_rows:
        blocks.extend([
            _detail_heading("자동판정 원문 대조"),
            _table(
                [("name", "법령명", 2), ("detail", "확인 내용", 5)],
                manual_rows,
                caption="",
            ),
        ])
    return blocks


def _manual_review_rows(contract: WeeklyContract) -> list[dict[str, str]]:
    """위치재배치의심·구조확장을 법령별 원문 대조 목록으로 만든다."""
    rows: list[dict[str, str]] = []
    for group in contract.amendment_groups:
        for law in group.laws:
            details = [
                f"{article.location_label}: {article.match_status}"
                for article in law.articles
                if article.match_status not in ("성공", "삭제(위치탐색제외)")
            ]
            details.extend(
                f"{expansion.article_label}: 구조확장 {len(expansion.new_items)}위치"
                for expansion in law.structural_expansions
            )
            if not details:
                continue
            rows.append({
                "name": law.law_name,
                "detail": "; ".join(details) + f"\n{law.source_url or '-'}",
            })
    return rows


def _review_badge(contract: WeeklyContract, law: LawChange) -> str:
    if any(item.law_id == law.law_id for item in contract.unresolved):
        return "확인 필요"
    if law.structural_expansions or any(article.match_status not in ("성공", "삭제(위치탐색제외)") for article in law.articles):
        return "원문 대조"
    return "일반"


def _change_scope(law: LawChange) -> str:
    expansion_items = sum(len(group.new_items) for group in law.structural_expansions)
    parts = [f"1:1 조문 {len(law.articles)}건"]
    if law.structural_expansions:
        parts.append(f"구조확장 {len(law.structural_expansions)}그룹/{expansion_items}위치")
    return ", ".join(parts)


def _major_summary(law: LawChange, *, limit: int = 280) -> str:
    reason = _clean(law.revision_reason)
    if reason:
        return _truncate(reason, limit)
    parts = []
    for article in law.articles[:6]:
        position = _position(article.article_label, article.clause_no, article.item_label, article.subitem_label)
        parts.append(f"{position} {article.change_type}: {_truncate(_clean(article.new_text or article.old_text), 120)}")
    for expansion in law.structural_expansions[:3]:
        parts.append(f"{expansion.article_label} 구조확장: {len(expansion.new_items)}개 위치")
    return _truncate("; ".join(parts), limit) or "상세 변경 조문 참조"


def _summary_method(contract: WeeklyContract) -> str:
    if contract.llm_summary:
        provider = _provider_label(contract.llm_summary.provider)
        return f"{provider} Responses API ({contract.llm_summary.model}) + 규칙 기반 사실 검증"
    return "규칙 기반 정리(LLM 요약 미사용)"


def _provider_label(provider: str) -> str:
    return {
        "openai": "OpenAI",
        "openrouter": "OpenRouter",
        "openai-compatible": "OpenAI 호환 API",
    }.get(provider.lower(), provider)


def _table(
    columns: list[tuple[str, str, int]], rows: list[dict[str, Any]], *, caption: str,
) -> dict[str, Any]:
    return {
        "type": "table",
        "caption": caption,
        "columns": [
            {"key": key, "label": label, "widthWeight": weight}
            for key, label, weight in columns
        ],
        "rows": [
            {key: str(row.get(key, "")) for key, _label, _weight in columns}
            for row in rows
        ],
    }


def _type_bucket(law_type: str) -> str:
    value = (law_type or "").strip()
    if value in TYPE_COLUMNS:
        return value
    if value in ("대통령령", "국무총리령"):
        return "시행령"
    if value in ("부령", "총리령"):
        return "시행규칙"
    return "행정규칙" if "행정규칙" in value else "법률"


def _position(article: str, clause: str, item: str, subitem: str) -> str:
    return "".join(part or "" for part in (article, clause, item, subitem)) or "위치 미상"


def _expanded_item_text(item: Any) -> str:
    position = _position("", item.clause_no, item.item_label, item.subitem_label)
    text = _clean(item.text)
    # API 원문은 위치 필드와 본문 양쪽에 '①', '1.', '가.'를 함께 주는 경우가
    # 있다. 보고서에서는 위치를 한 번만 표시해 '①1. 1.' 같은 중복을 없앤다.
    prefixes = [position, item.subitem_label, item.item_label, item.clause_no]
    for prefix in sorted((p for p in prefixes if p and p != "위치 미상"), key=len, reverse=True):
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip()
            break
    return f"• {position} {text}".strip()


def _presentation_text(value: str) -> str:
    """원문 의미는 유지하면서 HWPX 표시에서만 붙어 보이는 경계를 정리한다."""
    text = (value or "").strip()
    text = re.sub(r"(?<=\.)\s*(?=[①-⑳])", "\n", text)
    text = re.sub(r"(?<=[가-힣])\s+다\.", "다.", text)
    return text


def _date_text(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return "-"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw.replace("-", ".") + "."
    return raw


def _datetime_text(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return "-"
    try:
        return datetime.fromisoformat(raw).strftime("%Y.%m.%d. %H:%M")
    except ValueError:
        return raw


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"
