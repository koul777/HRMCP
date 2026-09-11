# 4단계 — 검색 모듈 분리 및 후속 계획

작성일: 2026-09-11

## 원인 분석

- `src/ncs_mcp/server.py`는 공개 도구, 렌더링, 검색 SQL, 추천 facade가 한 파일에 함께 있어 검색 변경의 영향 범위를 파악하기 어렵다.
- NCS 검색 블록은 `_ncs_search_markdown`부터 `search_ncs`까지 약 800줄이며 정규화, tier 생성, SQL 실행, 결과 병합을 자체적으로 완결한다.
- 외부 의존성은 `open_db`, `clamp_limit`, `unit_path` 세 서버 helper다. 일부 진단 스크립트는 `server._ncs_search_tier_predicates`와 `server._execute_ncs_search_tiers`를 임시 교체하므로 이 호환성도 보존해야 한다.

## 검색 모듈 변경 계획

1. `src/ncs_mcp/search/core.py`로 검색 상수, 정규화, tier/점수 생성, SQL 실행, 결과 병합, `search_ncs`를 이동한다.
2. `src/ncs_mcp/search/__init__.py`에서 기존 함수와 상수를 export한다.
3. `server.py`는 같은 이름을 import해 기존 `from ncs_mcp.server import search_ncs` 경로를 유지한다. DB와 공통 helper는 작은 runtime callback으로 주입해 순환 import를 막는다.
4. 로컬과 `deploy/vercel_mcp_app` 미러를 동시에 수정한다.
5. 검색 회귀, 검색 평가, 서버/배포 parity, 전체 unittest, MCP 계약 불변을 검증한다.

## 후속 후보 계획 — 이번 단계에서 구현하지 않음

### `db.py`

- 스키마 DDL을 도메인별 모듈(`schema/core.py`, `schema/ontology.py`, `schema/training.py`, `schema/review.py`)로 먼저 분리한다.
- 연결/transaction/read-only 정책은 `db.py`에 남기고 DDL registry만 외부화한다.
- 마이그레이션 순서와 idempotency가 동작 계약이므로 테이블군 하나씩 분리하고 schema 초기화 테스트를 매번 실행한다.

### `training_recommendation.py`

- 먼저 순수 출력 변환(`compact_*`, 교육체계 matrix/guide trace)을 `training/presentation.py`로 분리한다.
- 다음으로 질의 범위 해석, 후보 조회, 점수 계산, transition orchestration을 각각 분리하되 public facade는 기존 모듈에서 re-export한다.
- 추천 점수와 순위는 golden fingerprint 및 전체 추천 테스트가 완전히 일치할 때만 병합한다. 이번 4단계에서는 계획만 기록하고 구현하지 않는다.

## 구현·검증 결과

### 변경 파일

- `src/ncs_mcp/search/__init__.py`, `src/ncs_mcp/search/core.py`
- `deploy/vercel_mcp_app/src/ncs_mcp/search/__init__.py`, `deploy/vercel_mcp_app/src/ncs_mcp/search/core.py`
- `src/ncs_mcp/server.py`, `deploy/vercel_mcp_app/src/ncs_mcp/server.py`
- `scripts/audit_ncs_search_precision.py`
- `scripts/benchmark_ncs_search_normalization.py`
- `tests/test_ncs_search_recall.py`
- `reports/debug_2026-09/04_refactor_plan.md`

### 핵심 변경

- 검색 구현 852줄을 독립 `search` 패키지로 이동했다. 로컬 `server.py`는 4,526줄로 줄었고, Vercel 미러도 같은 구조와 바이트를 유지한다.
- `server.search_ncs`는 패키지의 동일 함수 객체를 재노출하므로 기존 공개 import 경로와 호출 계약이 유지된다.
- 검색 패키지는 서버를 역참조하지 않고 `open_db`, `clamp_limit`, `unit_path`를 runtime callback으로 받는다. 서버를 import하는 순환 의존성은 생기지 않았다.
- 1단계 기준선 재현 및 정규화 벤치마크가 기존처럼 검색 helper를 임시 교체할 수 있도록 진단 스크립트의 monkeypatch 대상을 패키지 구현까지 연결했다.
- DB 스키마, 원천 데이터, 검색 SQL/가중치, 공개 MCP 도구 계약은 변경하지 않았다.

