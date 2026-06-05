# 하네스 엔지니어링

## 에이전트 우선 목표

이 저장소는 Codex 같은 에이전트가 프로젝트 상태를 읽고, 파이프라인을 실행하고, 결과를 검증하고, 다음 작업을 이어갈 수 있도록 설계한다. 긴 설명보다 실행 가능한 점검 명령과 짧은 기준 문서를 우선한다.

## 지식 맵

- `AGENTS.md`: 에이전트와 기여자를 위한 짧은 작업 지도.
- `ARCHITECTURE.md`: 데이터 소스, 스키마, 불변조건의 기준 문서.
- `docs/NCS_MCP_PRD.md`: 제품 요구사항과 프로젝트 의도.
- `docs/HARNESS_ENGINEERING.md`: 실행 하네스와 에이전트 작업 방식.
- `reports/*.md`: 전처리, 품질진단, API 보강 결과의 증거 자료.

## 하네스 명령

저장소 루트에서 실행한다.

```powershell
$env:PYTHONPATH="C:\Workplace\NCS_MCP\src"
python scripts\ncs_harness.py inspect
python scripts\ncs_harness.py smoke
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py plan-elements --batch-size 8000
python scripts\ncs_harness.py dashboard
```

선택 단계 실행:

```powershell
python scripts\ncs_harness.py pipeline --quality --smoke
python scripts\ncs_harness.py pipeline --api-standards --api-subd --smoke
python scripts\ncs_harness.py pipeline --api-elements-hr --smoke
```

전체 전처리는 `--reset` 사용 시 기존 DB를 재생성한다.

```powershell
python scripts\ncs_harness.py pipeline --preprocess --reset --quality --smoke
```

## 피드백 루프

큰 변경 후에는 아래 세 가지를 통과해야 한다.

```powershell
python -m unittest discover -s tests -v
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
```

데이터 변경이 있었다면 관련 리포트를 결과 요약에 포함한다.

- `preprocess_summary.md`
- `quality_issues.md`
- `api_join_report.md`
- `api_subd_report.md`
- `api_elements_report.md`

## 대시보드와 수작업 정제

대시보드는 단순 진행률 화면이 아니라 온톨로지 준비 전처리 워크벤치다. Codex가 전처리를 실행한 뒤 사람이 다음을 확인한다.

```powershell
python scripts\ncs_dashboard.py --host 127.0.0.1 --port 8765
```

사용자가 파일로 실행할 때는 저장소 루트의 `run_dashboard.bat`를 더블클릭한다.

브라우저에서 `http://127.0.0.1:8765`를 연다. 대시보드는 다음을 제공한다.

- 단계별 전처리 진행률과 잔여 작업 확인
- 분류코드별 DB 전처리 결과 조회
- 능력단위별 API 매칭 상태와 요소 검증 상태 조회
- Excel에는 없고 API에만 존재하는 능력단위 조회
- `/NCS006` 요소 검증 상태 확인
- 전처리 완료/미처리/실패 항목 클릭 후 상세 리스트 조회
- 품질 이슈 목록 조회
- 분류, 능력단위, 요소, 수행준거, KSA의 수작업 정제본 입력
- 이슈 해결 처리

수작업 정제는 원문을 바꾸지 않는다. refined 계열 필드에만 저장하고 `review_status='human_reviewed'`로 표시한다.

## 운영 원칙

- 셸 명령에서는 한글명 필터보다 코드 필터를 우선한다. 예: `major_code=02`, `middle_code=02`, `small_code=02`, `sub_code=01`.
- `/NCS006` 전체 수집은 트래픽 제한 때문에 배치로 실행한다.
- 같은 실패가 반복되면 더 큰 프롬프트를 던지지 말고 하네스, 문서, 불변조건 검사를 개선한다.
