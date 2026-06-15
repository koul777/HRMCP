# Repository Guidelines

## 프로젝트 지도

이 저장소는 NCS정보망 Excel DB를 SQLite로 정규화하고, API 기준정보로 검증·보강한 뒤 MCP 서버로 노출하는 프로젝트다. 처음 작업할 때는 아래 문서를 순서대로 본다.

- `ARCHITECTURE.md`: 데이터 소스, 스키마, 불변조건.
- `docs/HARNESS_ENGINEERING.md`: 실행 하네스와 검증 루프.
- `docs/NCS_MCP_PRD.md`: 제품 요구사항과 배경.
- `docs/NCS_SQF_PROJECT_SYSTEM.md`: PRD 기반 전체 프로젝트 체계와 최종 MCP 발전 로드맵.
- `docs/NCS_SQF_PURPOSE_FROM_SOURCE.md`: `미래 교육 품질, NCS에서 길을 찾다.pdf`에서 추출한 NCS-SQF 연결 취지.
- `docs/NCS_SQF_ONTOLOGY.md`: NCS-SQF 온톨로지, 매핑, 추천 설계.
- `docs/NCS_SQF_HARNESS_ENGINEERING.md`: NCS-SQF 경영지원 MVP 하네스와 검증 루프.
- `docs/NCS_SQF_HANDOFF.md`: SQLite DB, schema, data dictionary, sample query 전달 패키지.
- `docs/SQF_SQLITE_ONTOLOGY_SYSTEM.md`: SQF API, 자료실 보고서, OCR/HWP 전처리, JSON-LD 산출 체계.
- `docs/CHATGPT_PRO_PROGRAM_BRIEF.md`: ChatGPT Pro에게 프로젝트를 정확히 전달하기 위한 설명서.
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
python scripts\ncs_harness.py collect-sqf-library --download --timeout 60
python scripts\ncs_harness.py build-sqf-sqlite-model
python scripts\ncs_harness.py preprocess-sqf-documents --ocr-empty --ocr-lang kor+eng --ocr-dpi 160
python scripts\ncs_harness.py build-sqf-precision-matches --min-score 9 --max-matches-per-chunk 8
python scripts\ncs_harness.py build-sqf-mappings --all-sqf --duty-limit 5000 --limit-per-duty 10
python scripts\ncs_harness.py ontology validate
python scripts\ncs_harness.py ontology export-jsonld --out exports\ncs_sqf_ontology.jsonld
python scripts\ncs_harness.py export-package
```

로컬 정책/개념 PDF는 다음처럼 온톨로지 원천으로 먼저 등록한다.

```powershell
python scripts\ncs_harness.py import-ontology-source --input "<local-pdf>" --title "<title>" --role framework_reference
python scripts\ncs_harness.py preprocess-sqf-documents --only-unprocessed --ocr-empty --ocr-lang kor+eng --ocr-dpi 160
```

## 학습모듈과 보고서 보강 원칙

교육 추천은 API 원천을 우선하지만, API가 모든 능력단위의 학습모듈을 충분히 반환한다고 가정하지 않는다. 학습모듈 API의 기본 항목은 `결과코드`, `결과메시지`, `대분류코드`, `대분류코드명`, `중분류코드`, `중분류코드명`, `소분류코드`, `소분류코드명`, `세분류코드`, `세분류코드명`, `학습모듈번호`, `학습모듈명`, `학습모듈내용`이다. API 응답 코드가 성공이어도 특정 `modulNm` 질의가 `002 empty data`이거나 내용이 빈약할 수 있다.

처리 우선순위는 아래 순서를 따른다.

1. 학습모듈 API에서 정확한 `학습모듈번호`와 `학습모듈명`이 있으면 `ncs_learning_modules`에 원천 API 행으로 저장한다.
2. API에 없지만 공식 NCS 학습모듈 PDF가 있으면 `role = ncs_learning_module`로 등록하고, `학습모듈의 개요`에서 목표, 선수학습, 내용체계, 핵심 용어를 추출해 `ncs_learning_modules`를 보강한다.
3. NCS 활용패키지 PDF는 `role = ncs_learning_package`로 등록하고, 수행준거, KSA, 자가진단, 직무기술서 근거로 사용한다. 정확한 학습모듈번호가 없으면 공식 학습모듈 행으로 가장하지 않는다.
4. SQF 보고서와 개발 매뉴얼은 `role = sqf_report` 또는 `framework_reference`로 등록하고, SQF 직무수준과 NCS 능력단위의 필수/선택 관계 근거로 사용한다.
5. 그래도 직접 학습모듈이 없으면 NCS 능력단위, 수행준거, KSA를 학습목표로 변환해 `NCS-derived education plan`으로 추천한다.

자료가 방대하므로 전체 파일을 무차별로 넣지 않는다. 대상 범위, API 공백, 추천 실패 케이스를 기준으로 공식 학습모듈 PDF, NCS 활용패키지, SQF 보고서를 자산 단위로 순차 등록한다. 다만 운영형 v1에서는 대상 SQF/NCS 범위의 공식 보고서와 학습모듈 PDF가 모두 `extracted` 상태가 되어야 한다.

로컬 학습모듈 PDF를 넣을 때는 파일명 또는 본문에서 `LM0202020101_19v2` 같은 안정적인 학습모듈번호를 추출한다. 현재 NCS DB의 능력단위 코드가 `0202020101_23v3`처럼 더 최신이면 기본 코드(`0202020101`)로 연결하되, 원래 PDF 버전은 `source_payload`와 `evidence_text`에 보존한다. 연결은 `learning_module_unit_links`에 저장하고 `link_method = local_pdf_unit_code`, 높은 confidence를 사용한다.

같은 PDF가 `(1)`, `(2)`, `(3)`처럼 중복으로 있으면 내용 해시로 같은 원천인지 확인하고 중복 등록하지 않는다. 원문 PDF와 API 원천 필드는 덮어쓰지 않고, 로컬 PDF에서 추출한 보강값은 `source_payload`에 출처, 문서 ID, 해시, 추출 위치를 남긴다.

권장 명령 예시는 다음과 같다.

```powershell
python scripts\ncs_harness.py query-study-modules --major-code 02 --module-name "인사기획"
python scripts\ncs_harness.py import-ontology-source --input "<LM...pdf>" --title "NCS 학습모듈 - <능력단위명>" --role ncs_learning_module
python scripts\ncs_harness.py import-ontology-source --input "<NCS-package.pdf>" --title "NCS 활용패키지 - <직무명>" --role ncs_learning_package
python scripts\ncs_harness.py import-ontology-source --input "<SQF-report.pdf>" --title "SQF 보고서 - <분야명>" --role sqf_report
python scripts\ncs_harness.py preprocess-sqf-documents --only-unprocessed --chunk-chars 2400 --overlap-chars 250 --ocr-empty --ocr-lang kor+eng --ocr-dpi 160
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

