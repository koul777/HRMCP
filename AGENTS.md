# Repository Guidelines

## AI-HR Agent Work Queue

Release-readiness automation may generate the current queue artifacts:

- `reports/aihr_agent_queue_20260617.json`
- `reports/aihr_agent_queue_20260617.md`
- `reports/aihr_agent_queue_status_20260617.json`
- `reports/aihr_agent_queue_status_20260617.md`
- `reports/aihr_agent_queue_run_dryrun_20260617.json`
- `reports/aihr_agent_queue_run_dryrun_20260617.md`
- `reports/aihr_agent_queue_run_20260617.json`
- `reports/aihr_agent_queue_run_20260617.md`

Older runs may still contain `reports/aihr_agent_work_queue_20260617.*`.
When both names exist, use the path recorded in the latest release-readiness
JSON `agent_work_queue_path`; the current standard path is
`reports/aihr_agent_queue_20260617.json`.

Automation agents must treat this queue as the current blocker-driven work
brief. Each item names the owner, agent role file, command, expected artifacts,
mutation policy, and acceptance checks. The queue does not grant permission to
set `human_reviewed`, `accepted`, or `reviewed`; those statuses require an
explicit human decision.

Before launching automated queue work, run:

```powershell
python scripts\ncs_harness.py agent-queue-status --queue reports\aihr_agent_queue_20260617.json --out reports\aihr_agent_queue_status_20260617.json --markdown-out reports\aihr_agent_queue_status_20260617.md
python scripts\ncs_harness.py agent-queue-run-ready --queue reports\aihr_agent_queue_20260617.json --dry-run --out reports\aihr_agent_queue_run_dryrun_20260617.json --markdown-out reports\aihr_agent_queue_run_dryrun_20260617.md
```

After starting the dashboard, inspect the same preflight artifact at
`/aihr-agent-queue-status` or fetch it as JSON from
`/api/aihr-agent-queue-status`. The latest automatic execution evidence is
available at `/aihr-agent-queue-run` and `/api/aihr-agent-queue-run`.

Only `ready_to_start` items are safe for automatic report regeneration. Items
with `manual_ready` require an operator decision or guarded API timing, and
`blocked_*` items must be fixed before execution.
`agent-queue-run-ready` is limited to items marked `can_start_automated=true`
with `mutation_policy=regenerate_reports_only`; it must not run human-decision
items or guarded API collection items. Run artifacts store bounded stdout/stderr
tails plus truncation metadata, not full command output.

AI-HR education-plan outputs must expose the 2026 guide rubric as
`training_system_guide_trace` with schema
`aihr_training_system_guide_trace_v1`. The trace is a validation/planning
rubric, not source training data, and must include the checks `job_scope`,
`task_ksa`, `course_link`, `required_optional`, `level_delivery`, and
`human_review`.

Planner outputs must also expose `recommended_path`. The path separates
`scope_confirmation`, `core_gap_training`, `supporting_or_adjacent_training`,
and, when facility or delivery preferences are provided, delivery-stage
constraints. Each `training_system_matrix` row must carry `job_scope`,
`target_level_band`, `education_type`, `required_optional_basis`,
`delivery_operation`, `planner_grouping`, `task_ksa_basis`,
`facility_constraint_fit`, `human_review`, and `course_fit.level`,
`course_fit.hours`, `course_fit.methods`, `course_fit.facilities`.
Unknown or `not_requested` facility evidence is a review state; it is not a
hard failure unless the requested facility conflicts with the course evidence.

AI-HR live planner outputs must also expose `query_route` with schema
`ncs_query_route_v1`, `tool=plan_ncs_education_path`, `expected_tool_chain`,
`route_contract`, and `route_fingerprint`. Dashboard verification treats a
missing route contract as a failed live-plan surface, even if the matrix rows
render successfully.

