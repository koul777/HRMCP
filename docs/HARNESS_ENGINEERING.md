# 하네스 엔지니어링

## 에이전트 우선 목표

이 저장소는 Codex 같은 에이전트가 프로젝트 상태를 읽고, 파이프라인을 실행하고, 결과를 검증하고, 다음 작업을 이어갈 수 있도록 설계한다. 긴 설명보다 실행 가능한 점검 명령과 짧은 기준 문서를 우선한다.

## 지식 맵

- `AGENTS.md`: 에이전트와 기여자를 위한 짧은 작업 지도.
- `ARCHITECTURE.md`: 데이터 소스, 스키마, 불변조건의 기준 문서.
- `docs/NCS_MCP_PRD.md`: 제품 요구사항과 프로젝트 의도.
- `docs/NCS_SQF_ONTOLOGY.md`: NCS-SQF 매핑 지식그래프와 추천 설계.
- `docs/NCS_SQF_HARNESS_ENGINEERING.md`: NCS-SQF 경영지원 MVP 실행 루프.
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
python scripts\ncs_harness.py pipeline --api-sqf --sqf-major-code 02
python scripts\ncs_harness.py build-sqf-mappings
python scripts\ncs_harness.py export-package
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
- `api_sqf_report.md`

## NCS-SQF 온톨로지 루프

NCS-SQF 작업은 바로 추천 알고리즘을 만들지 않고, 먼저 실제 데이터를 파악한 뒤 관계 그래프를 만든다. PDF 요약의 원칙처럼 테이블 값보다 의미 관계, 근거 추적, 설명 가능성을 우선한다. 1차 MVP는 SQF `02 > 경영관리 > 경영지원`과 NCS `02 경영·회계·사무`다.

1. 상태 확인:

```powershell
python scripts\ncs_harness.py inspect
```

확인 항목:

- `sqf_service_key_present`가 `true`인지 확인한다.
- `counts.sqf_duties`로 SQF 적재 건수를 확인한다.
- `api_sqf_report.md`가 최신인지 확인한다.

2. SQF 샘플 수집:

```powershell
python scripts\ncs_harness.py pipeline --api-sqf --sqf-major-code 02
```

`/openapi26`의 실제 응답은 Swagger 예시와 다를 수 있다. 현재 확인된 구조는 최상위 `data` 배열과 `dataInfo` 객체다. 정상 코드는 `000`, 빈 데이터는 `002 empty data`로 온다.

3. 전체 SQF 수집:

```powershell
python scripts\ncs_harness.py pipeline --api-sqf
```

수집 후에는 대분류별 건수, 교육훈련·자격·경력 필드 충실도, 빈 대분류를 확인한다. 현재 관찰상 SQF 교육훈련 필드는 일부 산업에만 채워져 있으므로, 추천은 SQF 필드와 NCS 능력단위/KSA를 함께 사용해야 한다.

4. 매핑 그래프 생성:

```powershell
python scripts\ncs_harness.py build-sqf-mappings
python scripts\ncs_harness.py build-sqf-mappings --all-sqf --major-code 02
python scripts\ncs_harness.py pipeline --build-sqf-mappings --smoke
python scripts\ncs_harness.py evaluate --scope-tag business_accounting_office_02
```

NCS와 SQF는 1:1 구조가 아니므로 `sameAs`로 단정하지 않는다. SQF 직무수준과 NCS 능력단위 사이에는 별도 매핑 객체를 둔다.
추천/갭분석 기본 매핑은 `score >= 7`, `relation != related`, `review_status != rejected` 품질 게이트를 통과해야 한다.

```text
Mapping
  source: SQF duty level
  target: NCS competency unit | element | KSA
  relation: requires | closeMatch | partiallyCovers
  confidence: official | lexical | reviewed
  evidenceSource: SQF API | NCS DB | API | human review
  version: source version
```

5. 추천 검증:

사용자가 원하는 업무를 질의하면 결과는 단순 교육명 목록이 아니라 아래 근거를 포함해야 한다.

- 매칭된 SQF 직무와 직무수준
- 직접 제공된 교육훈련·자격·경력 조건
- 연결된 NCS 능력단위
- 부족한 능력단위요소, 수행준거, KSA
- 매칭 점수와 근거 텍스트

공식 인정·평가는 MVP 범위가 아니다. 하네스의 성공 기준은 역량 탐색, 교육 추천, 부족역량 설명이 재현 가능하고 근거 추적 가능하게 나오는 것이다.

6. 핸드오프 패키지 생성:

```powershell
python scripts\ncs_harness.py export-package
python scripts\ncs_harness.py export-package --db-mode hardlink
```

첫 명령은 문서와 SQL만 생성한다. 두 번째 명령은 `exports/ncs_sqf_output/data/db/ncs_sqf.sqlite` 전달용 DB 이름을 하드링크로 만든다.

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
LLM/사람 검토용 JSONL 왕복도 원문을 덮어쓰지 않고 `refinement_jobs`에만 저장한다.

```powershell
python scripts\ncs_harness.py refine export-jsonl --issue-types short_ksa,duplicate_text --limit 100 --out data/refinement/export.jsonl
python scripts\ncs_harness.py refine import-jsonl --input data/refinement/results.jsonl
```

## 운영 원칙

- 셸 명령에서는 한글명 필터보다 코드 필터를 우선한다. 예: `major_code=02`, `middle_code=02`, `small_code=02`, `sub_code=01`.
- `/NCS006` 전체 수집은 트래픽 제한 때문에 배치로 실행한다.
- 같은 실패가 반복되면 더 큰 프롬프트를 던지지 말고 하네스, 문서, 불변조건 검사를 개선한다.
# SQF Library Collection

SQF library reports are collected as ontology source evidence. Metadata is stored in `sqf_library_posts`, `sqf_library_files`, and `sqf_document_sources`; attachments are downloaded into `data/raw/sqf_docs`.

```powershell
python scripts\ncs_harness.py collect-sqf-library --start-page 0 --end-page 10
python scripts\ncs_harness.py collect-sqf-library --start-page 0 --end-page 10 --download --timeout 60
python scripts\ncs_harness.py pipeline --collect-sqf-library --download-sqf-library --timeout 60
python scripts\ncs_harness.py build-sqf-sqlite-model
python scripts\ncs_harness.py preprocess-sqf-documents --chunk-chars 2400 --overlap-chars 250
python scripts\ncs_harness.py build-sqf-sqlite-model --summary
```

The downloader posts to `/common/file/downloadFile.do` with `sysDstinCd`, `fileMstky`, `filedetlSeq`, and `downlDstinCd`.
