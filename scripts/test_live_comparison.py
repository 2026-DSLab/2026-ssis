import logging
import sys
from pathlib import Path

# src 폴더를 경로에 추가
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lawtrack.config import load_settings, setup_logging
from lawtrack.db.conn import Database
from lawtrack.api.client import LawApiClient
from lawtrack.api.search import resolve_law
from lawtrack.api.oldnew import fetch_law_oldnew
from lawtrack.api.fulltext import fetch_law_fulltext
from lawtrack.parse.oldnew import extract_changes
from lawtrack.parse.fulltext import parse_articles, flatten_searchable
from lawtrack.locate.locator import locate_all

def main():
    # 로그 설정
    settings = load_settings()
    setup_logging("WARNING")
    client = LawApiClient(settings.api)
    db = Database(settings.db)
    
    # 터미널에 인자를 주면 그 법들을 검사하고, 없으면 DB에서 무작위로 5개를 가져옵니다.
    target_laws = sys.argv[1:]
    if not target_laws:
        with db.cursor() as (_, cur):
            # laws 테이블에서 무작위 5개 추출
            cur.execute("SELECT law_name FROM laws ORDER BY RAND() LIMIT 5")
            target_laws = [row["law_name"] for row in cur.fetchall()]
    
    for law_name in target_laws:
        print(f"\n\n{'='*60}")
        print(f"==== [{law_name}] 신구법 비교 및 6가드 위치탐색 테스트 ====")
        print(f"{'='*60}\n")
        
        print("1. API 목록 검색 중...")
        outcome = resolve_law(client, law_name)
        if outcome.status != "matched":
            print(f"   -> 검색 실패 또는 모호함: {outcome.status}")
            continue
            
        mst = outcome.candidates[0].serial_no
        print(f"   -> 최근 일련번호(MST): {mst} 발견\n")
        
        print("2. 신구법 비교문서(oldAndNew) API 조회 중...")
        oldnew = fetch_law_oldnew(client, mst)
        if not oldnew.available:
            print(f"   -> 앗, 이 법령은 현재 신구조문대비표가 제공되지 않습니다: {oldnew.reason}")
            continue
            
        print(f"   -> API에서 (구)문서 {len(oldnew.old_texts)}개, (신)문서 {len(oldnew.new_texts)}개 수신 완료.")
        
        changes = extract_changes(oldnew.old_texts, oldnew.new_texts)
        print(f"   -> HTML 등 제거 및 파싱된 실제 변경내역: {len(changes)}건\n")
        
        if not changes:
            print("   -> 변경 내역이 비어있어 다음 법령으로 넘어갑니다.")
            continue
            
        print("3. 법령 전문(Full Text) API 조회 및 검색 조각 분해 중...")
        full = fetch_law_fulltext(client, mst)
        articles = parse_articles(full.raw)
        units = flatten_searchable(articles)
        print(f"   -> 전체 법령을 {len(units)}개의 탐색 조각(SearchUnit)으로 분해 완료.\n")
        
        print("4. 6가드 위치 탐색(Locator) 실행 중...")
        results = locate_all(changes, units)
        
        success_cnt = 0
        print("\n---------------- [매칭 결과 요약] ----------------")
        for change, locs in results:
            for loc in locs:
                if loc.status.value == "성공":
                    success_cnt += 1
                    print(f"✅ [성공] {loc.location_label} ({change.change_type.value})")
                else:
                    print(f"❌ [실패: {loc.status.value}] 원본 문구: {change.new_clean[:40]}...")
        
        print("--------------------------------------------------")
        print(f"[{law_name}] 총 {len(changes)}건의 변경사항 중 {success_cnt}건 위치 확정 성공!\n")

if __name__ == "__main__":
    main()