AI-HR dashboard verification outputs must include `static_artifacts` for the
public demo JSON, public demo HTML, release-readiness JSON, queue-status JSON,
queue-run JSON, HRD guide prompt-coverage JSON, AI-HR guide surface audit JSON,
ontology-transferability education-system audit JSON, API linkage summary JSON,
qualification retry hygiene JSON, and qualification collection coverage-plan
JSON. Every listed artifact must exist and be non-empty before
the dashboard surface contract is considered valid. Live summaries must report
`missing_matrix_fields`, `missing_plan_fields`, `missing_guide_trace_fields`,
and `missing_query_route_fields`.

Release-readiness reports must be generated with both AI-HR demo artifacts and
dashboard verification artifacts. Missing proof artifacts are blockers, not
unchecked passes.

Ontology-transferability batch outputs may also generate
`reports/ontology_transferability_education_system_audit_*.json` or dated
AI-HR sample variants. This audit is report-only: it summarizes C1-1/C1-2/C2-1/C2-2
education-system readiness, course-link coverage, required/optional grouping,
delivery evidence, and human-review gates across major artifacts, but it must
not be used as a human approval signal or a DB write instruction. For this
audit, `contract_ok=true` only means the draft artifact shape is valid;
`approval_ready=false`, `status=review_required`, and top-level `ok=false` are
the expected state while rows remain human-gated.
Course-link gap diagnostics must not promote cross-major course-name similarity
as direct evidence. Same-name or similar-name courses outside the target NCS
major are `cross_scope_name_only` references and require external human review
before any link or education-system evidence claim.

Queue agents must read this file, `ARCHITECTURE.md`,
`docs/HARNESS_ENGINEERING.md`, `docs/NCS_MCP_PRD.md`, `.agents/README.md`, and
their assigned `.agents/*.md` role file before acting.

The converted 2026 NCS HRD guide Markdown is kept inside the project as a
framework reference. Before AI-HR education-plan, recommendation-card, router,
demo, dashboard, or release-readiness work, agents must inspect
`docs/NCS_HRD_GUIDE_REFERENCE.md` and the generated index
`docs/reference/ncs_hrd_guide_reference.index.json`. Rebuild the local reference
with:

```powershell
python scripts\ncs_harness.py preprocess-hrd-guide-reference
```

After router, prompt-template, or compact-response changes, regenerate the prompt
coverage report:

```powershell
python scripts\ncs_harness.py hrd-guide-prompt-coverage --out reports\hrd_guide_prompt_coverage_20260618.json --markdown-out reports\hrd_guide_prompt_coverage_20260618.md
```

For the first import from the user download folder, use:

```powershell
python scripts\ncs_harness.py preprocess-hrd-guide-reference --source <path-to-ncs_hrd_guide_codex_readable.md>
```

Treat the guide as `framework_reference` only. It can define planning stages,
guide-trace checks, prompt coverage, and development rules, but it must not
become scored source training data or a reason to write `human_reviewed`,
`accepted`, or `reviewed` automatically.

## KSA 정의 검토 운영 절차 (2026-06-26 기준)

현재 `ontology_concepts.definition`의 대량 정의는 승격 가능한 실질 정의가
아니라 타입별 boilerplate 성격이 강하다. `report-ksa-definition-promotion`
기준으로 `term_definition_candidate` 후보를 점검했을 때, 현재 실 DB에서는
승격 가능한 후보가 없고 boilerplate로 건너뛴 후보가 대부분이다. 자동화는 이
상태를 `defined`나 `human_reviewed`로 간주하면 안 된다.

KSA 정의 검토는 운영자 패킷으로 최소 범위만 본다.

```powershell
python scripts\ncs_harness.py ksa-definition-review-operator-packet --limit 25 --evidence-limit 2 --out reports\ksa_definition_review_operator_packet_20260626.json --markdown-out reports\ksa_definition_review_operator_packet_20260626.md
```

이 명령은 읽기 전용으로 아래 sidecar를 생성한다.

