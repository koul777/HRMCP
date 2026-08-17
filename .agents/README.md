# NCS MCP Subagents

이 디렉터리는 `NCS_MCP` 프로젝트에서 반복적으로 사용할 서브에이전트 역할 정의를 보관한다.

## Agents

- `prompt-intake-agent.md`: 사용자의 자연어 요청을 실행 가능한 작업 브리프로 정리한다.
- `product-analyst-agent.md`: 보고서와 프로젝트 원칙을 기준으로 제품 방향, 수용 기준, 리뷰 기준을 정의한다.
- `aihr-demo-runner-agent.md`: 교육체계 계획 JSON과 HTML 데모를 생성하고 contract check를 수행한다.
- `education-recommendation-agent.md`: NCS 과업/KSA/훈련과정 근거 기반 교육 추천을 수행한다.
- `evaluation-agent.md`: 추천 품질, 테스트, 온톨로지 검증, 회귀 위험을 평가한다.
- `data-system-improvement-agent.md`: 데이터 정교화, 코드 구조, 수집/전처리 개선을 제안하고 구현한다.
- `data-collection-agent.md`: 자격, 직업기초능력, 훈련과정 API 보강 수집을 guarded command로 실행한다.
- `ontology-review-agent.md`: 온톨로지 정의와 alias 검토 seedpack을 준비한다.
- `task-ksa-review-agent.md`: 수행준거/과업/KSA 관계 검토 근거를 준비한다.
- `training-goal-review-agent.md`: 훈련목표-KSA 링크와 약한 과정 근거를 검토 가능하게 정리한다.
- `korean-law-mcp-benchmark.md`: Law MCP 구조 원칙을 NCS 도메인에 맞게 번역한다.

## Shared Guardrails

- 활성 제품 범위는 NCS 기반 HR Ontology와 교육 추천이다.
- 활성 추천 근거 경로는 NCS HR ontology, NCS training API, career path, qualification API, job-base competency API이다.
- SQF와 NCS 학습모듈은 레거시/참조 경로로만 사용하고 active recommendation 근거로 사용하지 않는다.
- `ksa_items.ksa_text_raw` 같은 원천 필드는 수정하지 않는다.
- 사람이 확정하지 않은 자동 정의는 `definition_status='missing'` 또는 review 대기 상태로 둔다.
- 자동 에이전트는 명시적 사람 결정 없이 `human_reviewed`, `accepted`, `reviewed` 상태를 쓰지 않는다.
- `.env`와 서비스 키는 출력하거나 커밋하지 않는다.
- 운영 수집/전처리는 특정 `major_code="02"`에 고정하지 않는다. 02는 smoke/debug/query 예시로만 사용한다.
- `[2026년도 인사담당자 NCS 활용 실무 가이드] 교육훈련체계 구축.pdf`는 source data가 아니라 workflow/rubric 기준이다. 샘플 과정, 호텔 예시, 조직명은 운영 원천 데이터로 넣지 않는다.
- 추천과 평가 기준은 `직무 -> Duty/책무 -> 과업 -> 수행준거 -> KSA -> 교육과정` 연결, 교육목표 직접성, 수준/시간/방법 적합성, 필수/선택 판단, 중복/범용성 경고를 포함한다.
- 보고서나 참고문서에서 추출한 내용은 자동으로 추천 점수를 올리는 직접 근거가 아니라 review 또는 framework evidence로만 다룬다.
- Law MCP급 구조를 목표로 한다. 공개 툴 표면은 작게 유지하고, 자연어 요청은 `src/ncs_mcp/query_router.py`의 시나리오 라우팅을 거쳐 적절한 facade/tool로 보낸다.
- `ncs_discover_tools`의 `query_route`는 자동화 에이전트의 1차 선택 근거다. 누락 파라미터, 권장 체인, 위험 플래그를 확인한 뒤 `ncs_execute_tool` 또는 직접 툴 호출을 선택한다.
- MCP 서버를 띄우지 않는 하네스/서브에이전트 작업은 먼저 `python scripts\ncs_harness.py route-ncs-query "<intent>"`를 실행해 동일한 시나리오, 툴, 파라미터, 위험 플래그를 확인한다.
- 툴 표면/라우터 변경 시 먼저 `python scripts\export_mcp_tool_contract.py --out reports\mcp_tool_contract_<DATE>.json`로 export하고 release-readiness `--contract`도 reports로 맞춘다.

## 2026 Guide Workflow

All product, demo, and evaluation agents should map work to the guide stages:

- `C1-1`: investigate internal/external courses and map course evidence to job, Duty/task, performance criteria, and KSA.
- `C1-2`: review training necessity, duplicate/broad-course risk, required/optional basis, and feasibility before proposing a confirmed course list.
- `C2-1`: turn recommendations into an education-system matrix by job scope, target level, education type, and delivery operation.
- `C2-2`: preserve annual-operation and management-plan fields such as period, target group, method/facility constraints, and human review state.

## AI-HR Prototype Surface Contract

Live, demo, and release-readiness outputs must expose the current AI-HR
training-system prototype contract:

- `recommended_path`
- `training_system_matrix`
- `task_ksa_basis`
- `facility_constraint_fit`
- `human_review`
- `query_route`
- `training_system_guide_trace`

