# NCS MCP Project PRD

작성일: 2026-06-05  
문서 목적: NCS정보망 DB 전체 전처리, 공공데이터포털 API 연계, NCS MCP Server 구축 방향 정의

## 1. 제품 개요

NCS MCP Project는 국가직무능력표준(NCS)을 PDF 또는 단순 텍스트 검색 대상이 아니라, AI가 구조적으로 호출할 수 있는 직무지식 인프라로 전환하는 프로젝트다.

핵심은 NCS정보망 DB 엑셀의 전체 데이터를 정규화하고, 공공데이터포털 API로 능력단위 마스터 정보를 보강한 뒤, MCP Server를 통해 AI가 다음 관계를 직접 조회할 수 있게 만드는 것이다.

```text
분류체계
  └─ 능력단위
       └─ 능력단위요소
            ├─ 수행준거
            └─ KSA(지식·기술·태도)
```

중요한 설계 원칙은 원문 보존이다. NCS DB는 모듈별 품질이 균일하지 않을 수 있으므로, 원문을 덮어쓰지 않고 품질 진단 결과와 LLM 정제본을 별도 레이어로 관리한다.

## 2. 최종 방향

본 프로젝트는 단순한 "NCS DB MCP 연결"이 아니라 다음 구조를 가진다.

```text
원문 보존형 NCS 구조화 DB
  + 품질 진단 레이어
  + LLM 정제본 레이어
  + API 보강 레이어
  + MCP 조회 서버
```

따라서 전체 방향은 다음과 같다.

1. NCS정보망 DB 엑셀 전체를 SQLite에 정규화 적재한다.
2. 공공데이터포털 API를 호출하여 능력단위 코드, 명칭, 수준, 분류체계, 정의를 보강한다.
3. 두 소스는 `능력단위분류번호 = ncsClCd` 기준으로 연결한다.
4. 데이터 품질 이슈를 자동 진단한다.
5. LLM 정제본은 원문과 분리 저장한다.
6. MCP Server는 원문, 정제본, 품질 이슈, API 보강 정보를 모두 조회할 수 있게 한다.

## 3. 데이터 소스

### 3.1 NCS정보망 DB 엑셀

파일:

```text
NCS정보망DB(대분류별,2026년2월).xlsx
```

역할:

```text
전체 NCS 상세 구조의 원천 데이터
```

제공 데이터:

```text
대분류, 중분류, 소분류, 세분류,
능력단위,
능력단위요소,
수행준거,
KSA(지식·기술·태도)
```

확인된 전체 규모:

| 항목 | 수량 |
|---|---:|
| 시트 | 24개 |
| 원본 엑셀 행 | 2,458,668행 |
| 세분류 | 1,109개 |
| 능력단위 | 13,435개 |
| 능력단위요소 | 47,620개 |
| 수행준거 | 196,658개 |
| KSA | 574,279개 |

주의:

엑셀의 한 행은 KSA 1건이 아니라, 능력단위요소 안에서 `수행준거 × KSA`가 조합되어 반복된 원행이다. 따라서 정규화 시 KSA를 수행준거의 직접 하위로 두지 않는다. 수행준거와 KSA는 능력단위요소 기준으로 병렬 연결하고, 원본 행의 조합 관계는 별도 링크 또는 raw row로 보존한다.

### 3.2 공공데이터포털 API

1차 연결 API:

```text
한국산업인력공단_NCS 기준정보 조회
```

URL:

```text
https://apis.data.go.kr/B490007/hrdkapi
```

인증:

```text
공공데이터포털 ServiceKey
```

방식:

```text
REST GET / JSON·XML
```

응답 필드:

| 필드 | 의미 |
|---|---|
| `ncsClCd` | 능력단위코드 |
| `compeUnitName` | 능력단위명 |
| `compeUnitLevel` | 능력단위수준 |
| `ncsLclasCdnm` | 대분류명 |
| `ncsMclasCdnm` | 중분류명 |
| `ncsSclasCdnm` | 소분류명 |
| `ncsSubdCdnm` | 세분류명 |
| `compeUnitDef` | 능력단위정의 |

갱신 주기:

```text
연 2회 (2월, 8월)
```

중요한 설계 판단:

승인된 `NCS 기준정보 조회` API는 대·중·소·세분류와 능력단위분류코드 조회를 제공한다. 이 중 `/NCS005`는 `NCS_LCLAS_CD`, `NCS_MCLAS_CD`, `NCS_SCLAS_CD`, `NCS_SUBD_CD`를 필수 파라미터로 받아 능력단위코드, 능력단위명, 수준, 정의를 제공하므로 엑셀 DB 보강에 가장 적합하다. 엑셀을 기준 데이터로 두고 API는 left join 방식으로 연결한다.