- `*_promotion_status.json/.md`: boilerplate 정의 승격 가능성 점검.
- `*_priority_report.json`: 추천 관계 등장 빈도 기준 검토 우선순위.
- `*_priority_review_pack.json/.md/.csv`: 운영자용 정의 검토 큐와 CSV 결정지.
- `*_decision_audit.json/.md`: 빈 CSV 또는 사람이 채운 CSV의 결정 감사.
- `*_action_plan.json/.md`: 사람이 승인한 행만 읽기 전용 action plan으로 정리.

정의 검토 CSV의 `decision`, `approved_definition`, `reviewer_id`,
`reviewed_at`, `rationale`는 처음에는 반드시 비어 있어야 한다. 자동화가
채우지 않는다. `draft_definition`은 검토 보조 문장일 뿐이며, 사람 결정이나 DB
쓰기 지시가 아니다.

사람이 CSV를 채운 뒤에도 바로 DB에 쓰지 않는다. 먼저 아래 두 단계를 거친다.

```powershell
python scripts\ncs_harness.py audit-ksa-definition-review-decisions --csv <filled.csv> --source-packet <packet.json> --source-review-pack <review_pack.json> --out <audit.json> --markdown-out <audit.md>
python scripts\ncs_harness.py plan-ksa-definition-review-actions --csv <filled.csv> --source-packet <packet.json> --source-review-pack <review_pack.json> --out <action_plan.json> --markdown-out <action_plan.md>
```

감사와 action plan도 `status_update_allowed=false`, `db_writes=false`,
`approval_claim=false`를 유지한다. action plan 안에 `review_status:
human_reviewed`가 보이더라도 이는 사람이 승인한 행에 대한 준비 목록일 뿐,
자동 DB 쓰기가 아니다. 별도 guarded apply 절차와 명시적 운영자 승인이 없으면
`ontology_concepts`를 갱신하지 않는다.

불변 원칙:

- `ksa_items.ksa_text_raw`는 어떤 경우에도 수정하지 않는다.
- `human_reviewed`, `accepted`, `reviewed`는 사람 결정 없이 코드가 쓰지 않는다.
- boilerplate 또는 draft definition은 자동으로 `ontology_concepts.definition`에
  승격하지 않는다.
- AGENTS/리포트/패킷은 운영자 판단을 줄이기 위한 증거 묶음이지 승인 기록이
  아니다.

## 현재 알려진 데이터 품질 문제 (2026-06-25 기준)

### definition boilerplate 문제 (최우선)
ontology_concepts.definition 413,143건 전부가 아래 boilerplate 패턴이다.
패턴: "{concept_name}: {타입별 고정 문장}"
- knowledge → "업무 판단과 문제 해결에 필요한 관련 원리, 기준, 절차, 사례에 대한 지식."
- skill     → "업무 상황에서 관련 절차나 도구를 활용해 과업을 수행하는 능력."
- attitude  → "업무 수행 과정에서 품질, 협업, 책임성을 유지하기 위한 태도."

ksa_meaning_candidates에 term_definition_candidate 413K건(llm_reviewed)이 있지만
ontology_concepts.definition으로 승격하는 로직이 없다.
120,766건은 definition_status='missing'으로 빈 칸이다.

### 추천 검증 미완
- training_transition_gold_scenarios: 100건
- training_transition_scenario_reviews: 11건 (11%만 검증됨)

### 수집 미완 API
- ncs_job_base_competencies: 10건 (직업기초능력 사실상 빈 칸)
- ncs_qualification_items: 795건 (부분 수집)

### 불변 원칙 (절대 위반 금지)
- ksa_items.ksa_text_raw는 어떤 경우에도 수정하지 않는다.
- human_reviewed / accepted / reviewed 상태는 사람 결정 없이 코드로 쓰지 않는다.

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

## 2026 교육훈련체계 구축 보고서 반영 방향

사용자가 제공한 `[2026년도 인사담당자 NCS 활용 실무 가이드] 교육훈련체계 구축.pdf`는 이 프로젝트의 공식 원천 데이터가 아니라 **제품 설계와 검증 기준을 잡는 핵심 방법론 자료**로 사용한다. PDF의 호텔 예시, 샘플 과정명, 샘플 조직명은 DB 원천이나 추천 근거로 승격하지 않는다.