KQF/SQF 취지는 다음처럼 해석한다. KQF는 NCS 등을 바탕으로 학력, 자격, 현장경력, 교육훈련 이수 결과를 상호 연계하는 국가 수준 체계다. SQF는 산업별 현장에서 통용되는 직무를 도출·표준화하고, 직무수행에 필요한 능력을 구조화하여 교육훈련-학위-자격-현장경력을 연결하는 산업별 골격이다. 따라서 이 저장소의 온톨로지는 PDF 텍스트 검색기가 아니라 직무수준, 직무역량, 능력단위, 학습결과, 경력이동 근거를 잇는 materialized graph여야 한다.

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

온톨로지 작업을 바꾸면 추가로 아래를 확인한다.

```powershell
python scripts\ncs_harness.py ontology validate
python scripts\ncs_harness.py ontology export-jsonld --out exports\ncs_sqf_ontology.jsonld
```

온톨로지 완료 기준:

- 모든 SQF 문서 자산이 `extracted` 상태다.
- SQF API 원천 행과 `sqf_job_levels_normalized` 수가 일치한다.
- `sqf_ncs_matches`에는 전체 SQF 범위 후보가 생성되어 있다.
- `sqf_chunk_job_level_matches`에는 보고서/OCR/HWP 근거 후보가 생성되어 있다.
- MCP의 `analyze_gap`, `recommend_next_ncs_units`, `recommend_education_for_duty`, `search_sqf_precision_matches`가 샘플 DB에서 응답한다.
- JSON-LD export가 생성된다.

## 보안

`.env`는 비공개다. `NCS_SERVICE_KEY`와 `NCS_SQF_SERVICE_KEY`를 출력하거나 커밋하지 않는다. 생성 DB와 리포트는 재생성 가능한 산출물로 취급한다.

## 수작업 정제

사람이 직접 정제할 때는 대시보드를 사용한다.

```powershell
python scripts\ncs_dashboard.py --host 127.0.0.1 --port 8765
```

비개발 사용자는 저장소 루트의 `run_dashboard.bat`를 더블클릭한다. 대시보드는 온톨로지 준비 전처리 워크벤치로 사용한다. 단계별 진행률, 잔여 작업, 전처리 완료/미처리/실패 항목 리스트, API 매칭 상태, 품질 이슈, 수작업 정제 입력을 함께 제공한다.

