"""CLI 진입점.

    # 프롬프트만 확인 (API 키 불필요)
    python -m summarizer out/single_law_1_009513.json --dry-run --echo

    # 실제 실행 (.env 에 ANTHROPIC_API_KEY 필요)
    python -m summarizer out/single_law_1_009513.json
    python -m summarizer out/single_*.json
    python -m summarizer out/weekly_contract_2026-07-19.json
"""

from __future__ import annotations

import argparse
import logging
import sys

from pathlib import Path

from lawtrack.config import setup_console

from summarizer.config import ConfigError, load_settings
from summarizer.llm import build_client
from summarizer.pipeline import SummaryPipeline
from summarizer.sinks import HwpxSink, JsonSink

log = logging.getLogger("summarizer")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="summarizer", description="법령 개정 요약 파이프라인")
    parser.add_argument("paths", nargs="+", help="계약 JSON 경로 (single_*.json / weekly_*.json)")
    parser.add_argument("--dry-run", action="store_true", help="API 호출 없이 배선만 확인")
    parser.add_argument("--echo", action="store_true", help="--dry-run 시 프롬프트 전문 출력")
    parser.add_argument("--env-file", default=None, help=".env 경로 (기본: 프로젝트 루트)")
    parser.add_argument("--no-write", action="store_true", help="파일로 저장하지 않음")
    parser.add_argument("--hwpx", action="store_true", help="HWPX 보고서도 생성 (out/reports/)")
    parser.add_argument(
        "--db", action="store_true",
        help="요약을 law_summary 테이블에 적재 (.env 의 POSTGRES_* 필요)",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.env_file, require_api_key=not args.dry_run)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    # cp949 콘솔에서 em dash 같은 글자에 죽지 않게 한다. 요약을 다 만든 뒤
    # 화면에 뿌리다가 죽으면 LLM 호출 비용을 그대로 날린다.
    setup_console()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    client = build_client(settings.llm, dry_run=args.dry_run, echo=args.echo)
    results = SummaryPipeline(client, settings).run(args.paths)

    for contract in results:
        for law in contract.laws:
            print(f"\n■ [{law.law_type}] {law.law_name} (시행 {law.enforce_date or '미상'})")
            print(f"  {law.headline}")
            for caveat in law.caveats:
                print(f"  ※ {caveat}")

    if not args.no_write and not args.dry_run:
        JsonSink(settings.pipeline.output_dir).write(results)
        if args.hwpx:
            HwpxSink(settings.pipeline.output_dir.parent / "reports").write(results)
        if args.db:
            # DB 관련 import 는 여기서만 — 파일 출력만 쓰는 사람이
            # psycopg2 를 깔지 않아도 되게 한다.
            from lawtrack.config import load_db_settings
            from lawtrack.db.conn import Database

            from summarizer.sinks import DbSink

            db = Database(load_db_settings(args.env_file))
            DbSink(
                db,
                llm_provider=settings.llm.provider,
                llm_model=settings.llm.model,
            ).write(results)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
