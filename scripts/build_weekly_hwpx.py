"""WeeklyContract JSON에서 주간 법령개정 HWPX 보고서를 생성한다.

사용법:
    python scripts\build_weekly_hwpx.py out\weekly_contract_2026-07-19_d.json
    python scripts\build_weekly_hwpx.py input.json --output out\report.hwpx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lawtrack.config import configure_utf8_console  # noqa: E402
from lawtrack.report.hwpx import (  # noqa: E402
    REPORT_TITLE,
    ReportBuildError,
    load_contract_json,
    write_weekly_hwpx,
)


def main(argv: list[str] | None = None) -> int:
    configure_utf8_console()
    parser = argparse.ArgumentParser(
        description="WeeklyContract JSON을 주간 법령개정 HWPX 보고서로 변환합니다.",
    )
    parser.add_argument("input", type=Path, help="입력 weekly_contract_*.json")
    parser.add_argument("--output", "-o", type=Path, help="출력 .hwpx 경로")
    parser.add_argument("--title", default=REPORT_TITLE, help="보고서 제목")
    parser.add_argument("--author", default="(내부 기재)", help="생성자/작성자")
    parser.add_argument("--department", default="(내부 기재)", help="관리부서")
    parser.add_argument("--manager", default="(내부 기재)", help="검토자")
    args = parser.parse_args(argv)

    try:
        contract = load_contract_json(args.input)
        output = args.output or Path("out") / f"weekly_law_report_{contract.batch_date}.hwpx"
        result = write_weekly_hwpx(
            contract,
            output,
            title=args.title,
            author=args.author,
            department=args.department,
            manager=args.manager,
        )
    except ReportBuildError as exc:
        print(f"❌ HWPX 생성 실패: {exc}")
        return 1

    print(f"✅ HWPX 보고서 생성: {result['path']}")
    print(f"   파일 크기: {result['size']:,} bytes")
    print("   패키지·문서·재열기 검증: 통과")
    print("   실제 페이지 배치는 한컴오피스에서 최종 육안 확인이 필요합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
