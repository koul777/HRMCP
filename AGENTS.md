# Repository Guidelines

## 프로젝트 지도

이 저장소는 NCS정보망 Excel DB를 SQLite로 정규화하고, API 기준정보로 검증·보강한 뒤 MCP 서버로 노출하는 프로젝트다. 처음 작업할 때는 아래 문서를 순서대로 본다.

- `ARCHITECTURE.md`: 데이터 소스, 스키마, 불변조건.
- `docs/HARNESS_ENGINEERING.md`: 실행 하네스와 검증 루프.
- `docs/NCS_MCP_PRD.md`: 제품 요구사항과 배경.
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
```

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

`.env`는 비공개다. `NCS_SERVICE_KEY`를 출력하거나 커밋하지 않는다. 생성 DB와 리포트는 재생성 가능한 산출물로 취급한다.

## 수작업 정제

사람이 직접 정제할 때는 대시보드를 사용한다.

```powershell
python scripts\ncs_dashboard.py --host 127.0.0.1 --port 8765
```

비개발 사용자는 저장소 루트의 `run_dashboard.bat`를 더블클릭한다. 대시보드는 온톨로지 준비 전처리 워크벤치로 사용한다. 단계별 진행률, 잔여 작업, 전처리 완료/미처리/실패 항목 리스트, API 매칭 상태, 품질 이슈, 수작업 정제 입력을 함께 제공한다.

원문 필드는 수정하지 않는다. 사람이 보정한 값은 refined 계열 필드에 저장하고 `review_status='human_reviewed'`로 표시한다.
