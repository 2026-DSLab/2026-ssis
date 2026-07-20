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
아카이브)와 `article_diff`(조문별 diff)는 **반드시 비워둔 채로 시작해야 한다**
— 이 두 테이블에 행이 있는지 없는지 자체가 "이 버전을 이미 처리했는가"를
판단하는 개정감지의 핵심 신호이기 때문이다(`VersionRepo.law_exists`/
`admrul_exists`). 미리 채워 넣으면(빈 값이든 실제 값이든) 그 항목은 영원히
"이미 처리됨"으로 오판되어 개정감지가 동작하지 않는다 — 최초 백필은 반드시
아래 3번 단계(`scripts/run_weekly.py`)로 한다.

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

### 전체 구조 한눈에 보기 (트리)

필드가 많아 처음 보면 헷갈릴 수 있어 트리로 먼저 정리한다. 각 필드의
자세한 의미와 실제 값 예시는 아래 절에서 순서대로 설명한다.

```
WeeklyContract (최상위)
├─ contract_version, batch_date, period{from_date, to_date}
├─ amendment_groups[]              ← 공포번호 하나로 같이 개정된 법들의 묶음
│   └─ AmendmentGroup
│       ├─ group_id, promulgation_no, promulgation_date, revision_type
│       ├─ affected_law_ids[]
│       └─ laws[]                  ← 법령/행정규칙 1건
│           └─ LawChange
│               ├─ law_id, law_type, law_name, internal_name, dept_codes[]
│               ├─ old_serial_no, new_serial_no, enforce_date
│               ├─ revision_type, revision_reason, source_url
│               ├─ articles[]              ← 항상 1:1 대응만 (아래 참고)
│               │   └─ ArticleDiffItem
│               │       ├─ article_label, clause_no, item_label, subitem_label
│               │       ├─ change_type (개정|신설|삭제|미상)
│               │       ├─ old_text, new_text
│               │       └─ match_status (성공|삭제(위치탐색제외)|위치재배치의심)
│               ├─ structural_expansions[]  ← 1:N 그룹 (아래 참고)
│               │   └─ StructuralExpansion
│               │       ├─ article_label
│               │       ├─ old_text            ← 구법 원문 딱 1개(참고 맥락)
│               │       └─ new_items[]         ← 여기서 새로 생김
│               │           └─ ExpandedItem
│               │               ├─ clause_no, item_label, subitem_label
│               │               └─ text        ← 개정 후 정확한 문장
│               └─ unchanged_clauses{}     ← {"제34조": ["①","②"]} 형태
├─ unresolved[]                    ← 위치 확정 실패(0건실패/중복실패)
└─ no_comparison[]                 ← 신구법 대비 자체가 불가능(제정 등)
```

**핵심 구분 하나만 기억하면 된다**: `articles[]`는 항상 "행 하나 = 위치
하나"의 1:1 대응이고, `structural_expansions[]`는 "구법 문장 하나 → 신법
위치 여러 개"의 1:N 그룹이다. 한 `LawChange` 안에 이 둘이 같이 있을 수
있다 — 대부분 조문은 `articles[]`에 정상적으로 1:1로 들어가고, 개정으로
구조 자체가 바뀐 조문만 `structural_expansions[]`로 따로 빠진다.

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
| `articles` | 실제로 위치가 확정된 조문별 변경사항(항상 1:1 대응만). 아래 참고 |
| `structural_expansions` | 구법에 없던 항/호/목 구조가 새로 생긴 그룹(1:N). 아래 참고 |
| `unchanged_clauses` | 개정된 조문 중 "이번에 안 바뀐" 항/호 라벨. 아래 참고 |

### `articles[]` 안의 `ArticleDiffItem` — 조문 단위 변경 사실 (항상 1:1)

- `article_label`/`clause_no`/`item_label`/`subitem_label`: 위치(조/항/호/목).
  전부 합치면 `제26조의7④5.`처럼 사람이 읽는 위치 표기가 된다. 위치를 못 찾은
  경우(드묾, 대부분은 `unresolved`로 빠짐) `article_label`이
  `(위치미상#N-M)` 형태로 나올 수 있다.