### 검색 품질 불변 확인

동일한 40개 질의, `scope=unit`, `limit=10` 조건으로 2단계 결과와 비교했다.

| 버전 | Hit@1 | Hit@3 | MRR@10 | 검색 오류 |
|---|---:|---:|---:|---:|
| 2단계 기준 | 0.4750 | 0.5000 | 0.5062 | 0 |
| 4단계 분리 후 | 0.4750 | 0.5000 | 0.5062 | 0 |
| 변화 | 0 | 0 | 0 | 0 |

Hit@3 0.7 게이트는 기존과 동일하게 `warn` 상태다. 이는 4단계 회귀가 아니라 2단계에서 기록한 후속 검색 품질 과제다.

### 성능 확인

1단계 수정 후 기록된 중앙값과 같은 5개 질의를 로컬 warm 상태에서 7회 재측정했다. 실행 환경 변동을 포함한 참고 비교이며, 가장 큰 차이는 `급여 계산`의 +11.9%로 20% 기준 안이다.

| 질의 | 1단계 수정 후 | 4단계 분리 후 | 변화율 |
|---|---:|---:|---:|
| 성과평가 제도 설계 | 143.237 ms | 148.904 ms | +4.0% |
| 직원 교육훈련 계획 수립 | 113.204 ms | 115.208 ms | +1.8% |
| 인사 채용관리 | 111.427 ms | 119.914 ms | +7.6% |
| 인사평가 | 83.954 ms | 87.171 ms | +3.8% |
| 급여 계산 | 95.390 ms | 106.781 ms | +11.9% |

### 검증 결과

- `python -m unittest discover -s tests`: 2,121개 통과, 1개 환경상 skip, 실패 0개, 761.385초.
- 검색 벤치마크 관련 테스트: 24개 통과.
- 검색 회귀·자연어 평가·공개 payload·배포 parity 표적 테스트: 23개 통과.
- `python scripts/ncs_harness.py lint`: 오류 0, 경고 0.
- `python scripts/ncs_harness.py smoke`: 통과.
- 로컬/Vercel `server.py`, `search/__init__.py`, `search/core.py` byte parity: 통과.
- `python scripts/export_mcp_tool_contract.py --out mcp/ncs-tool-contract.json --check`: 통과. `mcp/ncs-tool-contract.json` 변경 없음.

### 미해결 이슈와 배포 영향

- 자연어 평가 Hit@3는 여전히 0.5000으로 목표 0.7에 미달한다. 특히 2단계 보고서에 기록한 총무·인사 동의어 보강이 다음 검색 품질 후보이며, 이번 동작 불변 리팩터링 범위에는 포함하지 않았다.
- `db.py`와 `training_recommendation.py`는 위 계획만 작성했으며 구현하지 않았다.
- 공개 API와 계약이 동일하므로 배포 호환성 영향은 없다. 새 `search` 패키지가 Vercel 미러에 함께 포함되는지는 parity 및 전체 배포 테스트로 검증했다.

### Production 배포 확인

- Vercel Production 배포 `dpl_C7C7gbkDWsjjf161GwULqceYdPJ9`가 READY 상태로 완료됐고 canonical alias `https://ncs-mcp-bridge-mini2.vercel.app`에 연결됐다.
- 원격 MCP 전송 검증은 `ok=true`, `failures=[]`였으며 공개 도구 7개와 `career_path`, `qualification`, `job_base`, `ontology` 분석 모드를 모두 확인했다.
- 원격 서버 버전은 `0.1.0+git.c93b3e2ff3ed34ce6524389631491976ca06ef98`로 이번 검색 모듈 커밋과 일치한다.
- 원격 `ncs_search(query="인사 채용관리", scope="unit", limit=3)`는 HTTP 200으로 `인력채용(0202020103_23v4)`을 1위에 유지했다.
