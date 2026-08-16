# Repository Guidelines

## 프로젝트 목표

이 저장소는 NCS 정보망 Excel DB, NCS 기준정보 API, NCS 훈련정보 API, NCS 경력개발경로 CSV, 능력단위별 자격 종목 API, 직업기초능력 API를 SQLite로 정규화하고 MCP 서버로 노출하는 프로젝트다. 최종 목표는 단순 검색기가 아니라 **NCS 기반 HR Ontology와 교육 추천 시스템**이다.

현재 활성 제품 범위는 NCS 중심이다. SQF와 NCS 학습모듈 흐름은 과거 호환/참조 코드와 데이터로만 취급하며, 활성 추천 경로의 기본 근거로 사용하지 않는다.

핵심 방향은 다음과 같다.

- 원천 NCS DB와 API 응답을 보존하면서 별도 온톨로지 테이블에 구조화한다.
- KSA를 원문 문자열 덩어리로만 쓰지 않고 원자 KSA 후보와 대표 개념으로 전처리한다.
- 수행준거를 과업 단위로 보고, 과업 수행에 필요한 지식/기술/태도 관계를 만든다.
- 과업 간 KSA 유사도와 전이 가능성을 계산해 업스킬링/리스킬링 추천 근거로 사용한다.
- 훈련정보 API의 능력단위명칭, 능력단위분류번호, 능력단위수준, 훈련목표, 훈련시간, 훈련시설, 훈련방법은 단순 속성이 아니라 NCS 과업/KSA와 연결된 추천 근거 관계로 승격한다.
- 추천 결과는 NCS 능력단위, 능력단위요소, 수행준거, KSA, 훈련과정, 경력개발경로, 자격, 직업기초능력 근거를 함께 반환한다.

## 현재 활성 데이터 축

- NCS DB: `classifications`, `competency_units`, `competency_elements`, `performance_criteria`, `ksa_items`
- KSA 온톨로지: `ksa_atomic_items`, `ontology_concepts`, `ontology_concept_aliases`, `ksa_concept_links`, `ksa_atomic_concept_links`, `criteria_concept_links`
- 과업/KSA 관계: `task_ksa_concept_relations`, `ontology_concept_relations`, `task_similarity_links`
- 훈련정보 API: `ncs_training_courses`, `ncs_training_course_unit_links`, `ncs_training_course_concept_links`, `ncs_training_course_element_links`, `training_goal_concept_links`, `training_delivery_relations`
- 경력개발경로 CSV: `ncs_career_paths`
- 능력단위별 자격 종목 API: `ncs_qualification_items`, `ncs_unit_qualification_links`
- 직업기초능력 API: `ncs_job_base_competencies`, `ncs_job_base_factors`, `ncs_unit_job_base_links`
- 질의/평가 보강: `ncs_query_aliases`, `training_transition_gold_scenarios`

## 전체 수집 범위 원칙

API 관련 작업은 smoke test, 디버깅, 단건 조회를 제외하면 특정 02 분야에 고정하지 않는다. 운영 전처리, 추천 근거 구축, 평가 데이터 생성은 항상 전체 NCS 범위를 대상으로 한다.

전체 수집 명령의 기준은 다음과 같다.

```powershell
python scripts\ncs_harness.py collect-training-courses --all-majors --num-of-rows 500
python scripts\ncs_harness.py collect-job-base --all-majors --num-of-rows 500
python scripts\ncs_harness.py collect-qualification-items --all-units
```

학습모듈 API를 레거시 참조 목적으로 다시 사용할 때도 단일 분야가 아니라 전체 대분류 조회를 기준으로 한다.

```powershell
python scripts\ncs_harness.py collect-study-modules --all-majors
```

코드에서는 `major_code="02"` 같은 기본값을 운영 수집 로직에 넣지 않는다. 02 분야는 쿼리 예시, smoke test, API 연결 확인 용도로만 허용한다. 전체 수집은 DB의 `available_major_codes` 또는 전체 `competency_units`를 순회해야 한다.

능력단위별 자격 API는 `ncs_qualification_collection_status`에 unit별 `collected`, `empty`, `error` 상태를 기록한다. 기본 수집은 완료/빈 데이터 unit을 건너뛰며 이어서 실행한다. 강제 재수집이 필요할 때만 `--refresh`를 사용한다. 이 API의 `numOfRows`는 최대 50으로 제한한다. 실패 unit 재시도는 `retry-qualification-errors`를 사용하며, 기본적으로 `next_retry_at`이 지난 항목만 조회한다.