## 4. API 연결 전략

### 4.1 1차 API

MVP가 아니라 전체 전처리 기준에서도 1차 API는 다음으로 확정한다.

```text
한국산업인력공단_NCS 기준정보 조회 / NCS005
```

이 API를 선택하는 이유:

1. 엑셀의 `능력단위분류번호`와 API의 `NCS_CL_CD`가 직접 조인된다.
2. 능력단위 정의(`COMPE_UNIT_DEF`)를 제공한다.
3. 능력단위 수준과 분류체계 검증에 사용할 수 있다.
4. 프로젝트 초기 목적과 가장 직접적으로 맞는다.

### 4.2 API 호출 방식

요청 예시:

```text
GET https://apis.data.go.kr/B490007/hrdkapi/NCS005?serviceKey={SERVICE_KEY}&pageNo=1&numOfRows=100&returnType=JSON&NCS_LCLAS_CD=02&NCS_MCLAS_CD=02&NCS_SCLAS_CD=02&NCS_SUBD_CD=01
```

환경변수:

```text
NCS_SERVICE_KEY=공공데이터포털_인증키
```

수집 절차:

1. `pageNo=1`, `numOfRows=100`부터 호출한다.
2. 응답의 `totalCount`를 확인한다.
3. 전체 페이지를 반복 호출한다.
4. API 원문 응답을 `api_raw_responses`에 저장한다.
5. 정제된 API 능력단위 정보를 `api_competency_units`에 upsert한다.
6. `api_competency_units.ncs_cl_cd = competency_units.unit_code` 기준으로 연결한다.
7. 조인 성공률, 미매칭 API 건수, 미매칭 엑셀 건수를 리포트로 생성한다.

### 4.3 API 조인 정책

엑셀 기준 left join을 사용한다.

```sql
competency_units.unit_code = api_competency_units.ncs_cl_cd
```

조인 정책:

| 상황 | 처리 |
|---|---|
| 엑셀과 API 모두 존재 | API 정의와 분류 정보를 보강 |
| 엑셀에는 있고 API에는 없음 | 엑셀 원문 기준 유지, `api_match_status = unmatched` |
| API에는 있고 엑셀에는 없음 | 별도 API orphan 데이터로 저장 |
| 명칭·수준·분류 불일치 | 품질 이슈로 기록 |

### 4.4 2차 확장 API

초기 구축 이후 필요 시 다음 API를 검토한다.

| API | 활용 가능성 | 적용 시점 |
|---|---|---|
| 국가기술자격시험_공통 NCS 정보 | NCS 최종연계일자, HRDNET 연계일자, 과정평가형·일학습병행 편성수 보강 | 자격·훈련 연계 확장 시 |
| NCS 기준정보 조회 | 대·중·소·세분류 기준정보 검증 | 분류체계 정합성 강화 시 |
| NCS 직업기초능력 | 직업기초능력 정보 보강 | 직업기초능력 분석 기능 추가 시 |
| NCS 고교직업교육과정 정보 서비스 | 고교 교육과정 연계 | 교육과정 응용 서비스 확장 시 |

초기에는 API를 여러 개 붙이지 않는다. 전체 전처리의 기준은 엑셀이며, 승인된 `NCS 기준정보 조회 / NCS005`만 연결해도 능력단위 마스터 보강과 조인 검증에는 충분하다.

## 5. 데이터베이스 설계

초기 DB는 SQLite로 구축한다. 전체 246만 원행 규모는 SQLite로도 처리 가능하다. 다만 운영 서비스나 다중 사용자 환경으로 확장할 경우 PostgreSQL 전환을 고려한다.

DB 파일:

```text
data/processed/ncs.db
```

### 5.1 주요 테이블

#### `raw_excel_rows`

엑셀 원문 행을 그대로 보존한다.

주요 컬럼:

```text
raw_row_id
source_file
sheet_name
sheet_row_number
major_code
major_name
middle_code
middle_name
small_code
small_name
sub_code
sub_name
unit_code
unit_name
unit_level
element_code
element_name
element_level
criteria_no
criteria_text
ksa_type_code
ksa_type_name
ksa_no
ksa_text
loaded_at
```

#### `classifications`

대·중·소·세분류를 저장한다.

```text
classification_id
major_code
major_name
middle_code
middle_name
small_code
small_name
sub_code
sub_name
```

#### `competency_units`

능력단위 마스터 테이블이다.