- `change_type`: `개정` \| `신설` \| `삭제` \| `미상`.
- `old_text`/`new_text`: 개정 전/후 문장. `신설`이면 `old_text`는 항상 빈
  문자열(개정 전엔 존재하지 않았으므로). `old_text`/`new_text` 둘 다 이
  행의 위치(`article_label`+`clause_no`+`item_label`+`subitem_label`) 하나에
  정확히 대응한다 — **1:N 케이스(구법이 세분화되지 않았던 경우)는 이
  배열에 절대 섞이지 않고 `structural_expansions[]`로 분리되어 나간다.**
  하나의 법제처 개정 단위(신구조문대비표의 한 쌍)가 신법 쪽 여러 위치로
  쪼개지는 경우(예: 항 하나가 본문+호 여러 개로 재작성), `old_text`도
  구법을 호 단위까지 똑같이 쪼개서 각 위치와 애매함 없이 1:1로 맞을 때만
  그 조각을 준다 — 구법에도 호 마커가 있어 신법과 대응이 되면 정밀한
  `old_text`를, 구법이 통짜 문장이라 대응이 안 되면(호 단위까지만 시도,
  목까지는 안 내려감 — 목 마커는 오탐 위험이 커서 제외) 아예
  `structural_expansions[]`로 분리한다. 조각 중 일부만 맞고 일부는 안
  맞는 애매한 상태는 만들지 않는다(all-or-nothing).
- `match_status`: `성공` \| `삭제(위치탐색제외)`(내용 자체가 "<삭제>" 마커라
  위치 검색 대상이 아닌 경우) \| **`위치재배치의심`**(같은 조문 안에서 항이
  신설되며 뒤 항 번호가 밀려, 법제처 원본 신구조문대비표가 위치(순번)
  기준으로만 신/구를 대응시켰을 수 있음 — `old_text`가 실제로는 다른
  항의 내용일 수 있다는 뜻). 진짜 실패(`0건실패`/`중복실패`)는 여기 안
  나오고 `unresolved`로 따로 빠진다. `old_text`는 `위치재배치의심`이어도
  안내문 없이 항상 순수 원문 그대로다 — 신뢰도 판단은 `match_status`
  필드로만 한다.

### `structural_expansions[]` 안의 `StructuralExpansion` — 1:N 그룹

★ 왜 이런 배열이 따로 있는가 (Before → After):

개정으로 구법엔 없던 항/호/목 구조가 새로 생기는 경우가 실제로 있다 —
예: 전자정부법 제56조의2①이 구법엔 그냥

> `"① 행정기관의 장은 해당 기관 및 그 소속 기관의 정보시스템을 안정적으로 운영ㆍ관리하기 위하여 정보시스템의 장애 예방 및 대응을 위한 방안을 마련하여야 한다."`

라는 문장 하나뿐이었는데, 이번 개정으로

> `"① 중앙사무관장기관의 장은 ... 다음 각 호의 사항을 포함한 ... 수립지침을 작성하여 ... 통보하여야 한다."` + `1.`~`5.` 호 목록

로 완전히 재작성됐다. 새로 생긴 `1.`~`5.`는 구법에 애초에 대응하는 문장이
없다 — 그 호 자체가 그때는 존재하지 않았으니까.

**Before(처음 설계)**: 이 경우도 `articles[]` 안에 넣고, 새로 생긴 위치
5개(①본문 + 1.~5.) 전부에 구법의 그 통짜 문장을 **복제**해서 `old_text`로
채운 뒤 `match_status`로만 "이건 못 믿는다"고 표시했다. 문제는 `articles[]`
가 "행 하나 = 위치 하나가 정확히 1:1로 대응한다"는 전제로 설계돼 있어서,
같은 문장이 5번 반복되는 게 사람이 보기엔 마치 처리 오류(버그)처럼 보였고,
`match_status` 안내문을 아무리 정교하게 붙여도 "행 하나 = 위치 하나"라는
기본 전제 자체가 깨진 부분이라 계속 헷갈린다는 지적을 받았다.