`training_system_guide_trace.schema` must be
`aihr_training_system_guide_trace_v1`. Its checks must include `job_scope`,
`task_ksa`, `course_link`, `required_optional`, `level_delivery`, and
`human_review`. This trace is a validation/planning rubric, not source training
evidence.

AI-HR live planner outputs must expose `query_route` with schema
`ncs_query_route_v1`, `tool=plan_ncs_education_path`, `expected_tool_chain`,
`route_contract`, and `route_fingerprint`. Missing route evidence is a failed
live-plan/dashboard contract.

Dashboard verification outputs must expose `static_artifacts` for the public
demo JSON, public demo HTML, release-readiness JSON, queue-status JSON,
queue-run JSON, HRD guide prompt-coverage JSON, and AI-HR guide surface audit
JSON when present. Every listed artifact must exist and be non-empty.

Release-readiness reports must include both AI-HR demo proof artifacts and the
dashboard verification artifact. Omitted proof inputs are blockers, not
unchecked passes.

## Standard Handoff

각 서브에이전트는 결과를 다음 구조로 반환한다.

1. 목표와 입력
2. 수행한 작업 또는 권장 작업
3. 증거 파일/명령/쿼리
4. 발견 사항
5. 남은 위험과 다음 조치

## Recommended Automation Flow

1. `prompt-intake-agent`가 요청을 정규화하고 산출물, 범위, 검증 명령을 정한다.
2. `product-analyst-agent`가 보고서 기반 수용 기준과 사용자 가치 기준을 만든다.
3. `data-system-improvement-agent` 또는 `education-recommendation-agent`가 서로 겹치지 않는 파일 범위에서 구현하거나 분석한다.
4. `aihr-demo-runner-agent`가 대표 시나리오 JSON/HTML 데모를 재생성하고 contract check를 기록한다.
5. `evaluation-agent`가 테스트, 품질 지표, 추천 근거, 회귀 위험을 검토한다.
6. 부모 에이전트가 통합 diff, 검증 결과, 다음 작업 큐를 정리한다.

## Release Blocker Agent Queue

Release-readiness emits dated queue JSON/Markdown paths. Use one artifact date
stamp for a whole run. Prefer the latest readiness JSON `agent_work_queue_path`
when it is present, and derive `<DATE>` from that path or from the current
release-readiness output path. Do not mix dates in one queue/status/run cycle.

The standard queue artifact family is:

- `reports/aihr_agent_queue_<DATE>.json`
- `reports/aihr_agent_queue_<DATE>.md`
- `reports/aihr_agent_queue_status_<DATE>.json`
- `reports/aihr_agent_queue_status_<DATE>.md`
- `reports/aihr_agent_queue_run_dryrun_<DATE>.json`
- `reports/aihr_agent_queue_run_dryrun_<DATE>.md`
- `reports/aihr_agent_queue_run_<DATE>.json`
- `reports/aihr_agent_queue_run_<DATE>.md`

Legacy alias queue artifacts may still exist and can be consumed only when
referenced by readiness JSON:

- `reports/aihr_agent_work_queue_<DATE>.json`
- `reports/aihr_agent_work_queue_<DATE>.md`

Before launching queue work, generate the preflight status:

```powershell
python scripts\ncs_harness.py agent-queue-status --queue reports\aihr_agent_queue_<DATE>.json      --out reports\aihr_agent_queue_status_<DATE>.json --markdown-out reports\aihr_agent_queue_status_<DATE>.md
python scripts\ncs_harness.py agent-queue-run-ready --queue reports\aihr_agent_queue_<DATE>.json      --dry-run --out reports\aihr_agent_queue_run_dryrun_<DATE>.json --markdown-out reports\aihr_agent_queue_run_dryrun_<DATE>.md
```

When the dashboard is running, the status artifact is also exposed at
`/aihr-agent-queue-status` and `/api/aihr-agent-queue-status`. The latest
automatic execution artifact is exposed at `/aihr-agent-queue-run` and
`/api/aihr-agent-queue-run`.

Use `ready_to_start` items for automatic report regeneration. Treat
`manual_ready` items as operator-controlled work and fix any `blocked_*` item
before execution.
`agent-queue-run-ready` is limited to `can_start_automated=true` and
`regenerate_reports_only` items; it must not execute human-decision or guarded
API collection work. Run artifacts store bounded stdout/stderr tails plus
truncation metadata, not full command output.

For training-goal review work, keep the transition scenario seedpack in the
triage input path. The queue expects
`reports/aihr_transition_scenario_seedpack_<DATE>.jsonl` before
`review-triage` so scenario-review evidence stays visible beside goal-link
evidence.

Additional blocker-focused agent roles:

- `ontology-review-agent.md`: prepares ontology definition and alias review work.
- `training-goal-review-agent.md`: prepares training-goal concept link review work.
- `task-ksa-review-agent.md`: prepares task/KSA relation review work.
- `data-collection-agent.md`: runs guarded supplemental API collection retries.

Agents must treat these queue items as work briefs, not as permission to mark
human review decisions. Any status such as `human_reviewed`, `accepted`, or
`reviewed` requires an explicit human decision.
