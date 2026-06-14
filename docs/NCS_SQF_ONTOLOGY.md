# NCS-SQF Ontology Design

## 목적

NCS-SQF 온톨로지의 1차 목표는 공식 인정 판정이 아니라 역량 탐색, 교육 추천, 부족역량 설명이다. 사용자가 원하는 업무를 물으면 시스템은 관련 SQF 직무수준, NCS 능력단위, 능력단위요소, 수행준거, KSA, 교육훈련, 자격, 경력 조건을 근거와 함께 반환해야 한다.

`미래 교육 품질, NCS에서 길을 찾다.pdf`의 관점도 이 목적을 뒷받침한다. NCS는 학벌·스펙 중심이 아니라 직무능력 중심의 능력사회를 만들기 위한 기준이고, KQF는 학력·자격·현장경력·교육훈련 이수 결과를 상호 연계하는 국가 수준 체계다. SQF는 KQF의 산업별 구현 단위로서 산업 현장의 직무를 도출·표준화하고, 교육훈련-학위-자격-현장경력을 직무수준에 연결하는 골격이다. 따라서 이 온톨로지는 교육 추천을 “강의명 나열”로 끝내지 않고 직무수준, 직무역량, 능력단위, KSA, 인정 근거, 보고서 근거를 함께 설명해야 한다.

PDF 요약의 기준을 설계 원칙으로 둔다.

- 값 나열보다 관계 그래프를 우선한다.
- 데이터 사일로를 없애고 의미 관계를 연결한다.
- 추천 결과는 설명 가능해야 한다.
- 멀티에이전트와 LLM이 공유할 수 있는 지식 표현을 만든다.
- 산업 지식 독립성을 위해 원천 데이터, 매핑 근거, 버전을 보존한다.

## 현재 데이터 관찰

SQF `/openapi26` 실제 응답은 최상위 `data` 배열과 `dataInfo` 객체로 온다. 정상 코드는 `000`, 빈 데이터는 `002 empty data`다.

현재 수집 결과:

- NCS 대분류 24개 중 SQF 데이터 제공 15개, 빈 데이터 9개.
- `sqf_duties` 총 2,397건.
- `duty_definition` 보유 1,360건.
- `duty_education_training` 보유 135건.
- `duty_qualification` 보유 135건.
- `duty_career` 보유 111건.
- `duty_license` 보유 103건.
- `sqf_job_levels_normalized` 2,397건.
- SQF 자료실 문서 105건.
- PDF/OCR/HWP 문서 자산 125개 모두 extracted.
- 문서 청크 9,105건.
- 문서 청크와 SQF 직무수준 근거 매칭 49,940건.
- SQF-NCS 후보 매핑 22,642건.

따라서 교육 추천은 SQF의 교육훈련 필드만으로 만들 수 없다. 교육훈련 필드가 비어 있는 SQF 직무는 NCS 능력단위, 능력단위요소, 수행준거, KSA를 학습 목표로 변환해 보완 추천해야 한다.

## MVP 범위

1차 MVP는 경영지원 분야다.

```text
SQF: 02 경영·회계·사무 > 경영관리 > 경영지원
NCS: 02 경영·회계·사무
```

현재 `sqf_duties` 기준 경영지원 SQF 직무수준은 7건이다. 이 범위에서 매핑 객체, 대시보드 검토, 교육 추천 근거 응답을 먼저 완성한다.

## 권장 기술 모델

초기 구현은 SQLite에 materialized graph를 둔다. 현재는 JSON-LD export와 readiness validation을 제공한다. RDF/SKOS/OWL/SHACL은 JSON-LD 구조를 기반으로 다음 단계에서 확장한다.

- SKOS: NCS 분류체계와 SQF 산업/직무 체계의 `broader`, `narrower`, `related`, `exactMatch`, `closeMatch`.
- OWL: 직무, 능력단위, KSA, 교육훈련, 자격, 경력 조건의 클래스와 속성.
- SHACL: 필수 관계와 값 조건 검증. 예: SQF 직무수준은 NCS 대분류와 연결되어야 한다.

## 노드 모델

