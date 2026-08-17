# NCS 훈련 추천 MCP 사용자 가이드

이 MCP는 NCS 능력단위, 수행준거, KSA, 훈련과정, 경력개발경로, 자격,
직업기초능력을 근거로 경력개발과 직무 전환 교육을 추천하는 서버다.

## 먼저 알아야 할 범위

- 활성 범위는 NCS 중심이다.
- SQF와 NCS 학습모듈은 레거시/참조 데이터로 남아 있지만 기본 추천 도구 표면에는 노출하지 않는다.
- 추천 결과는 공식 자격 인정이나 법적 적격성 판단이 아니라 교육훈련 안내다.
- 원천 NCS 텍스트는 수정하지 않고, 전처리/온톨로지/리뷰 결과는 별도 테이블에 둔다.
- 현재 배포 상태가 `private/draft developer preview`이면 기능 검증용 공유
  단계이며, 안정 공개/내부 릴리스가 아니다. 사람 검토, 자격 API 커버리지,
  provenance 재확인이 남아 있으면 추천 결과를 승인된 HR 의사결정으로
  표현하지 않는다.

## 실행 방식

로컬 MCP 클라이언트용 STDIO:

```powershell
.\run_ncs_mcp_stdio.cmd
```

HTTP 실험/배포용:

```powershell
.\run_ncs_mcp_http.cmd
```

HTTP 기본 주소:

- MCP: `http://127.0.0.1:8766/mcp`
- health: `http://127.0.0.1:8766/health`
- ready: `http://127.0.0.1:8766/ready`

## 도구 찾기

어떤 도구를 써야 할지 모르면 먼저 `ncs_discover_tools`를 호출한다.

```json
{
  "tool": "ncs_discover_tools",
  "arguments": {
    "intent": "직무 전환 교육 추천"
  }
}
```

읽기 전용 사용자 도구는 `ncs_execute_tool`로 실행할 수 있다.

```json
{
  "tool": "ncs_execute_tool",
  "arguments": {
    "tool_name": "recommend_training_transition",
    "params": {
      "current_query": "노무관리",
      "target_query": "인사기획",
      "limit": 5
    }
  }
}
```

`ncs_execute_tool`로 추천 도구를 실행하면 기본적으로 다음 정책이 적용된다.

- `save=false` 강제: 탐색 호출이 추천 감사 row를 만들지 않는다.
- `compact=true` 기본: 큰 evidence 응답 대신 클라이언트용 요약을 우선 반환한다.
- `compact=false`를 명시하면 전체 응답을 요청할 수 있다.

## 활성 사용자 도구

| 도구 | 용도 |
| --- | --- |
| `ncs_discover_tools` | 의도에 맞는 MCP 도구를 찾는다. |
| `ncs_execute_tool` | 읽기 전용 사용자 도구를 이름과 파라미터로 실행한다. |
| `ncs_search` | NCS 분류, 능력단위, 요소, 수행준거, KSA를 검색한다. |
| `ncs_unit_detail` | 특정 능력단위의 요소, 수행준거, KSA, 훈련, 자격 근거를 조회한다. |
| `ncs_training` | 훈련과정을 검색하거나 특정 훈련과정을 조회한다. |
| `ncs_analysis` | 경력개발경로, 자격, 직업기초능력, 온톨로지 근거를 조회한다. |
| `recommend_training_for_task` | 현재 과업/능력단위 기준으로 필요한 훈련을 추천한다. |
| `recommend_training_transition` | 현재 직무에서 목표 직무로 옮기기 위한 훈련을 추천한다. |
| `plan_ncs_education_path` | 직무/과업 전환 추천 결과를 교육체계 수립용 단계형 계획으로 재구성한다. |
| `recommend_task_transitions` | KSA 유사도 기반으로 가까운 과업 전환 후보를 추천한다. |
| `get_concept_evidence` | 특정 온톨로지 개념의 KSA/수행준거/추천 근거를 조회한다. |

## 운영자 도구

아래 도구는 명시적인 검토/품질 작업에만 사용한다.

| 도구 | 용도 |
| --- | --- |
| `get_quality_issues` | 품질 이슈를 조회한다. |
| `review_training_goal_concept_link` | 추천 점수에 쓰이는 훈련목표-KSA 개념 링크를 검토한다. |
| `review_task_ksa_concept_relation` | 직무 전환 추론에 쓰이는 과업-KSA 관계를 검토한다. |
| `review_learning_module_ncs_link` | 레거시 학습모듈-NCS 링크를 검토한다. |
| `review_ontology_concept` | 온톨로지 개념을 사람 검토 상태로 갱신한다. |

운영자 도구는 기본 공개 MCP 표면에 노출되지 않는다. 서버 시작 전에
`NCS_MCP_ENABLE_OPERATOR_TOOLS=1`을 설정한 운영 세션에서만 직접 호출할 수
있으며, `ncs_execute_tool`로 실행되지 않는다.

## 추천 예시

직무 전환 추천:

```json
{
  "tool": "recommend_training_transition",
  "arguments": {
    "current_query": "노무관리",
    "target_query": "인사기획",
    "limit": 5,
    "compact": true,
    "save": false
  }
}
```

과업 기준 훈련 추천:

```json
{
  "tool": "recommend_training_for_task",
  "arguments": {
    "query": "인력채용",
    "limit": 5,
    "compact": true,
    "save": false
  }
}
```

교육체계 수립 초안:

```json
{
  "tool": "plan_ncs_education_path",
  "arguments": {
    "current_query": "노무관리",
    "target_query": "인사기획",
    "plan_objective": "인사기획 담당자 전환 교육체계",
    "target_population": "노무관리 경험이 있는 HR 담당자",
    "scenario": "직무전환",
    "preferred_max_hours": 24,
    "preferred_methods": ["집체훈련"],
    "limit": 5,
    "save": false
  }
}
```

`scenario`는 생략하면 자동 선택한다. 지원 시나리오는 `직무전환`,
`온보딩`, `업스킬링`, `리스킬링`, `자격연계`, `직업기초능력`, `운영`
관점이다. 시나리오는 추천 점수를 공식 판정으로 바꾸지 않고, 같은
전환 추천 근거를 교육기획 관점으로 재구성하는 보조 분석이다.

근거 조회:

```json
{
  "tool": "ncs_unit_detail",
  "arguments": {
    "unit_code": "0202020101_23v3",
    "include": ["elements", "criteria", "ksa", "training", "qualification"]
  }
}
```

## 오류 해석

오류는 `ok=false`와 함께 `error.code`, `error.category`, `error.retryable`을 반환한다.

- `not_found`: 찾을 수 없음. 임의로 만들어내지 않는다.
- `validation`: 입력이 부족하거나 잘못됨.
- `unsupported`: 지원하지 않는 모드/상태/타입.
- `policy`: meta 실행 정책으로 차단됨.
- `execution`: 도구 내부 실행 실패.
- `configuration`: 키나 설정이 부족함.
- `external_dependency`: 외부 API 실패. `retryable=true`이면 재시도 후보.

자세한 목록은 `docs\MCP_ERROR_CODES.md`를 본다.

## 배포 전 확인

배포나 외부 MCP 등록 전에는 다음 문서를 따른다.

```text
docs\MCP_RELEASE_CHECKLIST.md
```

도구 계약 JSON:

```text
mcp\ncs-tool-contract.json
```
