#!/usr/bin/env python3
"""사용법:
  PYTHONPATH=src python3 scripts/check_document.py <문서.pdf|문서.hwpx> [...]

기본 seed 경로: database/seed_watchlist.sql (--seed 로 변경 가능)
"""
import argparse
import sys
from pathlib import Path

from doc_match.dictionary import load_from_seed
from doc_match.extract import extract_text
from doc_match.match import match_pages
from doc_match.report import build_summary, render_text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--seed", default="database/seed_watchlist.sql")
    args = ap.parse_args()

    if not Path(args.seed).exists():
        print(f"seed 파일 없음: {args.seed} (--seed 로 경로 지정)", file=sys.stderr)
        return 1
    d = load_from_seed(args.seed)

    for f in args.files:
        pages = extract_text(f)
        total_chars = sum(len(p) for p in pages)
        if total_chars < 100:
            print(f"⚠ {f}: 추출 텍스트가 거의 없음({total_chars}자) — "
                  f"스캔본(이미지) PDF일 가능성", file=sys.stderr)
        summary = build_summary(match_pages(pages, d), d)
        print(render_text(summary, Path(f).name))
        print("\n" + "=" * 70 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