**After(현재 설계)**: "구법에 이 위치가 있었는가"는 텍스트 구조만으로
100% 확정 가능한 사실이므로(의미 판단이 필요 없는, 순수 구조적 사실),
이 케이스를 `articles[]`에서 완전히 빼내 별도 배열로 분리했다. `old_text`
는 그룹당 **딱 1번**만 나오고, 새로 생긴 위치들은 `new_items[]`라는
명시적인 배열로 묶인다 — "이건 1:N 그룹이다"가 데이터 구조 자체로
드러나므로, LLM이 굳이 `match_status`를 읽고 추론하지 않아도 애초에
"이 old_text는 여기 여러 개랑 관련 있다"는 게 스키마 모양만 보고 바로
이해된다.

> 참고로 `match_status="위치재배치의심"`(항 신설로 순번이 밀려 신/구가
> 잘못 짝지어졌을 수 있는 경우)은 이렇게 분리하지 않고 `articles[]`에
> 그대로 남아있다 — "구법에 이 위치가 있었는가"와 달리 "이 old가 정말
> 이 new의 개정 전 내용인가"는 문장을 읽어야 아는 의미 판단이라 코드가
> 확정할 수 없기 때문이다(자세한 내용은 아래 `match_status` 설명 참고).

```json
{
  "article_label": "제2조",
  "old_text": "11. \"정보자원\"이란 행정기관등이 보유하고 있는 행정정보, 전자적 수단에 의하여...",
  "new_items": [
    { "clause_no": "", "item_label": "11.", "subitem_label": "", "text": "\"정보자원\"이란 행정기관등이 보유하거나 이용하는 다음 각 목의 자원을 말한다..." },
    { "clause_no": "", "item_label": "11.", "subitem_label": "가.", "text": "가. 행정정보" },
    { "clause_no": "", "item_label": "11.", "subitem_label": "나.", "text": "나. 정보시스템" }
  ]
}
```

- `old_text`: 구법의 통짜 원문 **1개** — `new_items` 각각에 정밀하게 대응하는
  게 아니라, 이 그룹 전체의 개정 전 참고 맥락이다.
- `new_items[]`: 이번 개정으로 새로 생긴 위치들. `clause_no`는 항목마다
  따로 붙는다 — 항 구분조차 없던 조문 하나가 통째로 새 항 여러 개(①②③④
  등)로 재작성되는 경우도 있어서, 그룹 전체가 아니라 항목별로 clause_no가
  다를 수 있기 때문이다(실측: 전자정부법 제56조의3). 각 `text`는 실제
  위치가 확정된 정확한 개정 후 문장이다(이쪽은 항상 신뢰 가능).
- `articles[]`에는 이 그룹에 속한 행이 **하나도 안 섞인다** — LLM이
  `articles[]`만 순회하면 항상 깨끗한 1:1 사실만 보게 된다.

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

전체 워치리스트(102건) 기준 실측(2026-07-20, old_text 호 단위 정밀매칭
반영 후 재검증): 74개 그룹, 법 93건, 조문변경(articles) 887건, 구조확장그룹
22건(추가로 97건이 여기 담김), 미확정 18건, 비교불가 9건 → 약 940KB.
`run_weekly.py`의 실제 운영 모드는
최근 7일치만 조회하므로 평소엔 이보다 훨씬 작다(개정이 없는 주는
`amendment_groups: []`로 사실상 빈 파일). 지금 `out/`에 있는 축소판들은
`weekly_contract_*`(법령 2+행정규칙 1, 3건)가 각 20~56KB, `single_*`
(법령 또는 행정규칙 1건, 조문변경 5건 이하)가 각 2~3KB 수준이다.

## 저장소 구조