## 처음 확인할 문서

처음 작업할 때는 아래 문서를 우선 확인한다.

- `ARCHITECTURE.md`: 데이터 소스, 스키마, 불변조건.
- `docs/HARNESS_ENGINEERING.md`: 실행 하네스와 검증 루프.
- `docs/NCS_MCP_PRD.md`: 제품 요구사항과 배경.
- `docs/NCS_SQF_PROJECT_SYSTEM.md`: PRD 기반 전체 프로젝트 체계와 MCP 발전 로드맵. SQF 내용은 현재 활성 범위와 분리해서 읽는다.
- `docs/NCS_SQF_ONTOLOGY.md`: 과거 NCS-SQF 온톨로지 설계. 현재는 NCS HR 온톨로지 원칙을 우선한다.
- `docs/NCS_SQF_HANDOFF.md`: SQLite DB, schema, data dictionary, sample query 전달 패키지.
- `reports/*.md`: 최근 전처리, 품질진단, API 보강 결과.

## 주요 디렉터리

- `src/ncs_mcp/`: 전처리, DB 스키마, API 수집, 품질진단, MCP 서버 코드.
- `tests/`: 단위 테스트.
- `scripts/`: 반복 실행하는 하네스.
- `data/raw/`: 원천 Excel/API/문서 자료. 대용량/민감 데이터는 커밋하지 않는다.
- `data/processed/`: 생성 SQLite DB.
- `reports/`: 생성 리포트.

## 핵심 명령

저장소 루트에서 실행한다.

```powershell
$env:PYTHONPATH="C:\workspace\NCS_MCP\src"
python scripts\ncs_harness.py inspect
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
python -m unittest discover -s tests -v
```

NCS 전체 온톨로지 전처리와 KSA/과업/훈련 관계 구축은 아래 순서로 실행한다.

```powershell
python scripts\ncs_harness.py preprocess-ncs-ontology --atomic-ksa
python scripts\ncs_harness.py preprocess-ncs-ontology --task-ksa-relations
python scripts\ncs_harness.py preprocess-ncs-ontology --task-similarity
python scripts\ncs_harness.py preprocess-ncs-ontology --training-course-links
```

스키마, 온톨로지, 추천 로직을 바꾼 뒤에는 아래도 확인한다.

```powershell
python scripts\ncs_harness.py ontology validate
python scripts\ncs_harness.py ontology export-jsonld --out exports\ncs_hr_ontology.jsonld
```

추천과 API 보강 확인에 자주 쓰는 명령은 다음과 같다.

```powershell
python scripts\ncs_harness.py recommend-training-transition --current-query "노무관리" --target-query "인사기획" --limit 5
python scripts\ncs_harness.py qualification-summary
python scripts\ncs_harness.py job-base-summary
```

## NCS KSA 온톨로지 원칙

이 프로젝트에서 KSA는 단순 문자열이 아니라 과업 수행 역량의 후보 노드다. 원천은 절대 덮어쓰지 않고 전처리 산출물은 별도 테이블에 저장한다.

불변조건:

- `ksa_items.ksa_text_raw`는 수정하지 않는다.
- KSA 원문을 자동으로 `ontology_concepts.definition`에 복사하지 않는다.
- 정의가 없으면 `definition_status='missing'`으로 둔다.
- 사람이 정의를 작성한 경우에만 `definition_status='defined'`, `review_status='human_reviewed'`로 표시한다.
- 동일 개념 통합은 원문 삭제가 아니라 대표 개념, 별칭, 링크 재연결로 처리한다.
- 개념 관계는 문자열 덮어쓰기가 아니라 구조화 테이블에 저장한다.

KSA 전처리 계층:

- `ksa_items`: Excel 원천 KSA. 변경 금지.
- `ksa_atomic_items`: KSA 원문을 줄바꿈, 불릿, 번호 등으로 분해한 원자 KSA 후보.
- `ontology_concepts`: 지식/기술/태도 대표 개념 노드.
- `ontology_concept_aliases`: 동일 개념 별칭.
- `ksa_concept_links`: 원천 KSA와 대표 개념의 링크.
- `ksa_atomic_concept_links`: 원자 KSA와 대표 개념의 링크.
- `criteria_concept_links`: 수행준거와 개념의 링크.

## 과업 수행 KSA 관계

수행준거는 과업을 판단하는 최소 실행 단위로 본다. 같은 수행준거를 수행하기 위해 함께 요구되는 KSA는 다음 관계로 저장한다.

