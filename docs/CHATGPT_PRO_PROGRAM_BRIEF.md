# NCS-SQF Ontology MCP Program Brief

이 문서는 ChatGPT Pro 또는 다른 설계 검토용 LLM에게 현재 프로그램을 정확히 설명하고, 다음에 Codex에게 줄 추가 프롬프트를 뽑기 위한 전달 문서다.

## 한 줄 설명

이 프로그램은 NCS 능력단위 데이터와 SQF 산업별역량체계 데이터를 SQLite 지식베이스로 정규화하고, SQF 직무수준과 NCS 능력단위 사이의 근거 기반 후보 매핑을 만들어 MCP 서버와 대시보드에서 검색, 교육 추천, 역량 갭분석에 활용하는 프로젝트다.

## 목표

최종 KPI는 사용자가 원하는 업무를 물었을 때 다음을 근거와 함께 반환하는 것이다.

- 관련 SQF 직무/직무수준
- 관련 NCS 능력단위
- 능력단위요소, 수행준거, 지식/기술/태도(KSA)
- 직접 SQF 근거: 교육훈련, 자격, 학위, 경력
- SQF 직접 근거가 비어 있을 때 NCS 기반 보완 학습목표
- 매핑 confidence, score, evidence, review_status

이 프로그램은 공식 인정 판정기가 아니라 근거 기반 역량 탐색, 교육 추천, 갭분석 보조 도구다.

## 현재 구현 상태

### 데이터

현재 로컬 SQLite DB:

```text
data/processed/ncs.db
```

핸드오프 패키지:

```text
exports/ncs_sqf_output/
  data/db/ncs_sqf.sqlite
  sql/schema.sql
  sql/indexes.sql
  sql/sample_queries.sql
  docs/schema.md
  docs/data_dictionary.md
  manifest.json
  README.md
```

주요 카운트:

```text
NCS 능력단위: 13,435
NCS 능력단위요소: 47,620
수행준거: 196,658
KSA: 574,279
SQF 직무수준: 2,397
NCS-SQF 후보 매핑: 67
```

### API

NCS 기준정보 API와 SQF `/openapi26` API를 수집한다.

SQF 실제 응답은 Swagger 예시와 다르게 최상위 `data` 배열과 `dataInfo` 객체를 사용한다. 정상 코드는 `000`, 빈 데이터는 `002 empty data`다.

API 키는 `.env`에만 저장하고 커밋하지 않는다.

```text
NCS_SERVICE_KEY=
NCS_SQF_SERVICE_KEY=
```

### 첫 MVP 범위

첫 MVP는 경영지원 분야다.

```text
SQF: 02 경영·회계·사무 > 경영관리 > 경영지원
NCS: 02 경영·회계·사무
```

현재 경영지원 SQF 직무수준 7건에 대해 NCS 후보 매핑 67건이 생성되어 있다.

### MCP 서버

현재 MCP 서버는 SQLite를 조회한다.

주요 도구:

```text
list_classifications
get_competency_units
get_unit_structure
get_element_detail
get_performance_criteria
get_ksa
search_ncs
get_quality_issues
compare_raw_refined
get_api_join_status
get_sqf_duties
search_sqf_jobs
get_sqf_job_level
build_sqf_ncs_mapping_candidates
map_sqf_to_ncs
analyze_gap
recommend_next_ncs_units
explain_mapping
recommend_education_for_duty
```

Resources:

```text
ontology://schema
sqf://mvp/management-support
```

Prompt:

```text
sqf_gap_report_prompt
```

### 대시보드

대시보드는 NCS-SQF 온톨로지 작업 현황을 보여주는 워크벤치다.

실행:

```powershell
python scripts\ncs_dashboard.py --host 127.0.0.1 --port 8765
```

기능:

- NCS 전처리 진행률
- API 매칭 상태
- SQF 직무수준 현황
- 경영지원 MVP 현황
- NCS-SQF 후보 매핑 현황
- 품질 이슈 목록
- refined 필드 수작업 정제

### LLM 정제 하네스

NCS DB 자체의 품질과 일관성 문제를 고려해 정제 구조를 만들었다.

원문은 절대 덮어쓰지 않고 refined 필드에 별도 저장한다.

현재 구조:

```text
quality_issues
  -> refinement_jobs
  -> refined fields
  -> review_status
```

관련 명령:

```powershell
python scripts\ncs_harness.py refine stats
python scripts\ncs_harness.py refine generate --issue-types double_space --limit 3000
python scripts\ncs_harness.py refine apply --min-confidence 0.99 --limit 3000
```

현재 상태:

```text
refinement_jobs 총 2,852건 적용됨
double_space 정제 job은 refined 필드에 model_refined 상태로 적용됨
criteria_format_issue 6,491건은 review_required 정제 후보로 생성됨
open_quality_issues에서 double_space는 제거됨
남은 주요 이슈는 duplicate_text, short_ksa, criteria_format_issue처럼 의미 검토가 필요한 항목임
```

기본 provider는 `local-rule`이다. 현재 외부 LLM API를 직접 호출하지 않고, 공백 정규화나 문장부호 같은 안전한 기계적 정제만 후보로 만든다. 의미 판단이 필요한 `short_ksa`, `duplicate_text`, `api_value_mismatch` 등은 사람이 검토해야 한다.

## 중요한 설계 원칙

1. 원문 필드는 수정하지 않는다.
2. 정제본은 `*_refined` 필드에만 저장한다.
3. NCS-SQF는 `sameAs`로 단정하지 않는다.
4. SQF와 NCS 연결은 별도 매핑 객체에 저장한다.
5. 모든 추천에는 evidence, confidence, score, review_status를 포함해야 한다.
6. candidate 매핑과 reviewed/human_reviewed 매핑을 구분한다.
7. 공식 인정/평가 판정과 교육 추천/갭분석은 분리한다.

## 현재 확인된 문제

### 1. 매핑 후보 품질

경영지원 MVP 후보 매핑은 구조적으로 연결됐지만, 일부 낮은 점수 후보가 추천/갭분석에 섞일 수 있다.

예:

```text
사무행정(3) -> 육상운송관리
score: 5.0
relation: related
```

따라서 `analyze_gap`, `recommend_next_ncs_units`는 기본적으로 다음 필터가 필요하다.

```text
score >= 7
relation != related
review_status != rejected
```

### 2. SQF 직접 교육훈련 필드 부족

SQF의 `duty_education_training`, `duty_qualification`, `duty_career`는 많은 행에서 비어 있다. 따라서 교육 추천은 SQF 필드만으로 만들면 안 되고, NCS 능력단위/요소/수행준거/KSA를 학습목표로 변환해야 한다.

### 3. LLM 정제 미완성

현재는 LLM 정제 구조와 로컬 규칙 provider만 있다. 실제 LLM API provider, batch JSONL export/import, human review UI는 아직 더 보강해야 한다.

## ChatGPT Pro에 붙여넣을 질문 프롬프트

아래 프롬프트를 ChatGPT Pro에 그대로 붙여넣고, Codex에게 줄 추가 프롬프트를 설계해 달라고 요청한다.