```
src/lawtrack/
  config.py           .env 로딩 → Settings(api, db) — 다른 모든 모듈이 여길 통해 설정을 받음

  api/                국가법령정보 Open API HTTP 레이어 (요청/응답 파싱까지, 비즈니스 로직은 없음)
    client.py           공통 HTTP 클라이언트 (LawApiClient) — 인증키(OC) 부착, 재시도, LawApiError
    search.py           목록조회 API — 워치리스트 항목의 최신 일련번호·시행상태 확인
    fulltext.py          법령/행정규칙 "본문조회" API 호출 (전문 JSON 원본을 그대로 반환)
    oldnew.py            "신구법 비교" API 호출 (法제처가 만든 개정 전/후 대비 원본)

  parse/              api/ 가 받아온 원본 JSON을 구조화된 파이썬 객체로 변환 (여기까지는 파싱만, 위치확정 없음)
    fulltext.py          전문 JSON → 조/항/호/목 트리 (parse_articles: 법령 / parse_admrul_units: 행정규칙)
    oldnew.py            신구법 비교 API 응답 → (article_label, change_type, old_text, new_text) 레코드 목록
    jsonutil.py           위 둘이 공유하는 JSON 순회/정규화 유틸

  text/               순수 텍스트 로직 (외부 의존성 없음, 입출력이 전부 str/객체)
    normalize.py          공백·특수문자·순화표기 등 비교 전 정규화
    split.py              조문 원문을 항/호/목 단위 Fragment로 분리 (마커 손실 버그 수정한 파일 — split_all/split_by_item)

  locate/             6단계 가드 파이프라인 — old_text가 신법 본문 어디에 해당하는지 확정 (이 프로젝트의 핵심 로직)
    locator.py            가드 1~6 순서대로 시도, 성공하면 위치 확정 / 전부 실패하면 unresolved로 보고

  db/                 MySQL 접근 계층 — 테이블별 Repo 클래스로 분리 (아래 "테이블 구조" 절 참고)
    conn.py               커넥션 풀 + Database.transaction() 컨텍스트매니저
    repo.py               WatchlistRepo / VersionRepo / ChangeLogRepo / ArticleDiffRepo

  link.py             연쇄개정 그룹핑 — 같은 공포번호로 같이 개정된 법들을 하나의 AmendmentGroup으로 묶음

  detect.py           워치리스트 1건 처리 파이프라인의 지휘자
                       (일련번호 변경 감지 → 본문/신구법 API 호출 → parse → locate → link → DB 저장)

  contract/           DB → LLM팀에게 넘길 최종 JSON 산출 계층
    schema.py             Pydantic 모델 전체 (WeeklyContract 이하 전 스키마, 위 "산출물 구조" 절이 이 파일을 설명함)
    export.py             DB 테이블들을 읽어 위 Pydantic 모델로 조립 (build_contract) — structural_expansions 그룹핑도 여기

scripts/
  run_weekly.py       주간 배치 진입점 — 워치리스트 전체를 detect.process_entry()로 돌리고 build_contract()로 JSON 산출
  run_single_check.py 법령/행정규칙 1건만 디버깅용으로 상세 실행 (locate 가드별 로그까지 출력)
  load_watchlist.py   워치리스트 초기 적재 스크립트 (Windows mysql CLI 한글 인코딩 문제 우회용, seed_watchlist.sql과 내용 동일)
  inspect_article.py  조문번호 필드(가지번호 포함, 예: 제6조의2)의 API 원본 JSON 구조를 그대로 출력해 파서 로직과 맞는지 확인하는 진단 스크립트
  test_live_comparison.py  실제 API를 호출해 locate 파이프라인을 눈으로 검증하는 수동 스크립트

database/
  schema.sql          전체 테이블 정의 (DDL) — 아래 "테이블 구조" 절 참고
  seed_watchlist.sql  워치리스트 초기 데이터 (법령 76건 + 행정규칙 26건, load_watchlist.py와 내용 동일한 데이터를 SQL로 표현)

tests/        pytest, 실측 데이터(실제 API 응답을 고정시킨 fixture) 기반 회귀 테스트. 파일명이 대상 모듈과 1:1 대응
              (예: test_split_jsonutil.py ↔ text/split.py + parse/jsonutil.py, test_export.py ↔ contract/export.py)
```