보고서에서 활성 제품에 반영할 핵심 원칙은 다음과 같다.

- 교육 추천은 교육명 문자열 매칭이 아니라 `직무 -> Duty/책무 -> 과업(Task) -> 수행준거 -> KSA -> 교육과정` 매핑이어야 한다.
- 교육과정은 교육명보다 목적, 내용, 대상, 운영방식, 난이도, 수준, 시간, 법정/필수 여부, 성과 기여도를 기준으로 판단한다.
- 한 교육과정은 여러 직무와 연결될 수 있으므로 N:N 매핑을 허용하되, 너무 많은 직무에 붙는 범용 과정은 추천 상위 근거로 과대평가하지 않는다.
- 교육 필요성은 직무 연계성, 직급/수준 적합성, 필수/선택 구분, 중복 여부, 실행 가능성, 성과 연계성을 함께 본다.
- 교육체계도 산출물은 추천 결과를 직무, 직급/수준, 교육유형, 교육과정, 필수/선택, 운영방식으로 재구성할 수 있어야 한다.
- 연간 교육운영계획과 개인 교육이력은 추천 시스템의 후속 제품화 축이다. 현재는 추천 카드와 감사근거에 필요한 필드와 평가 지표를 먼저 확보한다.

보고서 기반으로 다음 작업의 제품 방향을 잡는다.

1. 추천 카드 강화: 각 추천에 `직무/범위`, `과업`, `KSA`, `교육목표 직접성`, `훈련시간`, `훈련방법`, `훈련시설`, `능력단위수준`, `필수/선택 판단 근거`, `중복/범용성 경고`를 드러낸다.
2. 교육체계 설계 지원: 단건 추천을 넘어 직무 또는 전환 목표별 `필수 과정`, `선택 과정`, `보완 과정`, `인접 참고 과정`으로 묶어 교육체계도 초안을 만들 수 있게 한다.
3. 평가 기준 강화: 추천 품질은 단순 recall만 보지 않고 직접 근거 비율, KSA gap coverage, 수준 적합성, 시간 대비 커버 밀도, 범용 KSA 과잉 연결, 중복 과정, 약한 근거의 상위 노출을 함께 본다.
4. 리뷰 큐 강화: 사람이 검토할 seedpack에는 교육과정명만 넣지 말고 어떤 과업/KSA/수준/운영방식 때문에 필요한지 판단할 수 있는 작은 근거 샘플을 포함한다.
5. 참고문서 처리: 이 보고서를 `ncs_reference_documents` 계열로 넣는 경우에도 `framework_reference` 성격의 검증 기준으로만 쓰고, 자동 추천 점수를 직접 올리는 원천 근거로 사용하지 않는다.

보고서의 C1/C2 절차는 제품 계약으로 다음처럼 번역한다.

- C1-1 교육과정 조사: 내부/외부 교육과정의 목적, 대상, 내용, 시간, 유형, 운영방식, 평가 정보를 수집하고, 과정명만으로 매핑하지 않는다.
- C1-1 직무기반 매핑: 교육과정을 직무, Duty, 과업, 수행준거, KSA, 수준에 N:N으로 연결하고, 광범위 과정은 범용성 경고와 낮은 특이도 가중치를 둔다.
- C1-2 필요성 검토: 직무 연계성, 직급/수준 적합성, 필수/선택/법정 여부, 중복, 실행 가능성, 성과 기여도를 근거로 교육과정 리스트를 확정한다.
- C2-1 교육훈련체계도: 추천 결과를 직무 범위, 목표 수준, 교육유형, 필수/선택, 운영방식, 과정 묶음으로 재배열하는 `training_system_matrix`로 제공한다.
- C2-2 운영계획: 연간 운영계획과 관리체계는 현재 추천 산출의 후속 축이며, 지금은 월/주기, 대상 부서, 시설/방법 제약, 사람 검토 상태를 보존할 수 있는 필드를 우선 확보한다.