```text
아래는 내가 만들고 있는 NCS-SQF 온톨로지 MCP 프로그램 설명이다.

목표는 사용자가 원하는 업무를 물었을 때 관련 SQF 직무수준, NCS 능력단위, 능력단위요소, 수행준거, KSA, 교육훈련/자격/경력 근거를 연결해 교육 추천과 역량 갭분석을 제공하는 것이다.

현재 구조:
- NCS Excel/API와 SQF API를 SQLite로 정규화한다.
- MCP 서버는 실시간 API가 아니라 SQLite 지식베이스를 조회한다.
- 첫 MVP는 SQF 02 경영·회계·사무 > 경영관리 > 경영지원이다.
- 현재 DB에는 NCS 능력단위 13,435건, SQF 직무수준 2,397건, 경영지원 MVP 후보 매핑 67건이 있다.
- 매핑은 sameAs가 아니라 sqf_ncs_matches 테이블에 relation, score, confidence, evidence_text, review_status로 저장한다.
- 공식 인정 판정이 아니라 근거 기반 추천/갭분석 보조 도구다.
- NCS DB 품질 이슈가 있어서 quality_issues와 refinement_jobs 기반 LLM 정제 루프를 만들고 있다.
- 원문은 수정하지 않고 refined 필드에 정제본을 저장한다.
- 현재 local-rule provider로 double_space 같은 기계적 정제 job을 만들었고, 안전한 공백 정규화 2,852건은 model_refined 상태로 적용했다.
- criteria_format_issue 6,491건은 review_required 큐에 올렸고, 자동 적용하지 않았다.
- open_quality_issues에서 double_space는 제거됐고, duplicate_text/short_ksa/criteria_format_issue 등 의미 검토가 필요한 이슈가 남아 있다.

현재 고민:
1. NCS-SQF 후보 매핑 품질을 높이고 싶다.
2. 낮은 점수 related 후보가 교육 추천에 섞이지 않게 하고 싶다.
3. SQF 교육훈련 필드가 비어 있을 때 NCS KSA를 학습목표로 바꾸는 추천 로직이 필요하다.
4. NCS 원문 품질이 낮은 일부 모듈을 LLM으로 정제하고 싶다.
5. 대시보드에서 사람이 매핑과 정제 결과를 검토/승인하게 만들고 싶다.
6. 이 프로그램을 나중에 실제 교육 추천 MCP 서버로 안정화하고 싶다.

너는 이 프로젝트의 아키텍처 리뷰어이자 프롬프트 설계자다.

내가 Codex에게 순서대로 줄 추가 작업 프롬프트를 만들어줘.

요구사항:
- 한 번에 너무 큰 프롬프트 하나가 아니라, 5~8개의 단계별 Codex 프롬프트로 나눠줘.
- 각 프롬프트는 목적, 구현 범위, 변경 파일, 테스트 기준, 완료 기준을 포함해줘.
- 특히 다음 주제를 포함해줘.
  1. 매핑 후보 필터링과 갭분석 품질 개선
  2. 인사 분야까지 SQF-NCS 매핑 확장
  3. LLM 정제 provider 설계 또는 JSONL batch export/import
  4. refined 데이터 우선 조회 정책
  5. 교육 추천 로직: SQF 직접근거 + NCS KSA 학습목표 변환
  6. 대시보드 human review workflow
  7. 평가 지표와 회귀 테스트
- 공식 인정 판정과 추천/갭분석 보조를 분리하도록 주의사항을 넣어줘.
- API 키나 개인정보를 출력/커밋하지 않도록 보안 주의사항도 넣어줘.

출력은 바로 Codex에 붙여넣을 수 있는 프롬프트 묶음 형태로 작성해줘.
```

## Codex에게 바로 줄 수 있는 다음 후보 작업

ChatGPT Pro 답변을 기다리지 않고 바로 진행한다면, 다음 순서가 가장 현실적이다.

1. `analyze_gap`과 `recommend_next_ncs_units`에서 낮은 점수 후보 제외.
2. 경영지원 후보 67건 중 `score < 7` 후보를 `review_status='rejected'` 또는 `low_confidence`로 분리.
3. SQF `인사` 직무까지 매핑 후보 생성 확장.
4. LLM 정제 JSONL export/import 추가.
5. refined 데이터를 MCP 조회 기본값으로 쓸지 선택하는 정책 추가.
6. 대시보드에서 refinement_jobs 승인/반려 UI 추가.
