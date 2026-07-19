# 법령/행정규칙 개정 자동감지 파이프라인

국가법령정보 Open API를 이용해 워치리스트에 등록된 법령/행정규칙의 개정 여부를
매주 감지하고, 조/항/호/목 단위로 구조화된 diff를 만들어 LLM팀에게 JSON으로
넘겨준다.

## 요구사항

- Python 3.11+ (conda 환경 권장)
- MySQL 8.0+
- 국가법령정보 Open API 인증키(OC) — <https://open.law.go.kr>에서 발급

```bash
pip install -r requirements.txt
```

## 환경변수 (`.env`)

프로젝트 루트에 `.env` 파일을 만든다(코드 저장소에 커밋하지 말 것 — 인증키/DB
비밀번호가 들어간다).

```env
# 필수
LAW_API_OC=발급받은_OC_인증키
MYSQL_PASSWORD=MySQL_비밀번호

# 선택 (기본값 있음)
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_DATABASE=law_tracking_db
LOG_LEVEL=INFO
```

## DB 최초 구축 순서

**아래 순서를 반드시 지킨다.** `database/schema.sql`이 DB/테이블을 만들고,
`database/seed_watchlist.sql`이 감시 대상 워치리스트(현재 102건: 법령 76건 +
행정규칙 26건)를 등록한다. `laws`/`administrative_rules`(법령/행정규칙 전문
아카이브)와 `article_diff`(조문별 diff)는 **일부러 비워둔다** — 이 두 테이블에
행이 있는지 없는지 자체가 "이 버전을 이미 처리했는가"를 판단하는 개정감지의
핵심 신호이기 때문에, 미리 채워 넣으면 안 된다(아래 "왜 `seed_initial.sql`을
쓰면 안 되는가" 참고).

1. **스키마 생성**

   ```bash
   mysql -u root -p < database/schema.sql
   ```

2. **워치리스트 등록** — 아래 둘 중 하나만 실행(둘 다 같은 데이터,
   Windows에서 `mysql` CLI로 한글 SQL 파일을 실행하면 코드페이지 문제로
   깨질 수 있어 Python 스크립트를 권장한다):

   ```bash
   # 권장 (Windows 인코딩 문제 우회)
   python scripts/load_watchlist.py

   # 또는
   mysql -u root -p law_tracking_db < database/seed_watchlist.sql
   ```

3. **최초 전체 수집(백필)** — `laws`/`administrative_rules`/`article_diff`가
   비어있으므로, 워치리스트의 모든 항목이 첫 실행 시 "개정 감지됨"으로
   판정되어 각 법령/행정규칙의 현재 전문과 (있다면) 최근 개정분 diff가
   전부 채워진다. API 호출량이 커서(102건 × 여러 API 콜) 몇 분 정도
   걸릴 수 있다.

   ```bash
   python scripts/run_weekly.py
   ```

4. 이후로는 `python scripts/run_weekly.py`를 주기적으로(원래 설계는 주간
   1회) 실행하면 된다 — 실제 스케줄러(cron, Windows 작업 스케줄러 등)에
   등록하는 것은 이 프로젝트의 범위 밖이며, 인프라 담당이 별도로 구성해야
   한다. 산출물은 `out/weekly_contract_<날짜>.json`에 쌓인다.

## ⚠️ `database/seed_initial.sql`은 쓰지 않는다

이 파일은 `laws`/`administrative_rules`에 **내용이 빈 JSON**(`law_full_text =
JSON_OBJECT()`)으로 미리 행을 채워 넣는 옛 설계의 흔적이다. 이 프로젝트의
개정감지는 정확히 "`(law_id, 현재_일련번호)` 조합이 `laws`/`administrative_rules`에
이미 존재하는가"로 판단하므로(`VersionRepo.law_exists`/`admrul_exists`), 이 파일을
실행하면 실제 전문 데이터 없이 "이미 처리 완료"로 표시된 가짜 행이 생겨 —
**해당 항목들이 영원히 "변경없음"으로만 보고되고, 실제 전문이나 diff는 절대
채워지지 않는다.** 이걸 보완하려던 후속 스크립트 `scripts/load_full_text.py`도
끝내 구현되지 않아 0바이트로 남아있다. 최초 백필은 위 3번 단계
(`scripts/run_weekly.py`)로 한다 — 별도 전문 사전 적재가 필요 없다.

## 산출물(`out/*.json`) 구조

`scripts/run_weekly.py`를 실행하면 `out/weekly_contract_<batch_date>.json`
파일 하나가 만들어진다 — 이게 LLM팀에게 넘겨주는 실제 계약(contract)이다.
스키마는 `src/lawtrack/contract/schema.py`에 Pydantic 모델로 정의돼 있고,
여기 문서는 그 필드를 실제 값 예시와 함께 설명한다(예시는 전부
`build_contract()`가 실제로 만들어낸 값을 그대로 옮긴 것).

> 지금 `out/`에 있는 파일들은 실제 `run_weekly.py`를 그대로 돌린 결과가
> 아니라, LLM팀에게 스키마를 예시로 보여주기 위해 워치리스트 중 일부만
> **실제로 라이브 API를 다시 호출해**(기존 `laws`/`administrative_rules`
> 행을 지우고 `process_entry()`를 실제 실행 — 즉 국가법령정보 API를 그때마다
> 진짜로 호출했다) 재처리한 뒤, 그 대상만 남긴 축소판 `WeeklyContract`들이다.
> 필드 구성은 실제 배치 파일과 100% 동일(전부 Pydantic 재검증 통과)하고,
> 실제 `run_weekly.py`를 그대로 실행하면 이런 축소 없이 그 주에 감지된
> 전체 개정분이 `period`(최근 7일 기준)에 맞춰 하나의 파일로 나온다.
>
> | 파일 | 구성 | 비고 |
> |---|---|---|
> | `weekly_contract_2026-07-19.json` | 법령 2 + 행정규칙 1 | 국민체육진흥법에 `unresolved` 1건, (계약예규) 공동계약운용요령이 `no_comparison`으로 등장 |
> | `weekly_contract_2026-07-19_b.json` | 법령 2 + 행정규칙 1 | 전자정부법·전자정부법 시행령·장애인·고령자 등의 정보 접근... 고시 |
> | `weekly_contract_2026-07-19_c.json` | 법령 2 + 행정규칙 1 | 범죄피해자 보호법·장애인연금법·(계약예규) 예정가격작성기준, `unresolved` 1건 포함 |
> | `weekly_contract_2026-07-19_d.json` | 법령 2 + 행정규칙 1 | 고독사 예방 및 관리에 관한 법·사회서비스 지원...·조달청 내자구매업무 처리규정 |
> | `weekly_contract_2026-07-19_e.json` | 법령 2 + 행정규칙 1 | 기초연금법·개인정보 보호법 시행령·전자정부 웹사이트 품질관리 지침 |
> | `single_law_1_009513.json` | 법령 1건만 | 암관리법, 조문변경 1건 |
> | `single_law_2_013242.json` | 법령 1건만 | 재외국민보호를 위한 영사조력법, 조문변경 1건("지불"→"지급" 순화) |
> | `single_admrul_1_27947.json` | 행정규칙 1건만 | (계약예규) 물품구매(제조)계약일반조건, 조문변경 2건(`위치재배치의심` 포함) |
> | `single_admrul_2_43010.json` | 행정규칙 1건만 | 전자정부사업관리 위탁용역계약 특수조건, 조문변경 1건 |
>
> `weekly_contract_*`는 `amendment_groups`/`unresolved`/`no_comparison`
> 세 배열을 골고루 보여주는 "법령+행정규칙 섞인 소규모 위클리" 예시고,
> `single_*`는 조문변경이 5건 이하인 것만 골라 법령 또는 행정규칙 딱
> 1건만 담은 최소 단위 예시다.

### 최상위 구조

```json
{
  "contract_version": "1.0",
  "batch_date": "2026-07-19",
  "period": { "from_date": "2026-07-12", "to_date": "2026-07-19" },
  "amendment_groups": [ ... ],
  "unresolved": [ ... ],
  "no_comparison": [ ... ]
}
```

| 필드 | 의미 |
|---|---|
| `contract_version` | 스키마 버전(현재 고정값 "1.0") — LLM팀이 파싱 전 호환성 확인용 |
| `batch_date` | 이 배치가 실행된 날짜(오늘) |
| `period` | 이번에 조회한 시행일(`enforce_date`) 구간. `run_weekly.py`는 기본 최근 7일 |
| `amendment_groups` | **실제로 위치까지 확정된 개정 내용.** 아래 참고 |
| `unresolved` | 개정은 감지됐지만 본문에서 정확한 위치를 못 찾은 조각들. 절대 빠지지 않음 |
| `no_comparison` | 개정은 감지됐지만 신구법 대비 자체가 불가능한 건(제정/폐지제정 등) |

세 배열(`amendment_groups`의 law 단위, `unresolved`, `no_comparison`)은
서로 겹치지 않는다 — 워치리스트의 법령/행정규칙 1건은 이번 배치에서
정확히 이 셋 중 하나에만 속하거나(개정이 있었다면), 아무 데도 안 나타난다
(이번 기간에 개정이 아예 없었으면 = 변경없음, 셋 중 어디에도 안 나옴).

### `amendment_groups[]` — 공포번호로 묶은 개정 이벤트

법제처는 여러 법을 한 번에 묶어 개정하는 경우가 많다(예: 정부조직 개편으로
관련법 10여 개가 같은 날 동시 개정). 이런 "같은 공포번호"로 묶인 법들을
하나의 그룹으로 모아준다 — LLM이 "이건 하나의 사건"이라고 맥락을 잡을 수
있게 하기 위함이다.

```json
{
  "group_id": "21065",
  "promulgation_no": "21065",
  "promulgation_date": "",
  "revision_type": "",
  "affected_law_ids": ["000171", "000204", "001971", "001973", "009409",
                        "009595", "010181", "010909", "011170", "011181",
                        "011460", "012045", "012270"],
  "laws": [ /* LawChange 배열, 아래 참고 */ ]
}
```

- `group_id`: 공포번호로 묶였으면 공포번호 그대로, 단독 개정이면
  `single-{law_id}-{new_serial_no}` 형태(위 예시는 정부조직 개편 공포번호
  21065로 13개 법이 동시 개정된 실제 사례).
- `affected_law_ids`: 이 그룹에 속한 법령/행정규칙 ID 목록.
- `laws`: 실제 개정 내용이 담긴 `LawChange` 객체 배열(그룹 하나에 1개 이상).

### `laws[]` 안의 `LawChange` — 법령/행정규칙 1건의 개정 정보

```json
{
  "law_id": "000729",
  "law_type": "법률",
  "law_name": "보조금 관리에 관한 법률",
  "internal_name": "보조금 관리에 관한 법률",
  "dept_codes": [],
  "old_serial_no": "286449",
  "new_serial_no": "286449",
  "enforce_date": "2026-06-02",
  "revision_type": "일부개정",
  "revision_reason": "[일부개정] ◇ 개정이유 및 주요내용 기획예산처장관이 한국재정정보원에...",
  "source_url": "https://www.law.go.kr/DRF/lawService.do?target=law&MST=286449&type=HTML",
  "articles": [
    {
      "article_label": "제26조의7",
      "clause_no": "④",
      "item_label": "5.",
      "subitem_label": "",
      "change_type": "신설",
      "old_text": "",
      "new_text": "5. 보조금통합관리망을 통한 보조금 부정 수급 모니터링 결과에 대한 점검 및 현장조사 업무",
      "match_status": "성공"
    }
  ],
  "unchanged_clauses": { "제26조의7": ["①", "②", "③", "⑤"] }
}
```

| 필드 | 의미 |
|---|---|
| `law_id` | 법령ID/행정규칙ID(불변 식별자 — MST/일련번호와 다름, 개정돼도 안 바뀜) |
| `law_type` | `법률` \| `시행령` \| `시행규칙` \| `행정규칙` |
| `law_name` | 현재(API가 인식하는) 정식 명칭 |
| `internal_name` | 내부 관리명. 워치리스트 등록 당시 이름 — 제명변경이 있었으면 `law_name`과 달라짐(예: "국가정보화 기본법"→"지능정보화 기본법") |
| `dept_codes` | 소관부처(행정규칙의 동명이인 구분용, 법령은 대부분 빈 배열) |
| `old_serial_no` | 개정 전 일련번호 |
| `new_serial_no` | 개정 후(현재) 일련번호 — MST(법령) 또는 행정규칙일련번호 |
| `enforce_date` | 시행일자 |
| `revision_type` | 제개정구분 (일부개정/전부개정/제정/폐지제정/타법개정 등) |
| `revision_reason` | 법제처 공식 개정이유 원문. **원본에 아예 없는 경우 빈 문자열**(추론 안 함 — 없으면 없는 대로 정직하게 비움) |
| `source_url` | 법제처 원문 링크(OC 인증키는 제거된 안전한 URL) |
| `articles` | 실제로 위치가 확정된 조문별 변경사항. 아래 참고 |
| `unchanged_clauses` | 개정된 조문 중 "이번에 안 바뀐" 항/호 라벨. 아래 참고 |

### `articles[]` 안의 `ArticleDiffItem` — 조문 단위 변경 사실

- `article_label`/`clause_no`/`item_label`/`subitem_label`: 위치(조/항/호/목).
  전부 합치면 `제26조의7④5.`처럼 사람이 읽는 위치 표기가 된다. 위치를 못 찾은
  경우(드묾, 대부분은 `unresolved`로 빠짐) `article_label`이
  `(위치미상#N-M)` 형태로 나올 수 있다.
- `change_type`: `개정` \| `신설` \| `삭제` \| `미상`.
- `old_text`/`new_text`: 개정 전/후 문장. `신설`이면 `old_text`는 항상 빈
  문자열(개정 전엔 존재하지 않았으므로). **주의**: `new_text`는 실제
  위치가 확정된 조각 그대로라 정확하지만, `old_text`는 한 신구법 조각이
  여러 항/호로 쪼개질 경우 그 조각 전체(안 쪼개진 원문)를 재사용한다 —
  알려진 한계이며 각 행마다 정밀하게 대응되지 않을 수 있다.
- `match_status`: 이 배열에는 `성공`과 `삭제(위치탐색제외)`(내용 자체가
  "<삭제>" 마커라 위치 검색 대상이 아닌 경우), 그리고
  **`위치재배치의심`**만 나온다 — 진짜 실패(`0건실패`/`중복실패`)는 여기
  안 나오고 `unresolved`로 따로 빠진다. `위치재배치의심`은 같은 조문 안에서
  항이 신설되며 뒤 항 번호가 밀린 경우 — 법제처 원본 신구조문대비표가
  위치(순번) 기준으로만 신/구를 대응시키기 때문에, `old_text`가 실제로는
  다른 항의 내용일 수 있다는 뜻이다. **이 값이 뜨면 `old_text`를 "그
  조항의 개정 전 내용"으로 그대로 신뢰하면 안 된다.**

### `unchanged_clauses` — "이번에 확인상 안 바뀐" 항/호

`{"제26조의7": ["①", "②", "③", "⑤"]}`처럼, 개정된 조문 안에서 이번에
안 바뀐 항/호 라벨만 모아준다 — LLM이 "나머지 항도 바뀐 건가?"를 추론하지
않아도 되게 하려는 목적. 근거는 항상 법제처 원본이 준 사실이며(추론 아님),
두 가지 소스가 있다:

- **법령**: 법제처가 항마다 공식으로 붙이는 `항제개정유형` 필드 기준(항 라벨만,
  예: `"①"`).
- **행정규칙**: 신구법 비교의 `(생략)`/`(현행과 같음)` 스킵 표시 기준(항 또는
  호 라벨, 예: `"①3."`처럼 호가 어느 항에 속하는지까지 명시 — 항마다 호
  번호가 1부터 다시 시작할 수 있어서 항 접두어 없이는 어느 항의 호인지
  구분이 안 되기 때문).

스킵 표시가 없거나 해당 조문이 이번에 개정된 게 아니면 그 조문 자체가
`unchanged_clauses`에 아예 안 나온다(빈 배열이 아니라 키 자체가 없음).

### `unresolved[]` — 위치를 못 찾은 조각 (LLM팀이 반드시 확인해야 함)

```json
{
  "law_id": "001971",
  "law_name": "국민건강보험법",
  "new_serial_no": "276651",
  "reason": "중복실패",
  "detail": "본문검색: '감사는 임원추천위원회가 복수로 추천한 사람 …'; 중복 2건, 마커 없어 번호결합 불가",
  "source_url": "https://www.law.go.kr/DRF/lawService.do?target=law&MST=276651&type=HTML",
  "guards_tried": ["본문검색: '감사는 임원추천위원회가 복수로 추천한 사람 …'", "중복 2건, 마커 없어 번호결합 불가"]
}
```

`reason`은 `0건실패`(본문에서 아예 못 찾음) 또는 `중복실패`(완전히 동일한
문장이 본문 안에 2곳 이상 있어 어느 쪽인지 특정 불가 — 대부분 법 안에
우연히 같은 문장이 반복되는 진짜 케이스다). `guards_tried`는 6단계 위치확정
가드가 어디까지 시도했는지의 로그. **이 배열에 걸린 법은 `amendment_groups`
쪽의 `articles`가 비어 있거나 일부만 채워질 수 있으니, LLM팀은 특정 법이
개정됐는데 `articles`가 이상하게 적으면 여기도 반드시 대조해야 한다.**

### `no_comparison[]` — 신구법 대비 자체가 불가능한 건

```json
{
  "law_id": "35080",
  "law_name": "하도급거래공정화 지침",
  "new_serial_no": "2100000251404",
  "reason": "일부개정",
  "note": "신구법 대비표 없음 — 원문 링크 참조",
  "source_url": "https://www.law.go.kr/DRF/lawService.do?target=admrul&ID=2100000251404&type=HTML"
}
```

제정/폐지제정처럼 "이전 버전"이 존재하지 않거나, 법제처가 이번 개정에
대해 신구법 비교 자체를 제공하지 않는 경우다. `reason`은 대부분
`revision_type`을 그대로 옮긴 값(`제정`/`일부개정`/`폐지제정` 등)이고,
diff는 원천적으로 없으므로 `source_url`(원문 링크)만 제공한다 — LLM팀이
필요하면 원문을 직접 봐야 한다.

### 파일 크기 참고

전체 워치리스트(102건) 기준 실측: 74개 그룹, 법 93건, 조문변경 984건,
미확정 18건, 비교불가 9건 → 약 1MB. `run_weekly.py`의 실제 운영 모드는
최근 7일치만 조회하므로 평소엔 이보다 훨씬 작다(개정이 없는 주는
`amendment_groups: []`로 사실상 빈 파일). 지금 `out/`에 있는 축소판들은
`weekly_contract_*`(법령 2+행정규칙 1, 3건)가 각 20~56KB, `single_*`
(법령 또는 행정규칙 1건, 조문변경 5건 이하)가 각 2~3KB 수준이다.

## 저장소 구조

```
src/lawtrack/
  api/        HTTP 레이어 (client, search, fulltext, oldnew)
  text/       순수 로직 (normalize, split)
  parse/      API 응답 → 구조화 (jsonutil, oldnew, fulltext)
  locate/     6가드 위치확정 파이프라인 (핵심 diff 로직)
  db/         conn.py, repo.py (Watchlist/Version/ChangeLog/ArticleDiff Repo)
  contract/   schema.py(Pydantic), export.py (DB → LLM팀용 JSON)
  detect.py   워치리스트 1건 처리(감지→조회→분석→저장)

scripts/
  run_weekly.py       주간 배치 — 워치리스트 전체를 돌며 개정 감지·저장·JSON 산출
  run_single_check.py 법령/행정규칙 1건 디버깅용 상세 실행
  load_watchlist.py   워치리스트 초기 적재 (Windows 인코딩 우회)

database/
  schema.sql          테이블 정의
  seed_watchlist.sql  워치리스트 초기 데이터 (load_watchlist.py와 내용 동일)
  seed_initial.sql    ⚠️ 사용하지 않음 — 위 경고 참고

tests/        pytest, 실측 데이터 기반 회귀 테스트
```

## 테스트

```bash
pytest -q
```