## Law MCP급 구조 목표

`reports/reference/korean-law-mcp-main`은 이 프로젝트의 MCP 성숙도 기준이다. 코드를 그대로 복사하지 않고 구조 원칙만 NCS 도메인에 맞게 번역한다.

필수 구조 원칙:

- 공개 툴 표면은 작고 안정적으로 유지한다. 내부 수집, 리뷰, 레거시 SQF/학습모듈 도구는 기본 공개 표면에 올리지 않는다.
- `tool_registry.py`는 툴 설명, 카테고리, 별칭, 공개/운영/레거시 경계, 메타 실행 정책을 책임진다.
- `query_router.py`는 자연어 질의를 `education_system_design`, `training_transition`, `task_training`, `task_transition`, `evidence_analysis`, `operator_review`, `structure_search` 같은 시나리오로 해석하고 권장 툴, 파라미터, 누락 파라미터, 체인, 위험 플래그를 반환한다.
- `ncs_discover_tools`는 단순 카탈로그가 아니라 `query_route`를 함께 반환해야 한다. 에이전트는 먼저 이 라우팅 결과를 보고 도구를 선택한다.
- 하네스/서브에이전트 작업은 실행 전에 `python scripts\ncs_harness.py route-ncs-query "<intent>"`로 같은 라우팅 계약을 확인할 수 있다. 운영자 리뷰 도구를 포함해야 할 때만 `--include-operator-tools`를 붙인다.
- `ncs_execute_tool`은 읽기 전용 사용자 툴만 실행한다. 추천 계열은 메타 실행에서 `save=false`를 강제하고, 필요한 경우 `_route_query`로 라우팅 메타를 감사 기록에 붙인다.
- 고수준 facade는 실제 사용자 워크플로를 기준으로 한다. 현재 핵심 facade는 `plan_ncs_education_path`, `recommend_training_transition`, `recommend_training_for_task`이다.
- 리스크 규칙은 추천 결과와 공개 산출물에 모두 적용한다. 공식 승인, 자격 인정, 법적 적격성 판단처럼 오해될 수 있는 표현은 시스템/문서/데모에서 차단하거나 명확히 경고한다.
- MCP 계약은 `scripts/export_mcp_tool_contract.py`로 검증한다. 릴리스 리포트용 dated artifact는 먼저 `reports/mcp_tool_contract_20260617.json`으로 export하고, 레거시 고정 계약 파일을 갱신해야 할 때만 `mcp/ncs-tool-contract.json`을 함께 확인한다. 툴 표면이나 라우터 시나리오를 바꾸면 계약 파일과 테스트를 함께 갱신한다.
- Law MCP의 검색-상세 체인 패턴은 NCS에서는 `질의 -> 범위 해석 -> KSA/과업 근거 -> 훈련과정 -> 교육체계 행렬 -> 리뷰/준비도` 체인으로 번역한다.

## 전체 수집 범위 원칙

API 관련 작업은 smoke test, 디버깅, 단건 조회를 제외하면 특정 02 분야에 고정하지 않는다. 운영 전처리, 추천 근거 구축, 평가 데이터 생성은 항상 전체 NCS 범위를 대상으로 한다.

전체 수집 명령의 기준은 다음과 같다.