```text
unit_code
base_unit_code
unit_version
unit_name_raw
unit_level_raw
classification_id
api_unit_name
api_unit_level
api_definition
api_match_status
created_at
updated_at
```

`base_unit_code`와 `unit_version`은 `0202020101_23v3` 같은 코드를 분해하여 저장한다.

#### `competency_elements`

능력단위요소 테이블이다.

```text
element_id
unit_code
element_no
element_code_raw
element_name_raw
element_level_raw
```

#### `performance_criteria`

수행준거 테이블이다.

```text
criteria_id
element_id
criteria_no
criteria_text_raw
criteria_text_refined
review_status
```

#### `ksa_items`

KSA 테이블이다.

```text
ksa_id
element_id
ksa_type_code
ksa_type_name
ksa_no
ksa_text_raw
ksa_text_refined
review_status
```

#### `element_criteria_ksa_links`

엑셀 원행에서 나타난 수행준거와 KSA의 조합 관계를 보존한다.

```text
link_id
raw_row_id
element_id
criteria_id
ksa_id
```

이 테이블은 원본 엑셀의 반복 구조를 손실 없이 추적하기 위한 것이다. MCP의 기본 지식 구조는 능력단위요소 아래에 수행준거와 KSA를 병렬로 제공하되, 필요한 경우 원본 행 조합도 조회할 수 있게 한다.

#### `api_competency_units`

API에서 수집한 능력단위 정보를 저장한다.

```text
ncs_cl_cd
compe_unit_name
compe_unit_level
ncs_lclas_cdnm
ncs_mclas_cdnm
ncs_sclas_cdnm
ncs_subd_cdnm
compe_unit_def
api_fetched_at
```

#### `quality_issues`

품질 진단 결과를 저장한다.

```text
issue_id
target_type
target_id
issue_type
severity
issue_detail
suggested_action
detected_at
resolved_at
```

품질 이슈 유형:

```text
missing_required_value
duplicate_text
short_ksa
double_space
suspected_typo
classification_mismatch
api_unmatched
api_value_mismatch
criteria_format_issue
ambiguous_ksa
```

#### `refinement_jobs`

LLM 정제 작업 로그를 저장한다.

```text
job_id
target_type
target_id
model_name
prompt_version
input_hash
output_text
review_status
created_at
```

## 6. 전처리 파이프라인

### 6.1 전체 흐름

```text
1. 엑셀 전 시트 스트리밍 로드
2. raw_excel_rows 적재
3. 분류체계 정규화
4. 능력단위 정규화
5. 능력단위요소 정규화
6. 수행준거 정규화
7. KSA 정규화
8. 원행 조합 링크 생성
9. 품질 진단 실행
10. API 수집 및 조인
11. 조인/품질 리포트 생성
```

### 6.2 구현 방식

Python 패키지:

```text
openpyxl
sqlite3
requests
python-dotenv
```

전처리 방식:

1. `openpyxl.load_workbook(..., read_only=True, data_only=True)`로 메모리 사용을 줄인다.
2. 24개 시트를 순차 처리한다.
3. 원문 문자열은 그대로 저장한다.
4. 키 생성용 문자열만 공백 정규화한다.
5. `INSERT OR IGNORE` 또는 upsert로 중복을 제거한다.
6. 대량 적재 시 트랜잭션을 묶어 처리한다.
7. 인덱스를 적재 후 생성하여 속도를 확보한다.

### 6.3 산출물

```text
data/processed/ncs.db
reports/preprocess_summary.json
reports/preprocess_summary.md
reports/api_join_report.md
reports/quality_issues.md
```

## 7. 품질 진단 전략

품질 진단은 MCP 이전에 반드시 수행한다. NCS DB 자체가 모듈별로 품질이 불균일할 수 있기 때문이다.

### 7.1 1차 규칙 기반 진단

규칙 예시:

| 진단 항목 | 설명 |
|---|---|
| 필수값 누락 | 능력단위코드, 요소명, 수행준거, KSA 누락 |
| 짧은 KSA | 공백 제거 후 6자 이하 |
| 반복 KSA | 동일 KSA가 여러 요소에 과다 반복 |
| 수행준거 형식 | "할 수 있다" 문장 형식 여부 |
| 문장부호 | 수행준거 마침표 누락 |
| 이중 공백 | 불필요한 공백 |
| 의심 오탈자 | 사전 기반 또는 패턴 기반 탐지 |
| API 불일치 | API와 엑셀의 명칭·수준·분류 차이 |

### 7.2 LLM 정제 원칙

LLM은 새 직무지식을 생성하지 않는다. 역할은 원문 기반 정제와 품질 보조 판단으로 제한한다.