원문 필드는 수정하지 않는다. 사람이 보정한 값은 refined 계열 필드에 저장하고 `review_status='human_reviewed'`로 표시한다.

## 온톨로지 구축 방향

이 프로젝트의 최종 목표는 단순한 KSA 텍스트 정제가 아니라 `NCS -> 능력단위 -> 능력단위요소 -> 수행준거 -> 지식/기술/태도 -> 개념 정의 -> 개념 관계`까지 연결되는 NCS 기반 HR Ontology 구축이다. 이후 "온톨로지", "KSA 정제", "지식기술태도", "개념 정의", "관계 연결" 작업 지시가 나오면 항상 이 방향을 우선한다.

KSA 행은 단순 문자열이 아니라 온톨로지 후보 노드다. `ksa_items`는 원천 데이터를 보존하고, 온톨로지 작업은 별도 테이블에 저장한다.

- `ontology_concepts`: 대표 개념명, 개념 유형(`knowledge`, `skill`, `attitude`), 정의, 정의 작성 상태, 관계 연결 상태, 검토 상태.
- `ontology_concept_aliases`: 동일 개념의 별칭. 예: `직업정보론`, `직업 정보론`, `직업정보 이론`.
- `ontology_concept_relations`: 상위 개념, 하위 개념, 관련 개념 관계.
- `ksa_concept_links`: 원천 KSA 행과 대표 개념 노드의 연결.
- `criteria_concept_links`: 수행준거와 온톨로지 개념의 연결.

KSA 상세 화면이나 저장 로직을 바꿀 때는 아래 필드를 분리해서 다룬다.

- KSA 유형: 지식 / 기술 / 태도.
- KSA 원문: Excel 원천 문자열. 수정하지 않는다.
- 대표 개념명: 사람이 표준화하는 개념명.
- 개념 정의: 온톨로지 노드의 정의. KSA 원문을 자동 복사해 정의로 취급하지 않는다.
- 별칭: 같은 개념으로 통합할 표현.
- 상위 개념 / 하위 개념 / 관련 개념: `ontology_concept_relations`에 저장한다.
- 관련 수행준거 / 관련 능력단위요소 / 관련 능력단위: 원천 링크와 온톨로지 링크로 추적한다.

## 온톨로지 대시보드 원칙

대시보드는 "데이터 전처리 현황판"이 아니라 "온톨로지 구축 관리 시스템"으로 발전시킨다. UI를 바꿀 때는 분류 선택과 아래 내용이 항상 같은 범위로 연동되어야 한다.

- 대분류, 중분류, 소분류, 세분류를 클릭하면 그 선택 범위 기준으로 모든 현황이 바뀐다.
- 온톨로지 준비 단계, 작업 카드, 품질 이슈, KSA 개념 목록은 같은 선택 범위 필터를 공유한다.
- 지식/기술/태도별 온톨로지 구축 현황을 보여준다.
- 최소 집계 항목은 전체 개념 수, 정의 작성 완료, 정의 미작성, 관계 연결 완료, 관계 미연결, 검토 완료다.
- 온톨로지 작업 워크벤치에는 지식/기술/태도별 `정의 미작성`, `관계 미연결`, `중복 후보`, `검토 완료` 작업 대상을 클릭해서 볼 수 있어야 한다.
- KSA는 납작한 목록보다 `능력단위 -> 능력단위요소 -> 수행준거 -> KSA(지식/기술/태도)` 트리 안에서 보는 화면을 우선한다.

## 온톨로지 작업 불변조건

- 원천 Excel 필드와 `ksa_items.ksa_text_raw`는 수정하지 않는다.
- KSA 원문이 짧거나 단어 하나여도 그것을 그대로 "정의"로 저장하지 않는다.
- 정의가 없으면 빈 정의로 표시하고, `definition_status='missing'` 상태로 둔다.
- 사람이 정의를 작성하면 `ontology_concepts.definition`에 저장하고 `definition_status='defined'`, `review_status='human_reviewed'`로 표시한다.
- 대표 개념명 변경은 원천 KSA 변경이 아니라 `ontology_concepts.concept_name` 및 `ksa_concept_links` 변경으로 처리한다.
- 동일 개념 통합은 원문 삭제가 아니라 대표 개념 + 별칭 + 링크 재연결로 처리한다.
- 개념 관계는 문자열 덮어쓰기가 아니라 `ontology_concept_relations`에 구조적으로 저장한다.
- 수행준거와 개념의 연결은 `criteria_concept_links`에 저장한다.