```powershell
python scripts\ncs_harness.py collect-training-courses --all-majors --num-of-rows 500
python scripts\ncs_harness.py collect-job-base --all-majors --num-of-rows 500
python scripts\ncs_harness.py qualification-retry-hygiene --ncs006-checkpoint-path reports\checkpoint_ncs006_element_api_status_<DATE>_current.json --out reports\qualification_retry_hygiene_<DATE>.json --markdown-out reports\qualification_retry_hygiene_<DATE>.md
python scripts\ncs_harness.py qualification-coverage-plan --target-ratio 0.9 --batch-size 100 --ncs006-checkpoint-path reports\checkpoint_ncs006_element_api_status_<DATE>_current.json --out reports\qualification_collection_coverage_plan_<DATE>.json --markdown-out reports\qualification_collection_coverage_plan_<DATE>.md --csv-out reports\qualification_collection_coverage_plan_<DATE>.csv
python scripts\ncs_harness.py agent-queue-status --queue reports\aihr_agent_queue_<DATE>.json --out reports\aihr_agent_queue_status_<DATE>.json --markdown-out reports\aihr_agent_queue_status_<DATE>.md
python scripts\ncs_harness.py collect-qualification-items --all-units --limit-units 100 --num-of-rows 50 --max-pages 1 --request-delay 2 --max-retries 1 --retry-backoff-seconds 30 --stop-after-rate-limit-errors 3 --ncs006-checkpoint-path reports\checkpoint_ncs006_element_api_status_<DATE>_current.json
```

학습모듈 API를 레거시 참조 목적으로 다시 사용할 때도 단일 분야가 아니라 전체 대분류 조회를 기준으로 한다.

```powershell
python scripts\ncs_harness.py collect-study-modules --all-majors
```

코드에서는 `major_code="02"` 같은 기본값을 운영 수집 로직에 넣지 않는다. 02 분야는 쿼리 예시, smoke test, API 연결 확인 용도로만 허용한다. 전체 수집은 DB의 `available_major_codes` 또는 전체 `competency_units`를 순회해야 한다.

능력단위별 자격 API는 `ncs_qualification_collection_status`에 unit별 `collected`, `empty`, `error` 상태를 기록한다. `qualification-coverage-plan`은 목표 커버리지까지 필요한 guarded batch 계획만 생성하며 API를 호출하지 않는다. 실제 `collect-qualification-items` 실행은 `qualification-retry-hygiene`과 `agent-queue-status`에서 safety violation이 없고 guarded 항목이 operator-ready일 때만 작은 batch로 진행한다. 기본 수집은 완료/빈 데이터 unit을 건너뛰며 이어서 실행한다. 강제 재수집이 필요할 때만 `--refresh`를 사용한다. 이 API의 `numOfRows`는 최대 50으로 제한한다. 실패 unit 재시도는 `retry-qualification-errors`를 사용하며, 기본적으로 `next_retry_at`이 지난 항목만 조회한다.

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

이 저장소는 `data/processed/ncs.db` 같은 대용량 LFS 파일을 포함한다.
작업 중 일반 `git status`가 LFS clean/filter 임시 파일을 수십 GB 이상
만들 수 있으므로, 상태 확인은 아래처럼 대용량 LFS 경로를 제외해서 실행한다.

```powershell
git -c filter.lfs.clean=cat -c filter.lfs.smudge=cat -c filter.lfs.process= -c filter.lfs.required=false status --short --untracked-files=no -- . ":(exclude)data/processed/*.db" ":(exclude)data/processed/*.db-*" ":(exclude)data/raw/*.xlsx" ":(exclude)data/raw/*.xls" ":(exclude)data/ocr/tessdata/*.traineddata"
```

작업 전후 용량 점검은 아래 dry-run 명령을 먼저 사용하고, 재생성 가능한
LFS 캐시와 파이썬 캐시를 실제 삭제할 때만 `--apply`를 붙인다.