## 테이블 구조

MySQL 8.0, `database/schema.sql` 기준. 테이블 5개 — 이 프로젝트에서 "진실의
원천"은 항상 `laws`/`administrative_rules`의 전문 JSON이고, 나머지 테이블은
전부 거기서 파생되거나 그 처리 과정을 기록한 것이다.

### `laws` — 법령 전문 아카이브

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `law_id` | VARCHAR(50) | 법령 ID(불변 식별자, PK 일부) |
| `law_serial_no` | VARCHAR(50) | 법령 일련번호(개정마다 바뀜, PK 일부) |
| `law_name` | VARCHAR(255) | 법령명 |
| `law_full_text` | JSON | **법제처 API 원본 그대로** — 가공 없이 저장(진실의 원천, 재파싱 가능하도록 보존) |
| `law_articles_parsed` | JSON | `law_full_text`를 `parse_articles()`로 조/항/호/목 트리로 파싱한 캐시(조회 편의용, 파생값) |
| `db_timestamp` | TIMESTAMP | 삽입/수정 시각 |

- **PK**: `(law_id, law_serial_no)` — 같은 법이라도 일련번호(버전)마다 별도 행.
- 개정이 감지되면 새 일련번호로 새 행이 **추가**되며, 기존 행은 지우지
  않는다 — 즉 매 버전이 그대로 쌓이는 이력 테이블이다.
- **행 존재 여부 자체가 개정감지 신호다**: `VersionRepo.law_exists(law_id,
  new_serial_no)`가 False면 "아직 안 본 버전"이라는 뜻이고, 이게 곧
  "개정됨"으로 판정되는 기준이다. 그래서 최초 구축 시 이 테이블을 절대
  미리 채우면 안 된다(위 "DB 최초 구축 순서" 절 참고).

### `administrative_rules` — 행정규칙 전문 아카이브

`laws`와 완전히 동일한 구조(컬럼명만 `administrative_rule_*` 접두어), 행정규칙
전용. `administrative_rule_articles_parsed`만 파서가 다르다
(`parse_admrul_units` — 행정규칙 원문은 법령과 달리 마크업이 없는 평문이라,
조/항/호/목 트리가 아니라 "위치 라벨 + 텍스트"의 평평한 목록 형태로 파싱됨).

- **PK**: `(administrative_rule_id, administrative_rule_serial_no)`.

### `watchlist` — 감시 대상 목록 (법령 76건 + 행정규칙 26건)

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `law_id` | VARCHAR(50) | **PK.** 법령/행정규칙 ID |
| `law_type` | VARCHAR(50) | 법률/시행령/시행규칙/행정규칙 |
| `official_name` | VARCHAR(255) | 현재 정식 명칭 |
| `internal_name` | VARCHAR(255) | 등록 당시 이름(제명변경 추적용, 산출물의 `internal_name`과 동일 개념) |
| `previous_names` | JSON | 제명변경 이력 배열 |
| `dept_codes` | VARCHAR(255) | 소관부처 코드(콤마 구분, 행정규칙 동명이인 구분용) |
| `status` | VARCHAR(50) | 현행/시행전 등 |
| `successor_law_id` | VARCHAR(50) | 폐지·통합된 경우 후속 법령 ID |
| `scheduled_date` | DATE | 시행예정일(아직 시행 안 된 경우) |
| `last_serial_no` | VARCHAR(50) | 마지막으로 확인한 일련번호 |
| `last_checked_at` | DATETIME | 마지막 확인 일시 |

한 번 등록되면 삭제되지 않는 **단순 목록 테이블**이다(버전 이력이 아니라
"지금 감시 중인 항목이 무엇인가"만 담음). `run_weekly.py`가 매 배치마다
이 테이블 전체를 순회하며 `detect.process_entry()`를 호출하는 시작점.

