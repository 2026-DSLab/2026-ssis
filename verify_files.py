"""
파일 배치 검증 스크립트.

각 파일이 "제자리에 맞는 내용"을 담고 있는지 한 번에 검사한다.
파일명이 겹치는 것들(api/oldnew.py vs parse/oldnew.py 등) 때문에
엉뚱한 내용이 들어갔을 때, 하나씩 import 에러를 쫓는 대신 이 스크립트
하나로 전부 찾아낸다.

사용법 (2026-ssis 폴더 루트에서):
    python verify_files.py
"""

import importlib
import sys
from pathlib import Path

# 각 모듈이 반드시 갖고 있어야 하는 심볼(클래스/함수) 목록.
# 여기 없으면 = 그 파일이 비어있거나, 엉뚱한 내용이 들어간 것.
EXPECTED = {
    "lawtrack.config": ["ApiSettings", "DbSettings", "load_settings"],
    "lawtrack.text.normalize": ["normalize_name", "normalize_text", "names_match", "DOT_MAP"],
    "lawtrack.text.split": ["split_all", "ArticleNo", "strip_article_head", "searchable_fragments"],
    "lawtrack.parse.jsonutil": ["as_list", "dig", "find_key", "collect_texts", "looks_like_api_error"],
    "lawtrack.parse.oldnew": ["extract_changes", "ChangeType", "strip_p_tags", "ArticleChange"],
    "lawtrack.parse.fulltext": ["parse_articles", "flatten_searchable", "changed_articles", "SearchUnit"],
    "lawtrack.api.client": ["LawApiClient", "LawApiError", "LawApiAuthError", "assert_fulltext_payload"],
    "lawtrack.api.search": ["search_law", "search_admrul", "resolve_law", "resolve_admrul"],
    "lawtrack.api.fulltext": ["fetch_law_fulltext", "fetch_admrul_fulltext", "FullTextResult"],
    "lawtrack.api.oldnew": ["fetch_law_oldnew", "fetch_admrul_oldnew", "OldNewResult"],
    "lawtrack.locate.locator": ["locate_change", "locate_all", "LocateStatus", "LocateResult"],
    "lawtrack.db.conn": ["Database"],
    "lawtrack.db.repo": ["WatchlistRepo", "VersionRepo", "ChangeLogRepo", "ArticleDiffRepo"],
    "lawtrack.detect": ["detect_law", "detect_admrul", "process_law_entry", "DetectStatus"],
    "lawtrack.link": ["group_by_promulgation", "group_many", "LinkedGroup"],
    "lawtrack.contract.schema": ["WeeklyContract", "AmendmentGroup", "ArticleDiffItem"],
    "lawtrack.contract.export": ["build_contract", "write_contract"],
}

# 모듈 경로 -> 실제 파일 경로 (사람이 읽고 바로 찾아갈 수 있게)
FILE_PATH = {name: "src/" + name.replace(".", "/") + ".py" for name in EXPECTED}


def main() -> int:
    print("=" * 70)
    print("lawtrack 파일 배치 검증")
    print("=" * 70)

    ok_count = 0
    problems: list[str] = []

    for module_name, symbols in EXPECTED.items():
        file_path = FILE_PATH[module_name]
        path_exists = Path(file_path).exists()

        if not path_exists:
            problems.append(f"❌ {file_path}\n   → 파일 자체가 없음")
            continue

        try:
            mod = importlib.import_module(module_name)
        except Exception as exc:
            problems.append(f"❌ {file_path}\n   → import 실패: {type(exc).__name__}: {exc}")
            continue

        missing = [s for s in symbols if not hasattr(mod, s)]
        if missing:
            problems.append(
                f"❌ {file_path}\n"
                f"   → 다음 이름이 없음: {missing}\n"
                f"   → 십중팔구 다른 파일 내용이 잘못 들어간 것 (예: api/oldnew.py 에 "
                f"parse/oldnew.py 내용이 들어갔거나 그 반대)"
            )
            continue

        ok_count += 1

    print(f"\n정상: {ok_count}/{len(EXPECTED)}\n")

    if problems:
        print("문제 있는 파일:\n")
        for p in problems:
            print(p)
            print()
        print("=" * 70)
        print(f"총 {len(problems)}개 파일에 문제가 있습니다. 위 경로를 다시 채워 넣어주세요.")
        return 1

    print("모든 파일이 정상적으로 배치되었습니다. ✅")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, "src")
    raise SystemExit(main())