```powershell
python scripts\ncs_harness.py workspace-hygiene
python scripts\ncs_harness.py workspace-hygiene --apply
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

## 자동화 서브에이전트 운영 원칙

이후 자동화된 서브에이전트 작업은 보고서의 교육훈련체계 구축 흐름을 기준으로 분리한다. 부모 에이전트는 통합 책임자이며, 서브에이전트는 서로 겹치지 않는 파일과 산출물을 맡는다.

공통 규칙:

- 모든 서브에이전트는 이 파일, `ARCHITECTURE.md`, `docs/HARNESS_ENGINEERING.md`, `docs/NCS_MCP_PRD.md`, `.agents/README.md`를 먼저 읽는다.
- 작업 전 LFS clean/smudge/process 필터를 끄고 대용량 LFS 경로를 제외한 `git -c filter.lfs.clean=cat -c filter.lfs.smudge=cat -c filter.lfs.process= -c filter.lfs.required=false status --short --untracked-files=no -- . ":(exclude)data/processed/*.db" ":(exclude)data/processed/*.db-*" ":(exclude)data/raw/*.xlsx" ":(exclude)data/raw/*.xls" ":(exclude)data/ocr/tessdata/*.traineddata"` 또는 더 좁은 상태 확인으로 변경 파일을 파악하고, 다른 사람이 만든 변경을 되돌리지 않는다.
- 각 작업은 명령 출력, DB 카운트, 테스트 결과, 리포트, 코드 diff 중 하나 이상의 증거를 남긴다.
- 둘 이상의 서브에이전트가 같은 파일을 동시에 수정하지 않는다. 같은 주제라도 분석 에이전트와 구현 에이전트의 write set을 분리한다.
- 자동화 에이전트는 `human_reviewed`, `accepted`, `reviewed` 상태를 임의로 부여하지 않는다. 사람 검토가 없으면 후보 또는 자동 링크 상태로 둔다.
- broad collection, 전체 전처리, DB 대량 갱신은 전체 NCS 범위 원칙을 지키고, API rate limit과 retry guard를 적용한다.
- 보고서 샘플 문장과 예시 표는 정책/검증 기준으로 요약할 수 있지만, 원문 대량 복사나 샘플 데이터를 운영 DB 원천으로 주입하지 않는다.

권장 서브에이전트 트랙:

- Prompt intake: 사용자 요청을 `수집`, `전처리`, `추천`, `평가`, `문서화`, `운영자동화`로 나누고, 산출물과 검증 명령을 명시한다.
- Product analyst: 보고서 기준으로 추천 수용 기준, 교육체계도 필드, 필수/선택 판단 기준, 리뷰 seedpack 요구사항을 정의한다.
- Code mapper: DB 스키마, 추천 점수, MCP 도구, 하네스 명령, 테스트 진입점을 매핑하고 구현 파일을 제안한다.
- Data/system worker: 원천 보존을 지키면서 데이터 수집, 링크 생성, 품질 리포트, 인덱스, 하네스 개선을 한 조각씩 구현한다.
- Education recommendation worker: 실제 질의와 전환 시나리오에서 추천 카드, score component, evidence highlight, training sequence를 점검한다.
- AI-HR demo runner: 대표 전환 시나리오와 alias-heavy 시나리오를 JSON/HTML 데모로 생성하고 `recommended_path`, `training_system_summary`, `training_system_matrix`, `task_ksa_basis`, `facility_constraint_fit`, `human_review`, 시설/시간/방법/수준, SQF/학습모듈 미사용, `source_payload` 비노출을 검증한다.
- Evaluation/reviewer: 회귀, 과잉 추천, 약한 근거 상위 노출, SQF/학습모듈 의존, secret 노출, 테스트 누락을 검토한다.

자동화 우선순위는 다음 순서로 둔다.

1. 활성 NCS 추천 경로가 SQF/학습모듈 없이 독립적으로 동작하는지 확인한다.
2. 훈련과정 링크 커버리지와 `training_goal_concept_links`, `ncs_training_course_element_links`, `training_delivery_relations` 품질을 올린다.
3. 보고서 기준의 추천 카드와 교육체계도 초안 필드를 compact response와 리포트에 반영한다.
4. `plan-ncs-education-path`와 `render-aihr-plan-demo`로 사람이 바로 볼 수 있는 데모 JSON/HTML을 최소 2개 이상 유지한다.
5. transition gold/candidate scenario를 늘리고, 직접 근거 없는 상위 추천을 줄인다.
6. review seedpack과 triage 리포트를 사람 검토에 바로 쓸 수 있는 형태로 정리한다.

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