### `change_log` — 개정 이벤트 로그

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `id` | INT AUTO_INCREMENT | PK |
| `law_id` | VARCHAR(50) | 법령/행정규칙 ID |
| `old_serial_no` / `new_serial_no` | VARCHAR(50) | 개정 전/후 일련번호 |
| `promulgation_no` | VARCHAR(100) | 공포번호(`link.py`가 이 값으로 연쇄개정을 그룹핑) |
| `revision_type` | VARCHAR(50) | 제개정구분(일부개정/전부개정/제정/폐지제정 등) |
| `revision_reason` | TEXT | 법제처 공식 개정이유 원문 그대로(LLM팀이 추론할 필요 없게) |
| `unchanged_clauses` | JSON | `{"제34조": ["①","②"]}` 형태 — 이번에 안 바뀐 항(법령만, 항제개정유형 필드 기준) |
| `comparison_available` | BOOLEAN | 신구법 대비 가능 여부. FALSE면 `article_diff`에 이 버전의 행이 하나도 없다는 뜻이지만, 그 부재만으로는 "애초에 대비 불가"와 "대비했는데 0건 변경"을 구분할 수 없어 별도 컬럼으로 명시(`no_comparison` 산출의 근거) |
| `enforce_date` | DATE | 시행일자 |
| `detected_at` | DATETIME | 이 개정을 감지·기록한 시각 |

한 개정 이벤트(일련번호 변경) = 한 행. `article_diff`의 각 행은 반드시
`change_log`의 어떤 행(같은 `law_id`+`new_serial_no`)에 속한다 — 즉
`change_log`가 "이 버전에 무슨 일이 있었는가"의 헤더이고, `article_diff`가
그 개정의 조문별 세부 내역이다.

### `article_diff` — 조문 단위 diff (파이프라인의 핵심 산출 테이블)

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `law_id`, `law_serial_no` | VARCHAR(50) | 어느 법의 어느 버전(개정 후)인지 |
| `article_code` | VARCHAR(50) | 법제처 원본의 조문코드(내부 식별자) |
| `article_label` | VARCHAR(100) | 사람이 읽는 조 라벨(`제26조의7` 등). 위치를 못 찾으면 `(위치미상#N-M)` |
| `clause_no` / `item_label` / `subitem_label` | VARCHAR(50) | 항/호/목 라벨(없으면 빈 문자열 `''`, NULL 아님 — UNIQUE KEY에 NULL이 섞이면 중복판정이 깨지기 때문) |
| `enforce_date` | DATE | 시행일자 |
| `change_type` | VARCHAR(50) | 개정/신설/삭제/미상 |
| `old_text` / `new_text` | TEXT | 개정 전/후 문장 — `match_status`와 무관하게 항상 순수 원문 그대로 저장됨 |
| `match_status` | VARCHAR(50) | 성공 / 삭제(위치탐색제외) / 구조확장(구법미분리) / 위치재배치의심 |
| `match_detail` | JSON | locate 6가드 중 어느 가드로 확정됐는지, 시도 로그 등 디버깅용 |
| `created_at` | DATETIME | 삽입 시각 |

- **UNIQUE KEY** `(law_id, law_serial_no, article_code, clause_no, item_label,
  subitem_label, enforce_date)` — 같은 버전의 같은 위치가 중복 삽입되는 것을
  막는다(재처리를 여러 번 돌려도 같은 위치는 갱신되지 않고 최초 1행만 유지).
- `contract/export.py`의 `build_contract()`가 이 테이블을 읽어
  `match_status`에 따라 `articles[]`(1:1)와 `structural_expansions[]`(1:N,
  `match_status="구조확장(구법미분리)"` 행들을 `(article_label, old_text)`
  기준으로 그룹핑)로 갈라 담는다 — 자세한 그룹핑 규칙은 위 산출물 구조
  절의 `structural_expansions[]` 설명 참고.

## 테스트

```bash
pytest -q
```
