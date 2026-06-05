# NCS MCP 아키텍처

## 목적

이 프로젝트는 NCS정보망 Excel DB를 구조화된 SQLite 지식베이스로 정규화하고, MCP 서버를 통해 AI가 호출할 수 있게 만드는 것을 목표로 한다. 핵심은 PDF 문장 검색이 아니라 `직무-능력단위-능력단위요소-수행준거-KSA` 관계를 구조적으로 조회하는 것이다.

## 데이터 소스

- `data/raw/ncs_info_network_db_2026_02.xlsx`: 전체 NCS 계층의 원천 데이터. 능력단위요소, 수행준거, KSA를 포함한다.
- HRDK API `/NCS005`: 능력단위명, 수준, 정의를 검증·보강한다.
- HRDK API `/NCS004`: 세분류/직무 정의(`DUTY_DEF`)를 보강한다.
- HRDK API `/NCS006`: 능력단위요소명과 요소수준을 검증한다. 중복 차수 반환을 피하기 위해 항상 `USG_YN=Y`를 사용한다.

## 처리 파이프라인

```text
Excel 원천
  -> preprocess_excel.py
  -> data/processed/ncs.db
  -> quality.py
  -> collect_api.py (/NCS005, /NCS004, /NCS006)
  -> server.py MCP tools
```

반복 실행과 검증은 `scripts/ncs_harness.py`를 기준으로 한다.

## 핵심 스키마

- `classifications`: 대·중·소·세분류 코드와 API 직무정의.
- `competency_units`: 능력단위 원문 정보와 API 정의.
- `competency_elements`: 능력단위요소 원문 정보와 API 검증 상태.
- `performance_criteria`: 수행준거 원문/정제본.
- `ksa_items`: KSA 원문/정제본.
- `element_criteria_ksa_links`: Excel 원행의 수행준거-KSA 조합 보존.

중요한 구조 원칙: 수행준거와 KSA는 모두 능력단위요소에 귀속된다. KSA를 특정 수행준거의 하위 항목으로 모델링하지 않는다.

## 불변조건

- Excel 원문은 삭제하거나 덮어쓰지 않는다.
- API 데이터는 보강·검증용이며 원천 데이터를 무비판적으로 대체하지 않는다.
- `raw_*`와 `*_refined` 필드는 분리한다.
- 생성 DB는 `data/processed/`, 리포트는 `reports/` 아래에 둔다.
- API 키는 `.env`에만 두고 출력하거나 커밋하지 않는다.
