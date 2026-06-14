# Repository Guidelines

## 프로젝트 지도

이 저장소는 NCS정보망 Excel DB를 SQLite로 정규화하고, API 기준정보로 검증·보강한 뒤 MCP 서버로 노출하는 프로젝트다. 처음 작업할 때는 아래 문서를 순서대로 본다.

- `ARCHITECTURE.md`: 데이터 소스, 스키마, 불변조건.
- `docs/HARNESS_ENGINEERING.md`: 실행 하네스와 검증 루프.
- `docs/NCS_MCP_PRD.md`: 제품 요구사항과 배경.
- `docs/NCS_SQF_PROJECT_SYSTEM.md`: PRD 기반 전체 프로젝트 체계와 최종 MCP 발전 로드맵.
- `docs/NCS_SQF_ONTOLOGY.md`: NCS-SQF 온톨로지, 매핑, 추천 설계.
- `docs/NCS_SQF_HARNESS_ENGINEERING.md`: NCS-SQF 경영지원 MVP 하네스와 검증 루프.
- `docs/NCS_SQF_HANDOFF.md`: SQLite DB, schema, data dictionary, sample query 전달 패키지.
- `reports/*.md`: 최근 전처리, 품질진단, API 보강 결과.

## 주요 디렉터리

- `src/ncs_mcp/`: 전처리, DB 스키마, API 수집, 품질진단, MCP 서버 코드.
- `tests/`: 단위 테스트.
- `scripts/`: 에이전트와 개발자가 반복 실행하는 하네스.
- `data/raw/`: 원천 Excel 파일. 대용량/민감 데이터는 커밋하지 않는다.
- `data/processed/`: 생성 SQLite DB.
- `reports/`: 생성 리포트.

## 핵심 명령

저장소 루트에서 실행한다.

```powershell
$env:PYTHONPATH="C:\Workplace\NCS_MCP\src"
python scripts\ncs_harness.py inspect
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
python scripts\ncs_harness.py dashboard
python -m unittest discover -s tests -v
```

전처리와 API 보강은 하네스를 통해 선택 실행한다.

```powershell
python scripts\ncs_harness.py pipeline --preprocess --reset --quality --smoke
python scripts\ncs_harness.py pipeline --api-standards --api-subd --smoke
python scripts\ncs_harness.py pipeline --api-elements-hr --smoke
python scripts\ncs_harness.py pipeline --api-sqf --sqf-major-code 02
python scripts\ncs_harness.py build-sqf-mappings
python scripts\ncs_harness.py export-package
```

## NCS-SQF 온톨로지 작업 원칙

이 프로젝트의 다음 핵심 목표는 NCS 상세 역량 그래프와 SQF 산업별 직무 그래프를 연결해, 사용자가 원하는 업무를 물었을 때 근거가 추적되는 교육 추천을 제공하는 것이다. PDF 요약의 원칙처럼 값 나열보다 관계 중심 그래프를 우선한다. 1차 MVP 범위는 SQF `02 > 경영관리 > 경영지원`과 NCS `02 경영·회계·사무`다.

온톨로지 작업은 아래 순서로 진행한다.

1. `inspect`로 DB, API 키, 기존 수집 상태를 확인한다.
2. SQF API는 먼저 한 대분류만 샘플 수집한다. 예: `--api-sqf --sqf-major-code 02`.
3. 실제 응답 구조와 필드 충실도를 확인한 뒤 전체 수집한다. SQF `/openapi26`은 성공 코드가 `000`, 빈 데이터 코드가 `002`일 수 있다.
4. `ncs_lclas_cd = classifications.major_code`는 확정 연결로 사용한다.
5. SQF 직무와 NCS 세분류/능력단위/요소/KSA 연결은 별도 매핑 객체에 관계, 점수, 방식, 근거, 버전을 저장한다.
6. 추천 결과는 항상 `SQF 직무`, `NCS 능력단위`, `KSA/수행준거`, `교육훈련/자격/경력`, `매칭 근거`를 함께 반환해야 한다.

SQF의 `dutyEduTrain`, `dutyQualf`, `dutyCarr`는 일부 산업에만 채워져 있으므로, 교육 추천은 이 필드만으로 만들지 않는다. 비어 있는 경우 NCS 능력단위와 KSA를 학습 목표로 변환해 보완 추천한다.

공식 인정·평가와 추천·갭분석은 분리한다. 이 저장소의 1차 목표는 공식 판정이 아니라 NCS-SQF 연결 지식그래프 기반의 역량 탐색, 교육 추천, 부족역량 설명이다. `sameAs` 단정은 피하고 `requires`, `closeMatch`, `partiallyCovers`, `evidenceSource`, `confidence`, `version`을 사용한다.

## 코딩 규칙

Python 3, 4칸 들여쓰기, `snake_case`를 사용한다. 데이터 변환은 명시적인 함수와 SQL로 처리하고, 임의 문자열 파싱을 남발하지 않는다. API 데이터는 원천 Excel을 덮어쓰는 용도가 아니라 검증·보강용이다.

## 테스트 기준

변경 후 최소 아래를 실행한다.

```powershell
python -m unittest discover -s tests -v
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
```

스키마, 전처리 중복 제거, API 파서, MCP 응답 구조를 바꾸면 관련 테스트를 추가한다.

## 보안

`.env`는 비공개다. `NCS_SERVICE_KEY`와 `NCS_SQF_SERVICE_KEY`를 출력하거나 커밋하지 않는다. 생성 DB와 리포트는 재생성 가능한 산출물로 취급한다.

## 수작업 정제

사람이 직접 정제할 때는 대시보드를 사용한다.

```powershell
python scripts\ncs_dashboard.py --host 127.0.0.1 --port 8765
```

비개발 사용자는 저장소 루트의 `run_dashboard.bat`를 더블클릭한다. 대시보드는 온톨로지 준비 전처리 워크벤치로 사용한다. 단계별 진행률, 잔여 작업, 전처리 완료/미처리/실패 항목 리스트, API 매칭 상태, 품질 이슈, 수작업 정제 입력을 함께 제공한다.

원문 필드는 수정하지 않는다. 사람이 보정한 값은 refined 계열 필드에 저장하고 `review_status='human_reviewed'`로 표시한다.