정제 원칙:

1. 원문을 삭제하거나 덮어쓰지 않는다.
2. 정제본은 `*_refined` 컬럼 또는 별도 테이블에 저장한다.
3. 정제 이유를 `quality_issues` 또는 `refinement_jobs`에 남긴다.
4. 신뢰도가 낮은 정제는 `review_required` 상태로 둔다.
5. 연구 및 감사 가능성을 위해 원문과 정제본을 항상 비교 가능하게 한다.

## 8. MCP Server 설계

### 8.1 구현 기술

언어:

```text
Python
```

주요 구성:

```text
SQLite DB
Python MCP SDK
stdio transport
Claude Desktop 연결
```

서버 이름:

```text
ncs-mcp
```

### 8.2 MCP 도구 목록

#### `list_classifications`

분류체계 목록을 조회한다.

입력:

```text
major_name?
middle_name?
small_name?
sub_name?
```

출력:

```text
대·중·소·세분류 목록과 각 분류의 능력단위 수
```

#### `get_competency_units`

조건에 맞는 능력단위 목록을 조회한다.

입력:

```text
major_name?
middle_name?
small_name?
sub_name?
level_min?
level_max?
keyword?
api_match_status?
```

출력:

```text
unit_code, unit_name, level, classification, api_definition
```

#### `get_unit_structure`

특정 능력단위의 전체 구조를 조회한다.

입력:

```text
unit_code
text_version = raw | refined | both
include_quality_issues = true | false
```

출력:

```text
능력단위
  └─ 능력단위요소
       ├─ 수행준거
       └─ KSA
```

#### `get_element_detail`

능력단위요소 상세를 조회한다.

입력:

```text
element_id
text_version = raw | refined | both
```

출력:

```text
요소명, 요소수준, 수행준거 목록, KSA 목록
```

#### `get_performance_criteria`

수행준거를 조회한다.

입력:

```text
unit_code?
element_id?
keyword?
```

출력:

```text
criteria_id, criteria_no, criteria_text
```

#### `get_ksa`

KSA를 조회한다.

입력:

```text
unit_code?
element_id?
ksa_type = 지식 | 기술 | 태도 | null
keyword?
```

출력:

```text
ksa_id, ksa_type, ksa_no, ksa_text
```

#### `search_ncs`

NCS 전체 텍스트를 검색한다.

입력:

```text
query
scope = unit | element | criteria | ksa | all
limit
```

출력:

```text
검색 결과와 소속 계층 경로
```

#### `get_quality_issues`

품질 이슈를 조회한다.

입력:

```text
target_type?
unit_code?
issue_type?
severity?
limit?
```

출력:

```text
품질 이슈 목록, 대상 원문, 권장 조치
```

#### `compare_raw_refined`

원문과 정제본을 비교한다.

입력:

```text
target_type
target_id
```

출력:

```text
raw_text, refined_text, issue_detail, review_status
```

#### `get_api_join_status`

API 조인 상태를 조회한다.

입력:

```text
unit_code?
classification_filter?
```

출력:

```text
API 매칭 여부, API 정의, 불일치 항목
```

## 9. Claude Desktop 연결

Claude Desktop 설정 예시:

```json
{
  "mcpServers": {
    "ncs-mcp": {
      "command": "python",
      "args": [
        "C:/Workplace/NCS_MCP/src/ncs_mcp/server.py"
      ],
      "env": {
        "NCS_DB_PATH": "C:/Workplace/NCS_MCP/data/processed/ncs.db"
      }
    }
  }
}
```

실제 경로는 설치 위치에 맞춰 조정한다.

## 10. 개발 산출물 구조

권장 디렉터리 구조:

```text
C:/Workplace/NCS_MCP/
  docs/
    NCS_MCP_PRD.md
  src/
    ncs_mcp/
      __init__.py
      config.py
      db.py
      preprocess_excel.py
      collect_api.py
      quality.py
      server.py
  data/
    processed/
      ncs.db
  reports/
    preprocess_summary.md
    api_join_report.md
    quality_issues.md
  tests/
    test_preprocess.py
    test_api_join.py
    test_mcp_tools.py
  .env.example
  requirements.txt
  README.md
```

원본 엑셀 파일은 200MB 규모이므로 저장소에 커밋하지 않는다. 로컬 경로 또는 `data/raw/`에 별도 배치하되, git 추적에서는 제외한다.

## 11. 수용 기준

### 11.1 전처리 수용 기준

전처리 완료 조건:

1. 24개 시트 전체가 처리된다.
2. 원본 2,458,668행이 `raw_excel_rows`에 적재된다.
3. 고유 능력단위 13,435개가 생성된다.
4. 고유 능력단위요소 47,620개가 생성된다.
5. 고유 수행준거 196,658개가 생성된다.
6. 고유 KSA 574,279개가 생성된다.
7. 원본 행과 정규화 테이블 간 추적이 가능하다.
8. 전처리 요약 리포트가 생성된다.

### 11.2 API 수용 기준

API 연계 완료 조건:

1. `NCS 기준정보 조회 / NCS005` API를 세분류별로 수집한다.
2. API 원문 응답을 저장한다.
3. `NCS_CL_CD` 기준으로 엑셀 능력단위와 조인한다.
4. API 매칭률과 미매칭 목록을 리포트로 생성한다.
5. 명칭, 수준, 분류체계 불일치를 품질 이슈로 기록한다.

### 11.3 MCP 수용 기준

MCP 완료 조건:

1. Claude Desktop에서 `ncs-mcp` 서버가 연결된다.
2. 능력단위 목록 조회가 가능하다.
3. 특정 능력단위의 요소, 수행준거, KSA 전체 구조 조회가 가능하다.
4. KSA 유형별 필터링이 가능하다.
5. 품질 이슈 조회가 가능하다.
6. 원문과 정제본 비교가 가능하다.
7. API 조인 상태 조회가 가능하다.

## 12. 개발 단계

### Phase 1. 전체 전처리 기반 구축

목표:

```text
엑셀 전체를 SQLite에 원문 보존형으로 적재
```

작업:

1. DB 스키마 작성
2. 엑셀 스트리밍 로더 작성
3. raw table 적재
4. 정규화 테이블 생성
5. 전처리 요약 리포트 생성

### Phase 2. API 연계

목표:

```text
NCS 기준정보 조회 / NCS005 API 수집 및 능력단위 보강
```

작업:

1. API 수집기 작성
2. API 응답 저장
3. 능력단위 조인
4. API 매칭 리포트 생성

### Phase 3. 품질 진단

목표:

```text
NCS DB 품질 불균일 이슈 자동 탐지
```

작업:

1. 규칙 기반 품질 진단
2. 품질 이슈 테이블 적재
3. 품질 리포트 생성
4. HR 인사 분야 샘플 검토

### Phase 4. MCP Server

목표:

```text
AI가 NCS 구조를 직접 조회할 수 있는 MCP Server 구현
```

작업:

1. MCP 서버 골격 작성
2. 핵심 조회 도구 구현
3. 품질 조회 도구 구현
4. Claude Desktop 연결
5. 샘플 질의 테스트

### Phase 5. LLM 정제 레이어

목표:

```text
원문 추적성을 유지하면서 정제본 병행 저장
```

작업:

1. 정제 프롬프트 설계
2. 정제 결과 저장 구조 구현
3. 원문/정제본 비교 도구 구현
4. 사람 검토 상태 관리

## 13. 주요 리스크와 대응

| 리스크 | 설명 | 대응 |
|---|---|---|
| API 필수 파라미터 누락 | `/NCS005`는 세분류 코드 4종을 모두 요구함 | 엑셀의 분류코드에서 `NCS_LCLAS_CD`, `NCS_MCLAS_CD`, `NCS_SCLAS_CD`, `NCS_SUBD_CD`를 생성 |
| KSA 계층 오해 | 엑셀 원행만 보면 KSA가 수행준거 하위처럼 보일 수 있음 | KSA는 능력단위요소 하위 병렬 구조로 모델링 |
| 데이터 품질 불균일 | 오탈자, 중복, 짧은 KSA, 분류 불일치 가능 | 품질 진단 레이어와 정제본 레이어 분리 |
| 대용량 엑셀 처리 | 246만 행 규모로 메모리 부담 가능 | read_only 스트리밍, 트랜잭션 배치, 인덱스 후생성 |
| LLM 정제 환각 | 원문에 없는 내용을 생성할 수 있음 | 원문 기반 정제만 허용, 정제본 분리 저장, 검토 상태 관리 |

## 14. 결론

전체 프로젝트는 가능하다. 다만 API를 전체 원천으로 보면 안 된다. 전체 NCS 상세 구조의 기준은 NCS정보망 DB 엑셀이고, API는 능력단위 마스터 정보 보강과 검증에 사용해야 한다.

최종 방향은 다음 한 문장으로 정리된다.

```text
NCS MCP는 NCS 원문을 보존하면서 전체 직무지식 구조를 정규화하고, API 보강·품질 진단·LLM 정제 가능성을 함께 제공하는 AI 호출형 직무지식 인프라다.
```
