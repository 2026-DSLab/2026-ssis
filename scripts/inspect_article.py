"""조문번호 필드의 실제 구조를 확인하는 진단 스크립트.

'제6조의2' 처럼 가지번호가 있는 조문에서, 실제 JSON 필드가 어떻게
생겼는지 원본 그대로 출력한다. parse/fulltext.py 의 조문번호 파싱
로직이 실제 구조와 맞는지 확인하기 위함.

사용법:
    python scripts\\inspect_article.py 001973 276653
    (법령ID, MST 순서 — 기본값은 국민기초생활보장법 276653)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from lawtrack.config import configure_utf8_console, load_settings  # noqa: E402
from lawtrack.api.client import LawApiClient  # noqa: E402
from lawtrack.api.fulltext import fetch_law_fulltext  # noqa: E402
from lawtrack.parse.jsonutil import as_list, dig, find_key  # noqa: E402


def main() -> int:
    configure_utf8_console()
    settings = load_settings()
    mst = sys.argv[2] if len(sys.argv) > 2 else "276653"

    client = LawApiClient(settings.api)
    result = fetch_law_fulltext(client, mst)
    client.close()

    raw = result.raw
    print("=== 최상위 루트 키 ===")
    print(list(raw.keys()))

    # parse_articles 와 동일한 경로 탐색 로직으로 조문 목록을 찾는다
    root = raw
    for key in ("법령", "Law", "LawService"):
        if key in raw and isinstance(raw[key], dict):
            root = raw[key]
            break

    print("\n=== root 안의 키 ===")
    print(list(root.keys()))

    items = None
    for path in (("조문", "조문단위"), ("조문",)):
        candidate = dig(root, *path)
        if candidate:
            items = as_list(candidate)
            print(f"\n조문 목록 발견 경로: {path}, 개수: {len(items)}")
            break

    if not items:
        found = find_key(root, "조문번호")
        print("\n경로 탐색 실패. find_key로 '조문번호' 직접 검색:", found)
        return 1

    print("\n=== 조문번호 '6' 또는 '6의2' 관련 항목을 찾아 원본 그대로 출력 ===\n")
    for item in items:
        if not isinstance(item, dict):
            continue
        # 조문번호 관련 필드를 폭넓게 탐색 (정확한 키를 몰라서 값으로 필터링)
        dump = json.dumps(item, ensure_ascii=False)
        if '"6"' in dump or '"6의2"' in dump or '중위소득' in dump or '000602' in dump:
            print(json.dumps(item, ensure_ascii=False, indent=2))
            print("-" * 60)

    print("\n=== (참고) 조문 목록 첫 3개 항목의 키 구조만 ===")
    for item in items[:3]:
        if isinstance(item, dict):
            print(list(item.keys()))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
