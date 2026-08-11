"""watchlist 102건 매칭 사전.

DB 연결 없이 database/seed_watchlist.sql 을 직접 파싱한다.
(PoC 단계에서 테스트를 DB 상태와 무관하게 만들기 위함.
 웹 통합 시 watchlist 테이블 SELECT 로 교체 가능한 동일 인터페이스.)

사전 키 = norm(official_name) 및 norm(internal_name).
official/internal 이원 구조 덕에 제명변경(지능정보화 기본법 등)과
사내 표기가 모두 키로 등록된다.
"""
import re
import unicodedata
from dataclasses import dataclass, field

from doc_match.normalize import norm

_ROW = re.compile(r"\('(\d+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',")

# 실문서에서 확인된 약칭 → watchlist official_name 수동 매핑.
# (국가법령정보 API 법령약칭 조회로 자동화 가능하나 미검증 — 확장 시 검토)
EXTRA_ALIASES = {
    "국가계약법": "국가를 당사자로 하는 계약에 관한 법률",
    "국가계약법 시행령": "국가를 당사자로 하는 계약에 관한 법률 시행령",
    "국가계약법 시행규칙": "국가를 당사자로 하는 계약에 관한 법률 시행규칙",
    "파견근로자보호법": "파견근로자보호 등에 관한 법률",  # watchlist 외지만 명칭 통일용은 아님 — 미등록 시 무시됨
}


@dataclass
class WatchEntry:
    law_id: str
    law_type: str
    official: str
    internal: str


@dataclass
class Dictionary:
    entries: dict[str, WatchEntry] = field(default_factory=dict)  # law_id → entry
    alias: dict[str, str] = field(default_factory=dict)  # norm key → law_id

    @property
    def keys_longest_first(self) -> list[str]:
        return sorted(self.alias, key=len, reverse=True)


def load_from_seed(path: str) -> Dictionary:
    sql = open(path, encoding="utf-8").read()
    d = Dictionary()
    for law_id, law_type, official, internal in _ROW.findall(sql):
        official = unicodedata.normalize("NFC", official)
        internal = unicodedata.normalize("NFC", internal)
        d.entries[law_id] = WatchEntry(law_id, law_type, official, internal)
        for name in {official, internal}:
            key = norm(name)
            if key:
                d.alias[key] = law_id
    # 약칭: 대상 official_name 이 watchlist 에 실존할 때만 등록
    by_official = {e.official: e.law_id for e in d.entries.values()}
    for short, official in EXTRA_ALIASES.items():
        if official in by_official:
            d.alias[norm(short)] = by_official[official]
    return d