- `knowledge_enables_skill`: 특정 지식이 과업 수행 기술을 가능하게 한다.
- `attitude_supports_skill`: 특정 태도가 과업 수행 기술을 뒷받침한다.
- `knowledge_informs_attitude`: 특정 지식이 과업 수행 태도 형성에 영향을 준다.
- `co_required_in_element`: 같은 능력단위요소 안에서 함께 요구된다.

관계 근거는 `task_ksa_concept_relations`에 수행준거, 능력단위요소, 원자 KSA, confidence, evidence를 함께 저장한다. 전체 그래프 조회를 위해 요약 관계는 `ontology_concept_relations`에도 반영한다.

## 훈련 추천 원칙

추천은 단순히 질의어와 훈련명 문자열을 맞추는 방식이 아니다. 질의는 NCS 대분류, 중분류, 소분류, 세분류, 능력단위, 능력단위요소, 수행준거를 위에서 아래로 확인해 범위를 잡고, 그 범위 안의 KSA와 과업 근거로 내려간다.

추천 결과는 아래 근거를 함께 사용한다.

- 현재 직무와 목표 직무의 공통 KSA: 전이 가능한 역량.
- 목표 직무에만 강한 KSA: 부족 역량.
- 훈련목표가 부족 KSA와 수행준거를 얼마나 직접 커버하는지.
- 능력단위요소 단위로 훈련과정이 무엇을 커버하는지.
- 훈련시간 대비 KSA/과업 커버 밀도.
- 훈련방법이 지식 중심, 기술 중심, 실습 중심 중 어디에 가까운지.
- 훈련시설이 실제 수행 환경과 맞는지.
- 능력단위수준과 경력개발경로 단계가 현재 수준 대비 적절한지.
- 관련 자격 종목이 직무 전환의 보조 근거가 되는지.
- 직업기초능력의 공통점과 부족점이 전환 난이도 설명에 기여하는지.

추천 점수는 관계 강도를 구분한다. `training_goal_concept_text` 직접 매칭을 가장 강하게 보고, `training_goal_concept_token` 토큰 매칭은 그보다 낮게 보며, `training_goal_element_implied_concept` 요소 기반 추론은 보조 근거로 본다. `unit_ksa_concept_inherited` 능력단위 상속 링크는 후보 확장용 보조 근거이며 단독으로 추천 상위에 올리지 않는다. 범용 KSA는 개념 특이도 가중치를 낮춰 여러 직무에 과잉 연결되지 않게 한다.

추천은 공식 자격 인정이나 법적 적격성 판단이 아니라 교육훈련 안내다. 자격/직무 인정 판단과 교육 추천 판단은 분리한다.

## 학습모듈과 SQF 처리 원칙

현재 활성 추천 경로에서는 SQF와 NCS 학습모듈을 기본 근거로 사용하지 않는다. 기존 테이블, 테스트, 문서는 과거 호환성과 참조 용도로 남아 있을 수 있다.

새 기능을 구현할 때는 기본적으로 다음 우선순위를 따른다.

1. NCS 원천 DB와 KSA/과업 온톨로지.
2. 훈련정보 API의 과정, 목표, 시간, 시설, 방법, 능력단위 링크.
3. 경력개발경로 CSV.
4. 능력단위별 자격 종목 API.
5. 직업기초능력 API.

SQF나 학습모듈을 다시 활성화하려면 사용자 요구가 명확해야 하며, 공식 인정/평가와 추천/갭분석을 분리해야 한다.

## 테스트 기준

변경 후 최소 아래를 실행한다.

```powershell
python -m unittest discover -s tests -v
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
```

온톨로지나 추천 근거 테이블을 바꾸면 추가로 아래를 확인한다.

```powershell
python scripts\ncs_harness.py ontology validate
```

## 보안

`.env`는 비공개다. `NCS_SERVICE_KEY`, `NCS_TRAINING_COURSE_SERVICE_KEY`, `NCS_QUALIFICATION_SERVICE_KEY`, `NCS_JOB_BASE_SERVICE_KEY`를 출력하거나 커밋하지 않는다. 생성 DB와 리포트는 재생성 가능한 산출물로 취급한다.

## 수작업 정제

사람이 직접 정제할 때는 대시보드를 사용한다.

```powershell
python scripts\ncs_dashboard.py --host 127.0.0.1 --port 8765
```

원문 필드는 수정하지 않는다. 사람이 보정한 값은 refined 계열 필드 또는 온톨로지 대표 개념/정의/관계 테이블에 저장하고 `review_status='human_reviewed'`로 표시한다.
