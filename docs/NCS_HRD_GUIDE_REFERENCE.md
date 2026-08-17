# NCS HRD Guide Reference

이 프로젝트는 `ncs_hrd_guide_codex_readable.md`를 NCS 교육훈련체계 개발 기준 문서로 보관한다. 이 문서는 원천 NCS 데이터가 아니라 제품 설계, 응답 계약, 검증 루브릭이다.

## 고정 경로

- 원본 복사본: `docs/reference/ncs_hrd_guide_codex_readable.md`
- 전처리 인덱스: `docs/reference/ncs_hrd_guide_reference.index.json`
- 사람이 읽는 요약: `docs/reference/ncs_hrd_guide_reference.md`
- 검색/참조용 청크: `docs/reference/ncs_hrd_guide_reference.chunks.jsonl`

## 갱신 명령

처음 가져올 때:

```powershell
python scripts\ncs_harness.py preprocess-hrd-guide-reference --source <path-to-ncs_hrd_guide_codex_readable.md>
```

프로젝트 내부 복사본으로 다시 전처리할 때:

```powershell
python scripts\ncs_harness.py preprocess-hrd-guide-reference
```

Guide prompt coverage:

```powershell
python scripts\ncs_harness.py hrd-guide-prompt-coverage --out reports\hrd_guide_prompt_coverage_20260618.json --markdown-out reports\hrd_guide_prompt_coverage_20260618.md
```

## 사용 원칙

- `framework_reference`로만 사용한다.
- 가이드의 호텔 예시, 샘플 과정명, 샘플 조직명은 DB 원천이나 추천 점수 근거로 승격하지 않는다.
- 교육계획 응답은 `직무/범위 -> 과업/수행준거 -> KSA -> 교육과정 -> 교육훈련체계도 -> 리뷰/운영계획` 흐름을 따라야 한다.
- `training_system_guide_trace`는 `job_scope`, `task_ksa`, `course_link`, `required_optional`, `level_delivery`, `human_review`를 포함해야 한다.
- 자동화는 검토 필요 상태를 만들 수 있지만 `human_reviewed`, `accepted`, `reviewed`는 명시적 사람 결정 없이는 쓰지 않는다.

## 개발 체크

AI-HR 교육계획, 추천 카드, 라우터, 데모, 대시보드 작업을 시작하기 전에는 전처리 인덱스의 `guide_workflow`, `guide_trace_contract`, `prompt_scenario_templates`, `development_rules`를 확인한다.

특히 가이드 프롬프트 예시는 다음 MCP 표면으로 답할 수 있어야 한다.

| 프롬프트 유형 | 기본 도구 | 필수 표면 |
| --- | --- | --- |
| 직무 전환 교육훈련체계 수립 | `plan_ncs_education_path` | `query_route`, `recommended_path`, `training_system_matrix`, `training_system_guide_trace` |
| 과업 기준 교육과정 추천 | `recommend_training_for_task` | 과업/KSA 근거, 교육목표 직접성, 시간/방법/시설/수준 |
| 현재-목표 직무 KSA gap 분석 | `recommend_training_transition` | 공통 KSA, 부족 KSA, 보완/인접 과정 구분 |
| 연간 운영계획 초안 | `plan_ncs_education_path` | 운영방식, 시설 제약, 대상, 사람 검토 상태 |

## 수용 기준

Blocker:

- 가이드 프롬프트 템플릿의 필수 응답 필드가 빠진 경우.
- `training_system_guide_trace`가 없거나 schema가 다르거나 6개 체크 중 하나가 빠진 경우.
- `recommended_path` 또는 `training_system_matrix`의 필수 planner 필드가 빠진 경우.
- live planner 출력에서 `query_route`, `route_contract`, `expected_tool_chain`, `route_fingerprint`가 빠진 경우.
- 가이드 예시를 원천 훈련 데이터, 추천 점수 가중 근거, 공식 승인 근거로 사용한 경우.
- 자동화가 명시적 사람 결정 없이 `human_reviewed`, `accepted`, `reviewed`를 기록한 경우.

Warning:

- `facility_constraint_fit`이 `unknown` 또는 `not_requested`이고 직접 충돌은 없는 경우.
- 범용/중복 과정 경고가 있는 경우.
- 과정명 중심 근거가 많지만 과업/KSA 및 과정 연결 근거가 완전히 빠지지는 않은 경우.