```text
NCSMajor
NCSClassification
NCSCompetencyUnit
NCSCompetencyElement
NCSPerformanceCriterion
NCSKSA

SQFField
SQFJob
SQFDuty
SQFDutyLevel
SQFEducationTraining
SQFQualification
SQFCareerRequirement

MappingEvidence
UserGoal
Recommendation
```

## 관계 모델

```text
NCSMajor broader/narrower NCSClassification
NCSClassification hasUnit NCSCompetencyUnit
NCSCompetencyUnit hasElement NCSCompetencyElement
NCSCompetencyElement hasPerformanceCriterion NCSPerformanceCriterion
NCSCompetencyElement requiresKnowledge NCSKSA
NCSCompetencyElement requiresSkill NCSKSA
NCSCompetencyElement requiresAttitude NCSKSA

SQFField hasJob SQFJob
SQFJob hasDuty SQFDuty
SQFDuty hasDutyLevel SQFDutyLevel
SQFDutyLevel mappedToMajor NCSMajor
SQFDutyLevel requiresNCSUnit NCSCompetencyUnit
SQFDutyLevel partiallyCovers NCSCompetencyUnit
SQFDutyLevel closeMatch NCSCompetencyUnit
SQFDutyLevel hasTraining SQFEducationTraining
SQFDutyLevel hasQualification SQFQualification
SQFDutyLevel hasCareerRequirement SQFCareerRequirement
```

## 매핑 객체

NCS와 SQF는 입도가 다르기 때문에 단순 `sameAs`를 기본값으로 쓰지 않는다. 모든 연결은 독립 매핑 객체로 저장한다.

```text
Mapping
  source_node_id
  target_node_id
  relation
  confidence
  score
  match_method
  evidence_text
  evidence_source
  source_version
  review_status
```

권장 relation:

- `requiresNCSUnit`: SQF 직무수준 수행에 필요한 NCS 능력단위.
- `partiallyCovers`: 일부 수행준거나 KSA만 겹치는 경우.
- `closeMatch`: 명칭/정의가 강하게 유사하지만 공식 매핑은 아닌 경우.
- `related`: 약한 관련성.

권장 confidence:

- `official`: 공식 API나 문서에 명시된 관계.
- `reviewed`: 사람이 검토한 관계.
- `lexical`: 명칭/정의 텍스트 기반 자동 매칭.
- `inferred`: 그래프 추론이나 LLM 보조 추론.

## 추천 흐름

```text
사용자 업무 질의
  -> SQF 직무/직무수준 후보 검색
  -> SQF-NCS 매핑 조회
  -> 직접 교육훈련/자격/경력 조건 확인
  -> NCS 능력단위요소, 수행준거, KSA 확장
  -> 사용자가 보유한 역량과 비교
  -> 부족 능력단위/KSA 기반 교육 추천
  -> 근거와 confidence 포함 응답
```

## MVP 순서

1. `sqf_duties` 수집과 필드 충실도 리포트.
2. `sqf_ncs_matches` 테이블과 매핑 객체 저장.
3. `build-sqf-mappings` 하네스로 경영지원 MVP 자동 매칭 점수 산출.
4. MCP 도구에서 SQF 검색, 매핑 조회, 갭분석, 다음 NCS 능력단위 추천 제공.
5. 대시보드에서 매핑 후보 검토와 `review_status` 저장.
6. `recommend_education_for_duty`가 매핑 근거를 사용하도록 개선.
7. JSON-LD export와 ontology validate.
8. SKOS/OWL/SHACL export 및 검증 확장.

## 추천 KPI

성공 기준은 사용자가 원하는 업무를 물었을 때 다음을 반환하는 것이다.

- 관련 SQF 직무수준.
- 직접 조건: 교육훈련, 자격, 경력.
- 보완 조건: 관련 NCS 능력단위, 능력단위요소, 수행준거, KSA.
- 부족역량과 추천 학습 목표.
- 매핑 점수, 근거 문장, 원천.

공식 인정·평가는 별도 단계다. 이 프로젝트는 먼저 인정 가능성, 부족역량, 추천 근거를 설명하는 보조 엔진을 만든다.
