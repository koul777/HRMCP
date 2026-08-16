# NCS MCP Subagents

이 디렉터리는 `NCS_MCP` 프로젝트에서 반복적으로 사용할 서브에이전트 역할 정의를 보관한다.

## Agents

- `prompt-intake-agent.md`: 사용자의 자연어 요청을 실행 가능한 작업 브리프로 정리한다.
- `education-recommendation-agent.md`: NCS 과업/KSA/훈련과정 근거 기반 교육 추천을 수행한다.
- `evaluation-agent.md`: 추천 품질, 테스트, 온톨로지 검증, 회귀 위험을 평가한다.
- `data-system-improvement-agent.md`: 데이터 정교화, 코드 구조, 수집/전처리 개선을 제안하고 구현한다.

## Shared Guardrails

- 활성 제품 범위는 NCS 기반 HR Ontology와 교육 추천이다.
- SQF와 NCS 학습모듈은 레거시 참조로만 사용하고 기본 추천 근거로 사용하지 않는다.
- `ksa_items.ksa_text_raw` 같은 원천 필드는 수정하지 않는다.
- 사람이 확정하지 않은 자동 정의는 `definition_status='missing'` 또는 review 대기 상태로 둔다.
- `.env`와 서비스 키는 출력하거나 커밋하지 않는다.
- 운영 수집/전처리는 특정 `major_code="02"`에 고정하지 않는다. 02는 smoke/debug/query 예시로만 사용한다.

## Standard Handoff

각 서브에이전트는 결과를 다음 구조로 반환한다.

1. 목표와 입력
2. 수행한 작업 또는 권장 작업
3. 증거 파일/명령/쿼리
4. 발견 사항
5. 남은 위험과 다음 조치
