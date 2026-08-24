"""매칭 결과 → 사람이 읽는 리포트(텍스트) + 구조화 dict.

웹 통합 시 build_summary() 반환 dict 를 JSON 응답으로 그대로 쓸 수 있음.
"""
import collections

from doc_match.dictionary import Dictionary
from doc_match.match import MatchResult
from doc_match.normalize import norm


def build_summary(result: MatchResult, d: Dictionary) -> dict:
    per_law: dict[str, dict] = {}
    for h in result.hits:
        e = d.entries[h.law_id]
        item = per_law.setdefault(
            h.law_id,
            {
                "law_id": e.law_id,
                "law_type": e.law_type,
                "official_name": e.official,
                "count": 0,
                "pages": [],
                "sample_snippet": h.snippet,
            },
        )
        item["count"] += 1
        if h.page not in item["pages"]:
            item["pages"].append(h.page)

    # 대상 외 후보: 정규화 키로 표기 변형 병합
    cand = collections.defaultdict(lambda: {"count": 0, "pages": [], "labels": set()})
    for c in result.candidates:
        g = cand[norm(c.raw)]
        g["count"] += 1
        g["labels"].add(c.raw)
        if c.page not in g["pages"]:
            g["pages"].append(c.page)
    candidates = [
        {
            "name": sorted(g["labels"], key=len)[-1],
            "variants": sorted(g["labels"]),
            "count": g["count"],
            "pages": g["pages"],
        }
        for g in cand.values()
    ]
    candidates.sort(key=lambda x: -x["count"])

    matched = sorted(per_law.values(), key=lambda x: -x["count"])
    return {
        "watchlist_total": len(d.entries),
        "watchlist_matched": len(matched),
        "matched": matched,
        "out_of_watchlist_candidates": candidates,
    }


def render_text(summary: dict, doc_name: str) -> str:
    lines = [
        f"문서: {doc_name}",
        f"모니터링 대상 {summary['watchlist_total']}건 중 "
        f"{summary['watchlist_matched']}건 인용 확인",
        "",
        "[모니터링 대상 내 인용]",
    ]
    for m in summary["matched"]:
        pages = ", ".join(map(str, m["pages"][:10]))
        more = f" 외 {len(m['pages']) - 10}p" if len(m["pages"]) > 10 else ""
        lines.append(
            f"  {m['count']:3d}회  [{m['law_type']}] {m['official_name']}"
            f"  (p.{pages}{more})"
        )
    lines += ["", "[모니터링 대상 외 인용 후보]"]
    if not summary["out_of_watchlist_candidates"]:
        lines.append("  (없음)")
    for c in summary["out_of_watchlist_candidates"]:
        pages = ", ".join(map(str, c["pages"][:10]))
        lines.append(f"  {c['count']:3d}회  {c['name']}  (p.{pages})")
    return "\n".join(lines)
