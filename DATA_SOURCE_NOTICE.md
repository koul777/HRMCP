# HRMCP 데이터 출처·이용조건 고지

최종 확인일: **2026-08-30**

이 문서는 HRMCP 저장소가 연결하거나 운영자가 입력할 수 있는 외부 데이터 원천을 하나씩 구분하고,
공식 출처, 코드 연결 지점, 현재 제품에서의 역할, 확인된 이용조건을 기록합니다. 법률 자문이 아니며,
실제 재배포 전에는 각 원천의 최신 공식 페이지와 개별 파일에 표시된 이용조건을 다시 확인해야 합니다.

## 먼저 구분해야 할 사항

- **프로젝트 코드와 원천 데이터의 권리는 서로 다릅니다.** 데이터 이용조건이 코드의 복제·수정·재배포
  권한을 부여하지 않고, 코드 라이선스도 원천 데이터의 권리를 바꾸지 않습니다.
- 저장소 작성 코드와 문서는 [MIT License](LICENSE)로 제공합니다. MIT는 NCS 원천 데이터, 가공 DB,
  배포 snapshot, OCR 모델, vendor 코드, 다운로드 문서·이미지 등 별도 권리 자료를 재허락하지 않습니다.
  저장소 라이선스의 적용 경계와 제3자 고지는 [NOTICE](NOTICE)를 함께 확인합니다.
- 공공데이터포털에서 아래 API 레코드에 표시된 `이용허락범위 제한 없음`은 해당 **공식 레코드의 표시**를
  옮긴 것입니다. API 키 발급, 활용신청, 운영 승인, 트래픽 한도는 별도로 적용됩니다.
- NCS 누리집의 다운로드 파일·문서·이미지는 API 레코드와 이용조건이 같다고 단정하지 않습니다. NCS
  저작권정책에 따라 개별 저작물의 공공누리 표시와 제3자 권리 포함 여부를 확인해야 합니다.
- HRMCP가 만드는 compact SQLite, 온톨로지 개념·링크·관계와 추천용 색인은 원천을 가공한 산출물입니다.
  가공했다는 이유로 원천 데이터의 이용조건이 사라지거나 더 넓은 권리가 새로 생기지는 않습니다.
- 생성 운영 DB와 배포 snapshot ZIP은 Git 소스 파일로 추적하지 않습니다. 그러나 Vercel 릴리스에서는
  검증된 compact snapshot을 별도 산출물로 스테이징하고 런타임 읽기 전용 DB로 materialize할 수 있으므로,
  Git 미포함 여부와 실제 배포 포함 여부는 각각 확인해야 합니다.

Vercel의 공개 MCP 함수는 사용자 요청 때 아래 외부 API를 다시 호출하지 않습니다. API 수집은 운영자의
upstream refresh 단계에서만 실행되고, 검증된 canonical DB를 Builder가 compact SQLite로 만든 뒤
Vercel이 이를 읽기 전용으로 조회합니다. 따라서 아래의 `코드 연결`은 데이터 수집·갱신 경로이며,
공개 MCP 요청의 실시간 제3자 API 호출 목록이 아닙니다.

수집 코드가 인식하는 인증 환경변수는 아래와 같습니다. 이름만 문서화하며 실제 값은 Git, DB, 로그,
리포트에 기록하지 않습니다.

| 환경변수 | 연결 원천 | 공개 Vercel 요청 시 필요 여부 |
| --- | --- | --- |
| `NCS_SERVICE_KEY` | NCS 기준정보·NCS 관련 정보의 기본 키와 일부 수집기의 fallback | 불필요 |
| `NCS_TRAINING_COURSE_SERVICE_KEY` | NCS 훈련과정 API | 불필요 |
| `NCS_QUALIFICATION_SERVICE_KEY` | 능력단위별 자격 종목 API | 불필요 |
| `NCS_JOB_BASE_SERVICE_KEY` | NCS 직업기초능력 API | 불필요 |
| `NCS_STUDY_MODULE_SERVICE_KEY` | NCS 학습모듈정보 API | 불필요·레거시 |
| `NCS_SQF_SERVICE_KEY` | SQF 분야·직종 API | 불필요·레거시 |

## 현재 활성 데이터 원천

### 1. NCS 정보망 Excel DB

| 항목 | 내용 |
| --- | --- |
| 제공 주체 | 한국산업인력공단 NCS |
| 프로젝트 입력 | `data/raw/ncs_info_network_db_2026_02.xlsx` |
| 코드 연결 | [`preprocess_excel.py`](src/ncs_mcp/preprocess_excel.py) |
| 저장 범위 | `raw_excel_rows`, `classifications`, `competency_units`, `competency_elements`, `performance_criteria`, `ksa_items`, `element_criteria_ksa_links` |
| 제품 역할 | 분류·능력단위·능력단위요소·수행준거·원천 KSA의 canonical 기반 |
| 이용조건 상태 | **파일 단위 재확인 필요**. 현재 DB에는 원 다운로드 URL과 개별 공공누리 표지가 보존되어 있지 않습니다. |

NCS 누리집의 일반 저작권정책만으로 이 Excel 파일을 `이용허락범위 제한 없음`이라고 단정하지 않습니다.
다음 원천 갱신부터는 다운로드 페이지, 공공누리 유형, 취득일, 원본 SHA-256을 함께 보존해야 합니다.

### 2. NCS 기준정보 조회 API — NCS004·NCS005·NCS006

| 항목 | 내용 |
| --- | --- |
| 공식 레코드 | [한국산업인력공단_NCS 기준정보 조회](https://www.data.go.kr/data/15128213/openapi.do) |
| 코드상 base URL | `https://apis.data.go.kr/B490007/hrdkapi` |
| 실제 사용 operation | `/NCS004` 세분류·직무 정의, `/NCS005` 능력단위·정의·수준, `/NCS006` 능력단위요소 검증 |
| 코드 연결 | [`collect_api.py`](src/ncs_mcp/collect_api.py) |
| 저장·보강 범위 | `api_raw_responses`, `api_competency_units`, `classifications`, `competency_units`, `competency_elements`, `quality_issues` |
| 제품 역할 | Excel 원천의 직무 정의·능력단위 정의 보강과 능력단위요소 일치 검증 |
| 공식 레코드 표시 | 무료, `이용허락범위 제한 없음`, 개발·운영 자동승인, 개발계정 10,000건 |

API 응답 원문은 별도 응답 테이블에 보존하고, Excel 원천의 수행준거와 KSA 원문을 덮어쓰지 않습니다.

### 3. NCS 훈련과정 정보 API

| 항목 | 내용 |
| --- | --- |
| 공식 레코드 | [한국산업인력공단_NCS 훈련과정 정보](https://www.data.go.kr/data/15086447/openapi.do) |
| 공식 요청주소 | `https://apis.data.go.kr/B490007/ncsTrainingCource/openapi18` |
| 코드 연결 | [`training_course_api.py`](src/ncs_mcp/training_course_api.py) |
| 저장 범위 | `ncs_training_courses`, `ncs_training_course_unit_links` 및 파생 concept·element·goal·delivery 링크 |
| 제품 역할 | 훈련목표·시간·시설·방법과 NCS 능력단위를 교육추천 근거로 연결 |
| 공식 레코드 표시 | 무료, `이용허락범위 제한 없음`, 개발 자동승인·운영 심의승인, 개발계정 10,000건 |

공식 레코드는 HTTPS 요청주소를 제공합니다. 현재 수집기 상수는 같은 경로의 HTTP 스킴을 사용하므로,
재수집 전 공식 HTTPS 주소 사용 여부를 운영 점검 항목으로 둡니다.

### 4. NCS 능력단위별 자격 종목 조회 API

| 항목 | 내용 |
| --- | --- |
| 공식 레코드 | [한국산업인력공단_NCS 능력단위별 자격 종목 조회 서비스](https://www.data.go.kr/data/15074404/openapi.do) |
| 공식 요청주소 | `https://apis.data.go.kr/B490007/ncsClCdJm/getNcsClCdJmList` |
| 코드 연결 | [`qualification_api.py`](src/ncs_mcp/qualification_api.py) |
| 저장 범위 | `ncs_qualification_items`, `ncs_unit_qualification_links`, `ncs_qualification_collection_status` |
| 제품 역할 | 능력단위와 관련 자격 종목을 보조 근거로 연결. 공식 자격 인정이나 적격성 판정에는 사용하지 않음 |
| 공식 레코드 표시 | 무료, `이용허락범위 제한 없음`, 개발 자동승인·운영 심의승인, 개발계정 1,000건 |

전체 수집은 NCS006 상태와 rate-limit 이력을 확인한 뒤 작은 guarded batch로만 실행합니다.
공식 레코드는 HTTPS 요청주소를 제공합니다. 현재 수집기 상수는 같은 경로의 HTTP 스킴을 사용하므로,
재수집 전 공식 HTTPS 주소 사용 여부를 운영 점검 항목으로 둡니다.

### 5. NCS 직업기초능력 API

| 항목 | 내용 |
| --- | --- |
| 공식 레코드 | [한국산업인력공단_NCS 직업기초능력](https://www.data.go.kr/data/15086440/openapi.do) |
| 공식 요청주소 | `https://apis.data.go.kr/B490007/ncsJobBase/openapi19` |
| 코드 연결 | [`job_base_api.py`](src/ncs_mcp/job_base_api.py) |
| 저장 범위 | `ncs_job_base_competencies`, `ncs_job_base_factors`, `ncs_unit_job_base_links` |
| 제품 역할 | 직무 간 공통·부족 직업기초능력을 전환·교육 검토의 보조 근거로 제공 |
| 공식 레코드 표시 | 무료, `이용허락범위 제한 없음`, 개발 자동승인·운영 심의승인, 개발계정 10,000건 |

공식 레코드는 HTTPS 요청주소를 제공합니다. 현재 수집기 상수는 같은 경로의 HTTP 스킴을 사용하므로,
재수집 전 공식 HTTPS 주소 사용 여부를 운영 점검 항목으로 둡니다.

### 6. NCS 경력개발경로 CSV

| 항목 | 내용 |
| --- | --- |
| 제공 주체 | 한국산업인력공단 NCS |
| 코드 연결 | [`career_path.py`](src/ncs_mcp/career_path.py) |
| 저장 범위 | `ncs_career_paths`와 원본 `source_file`, `source_row_number` |
| 제품 역할 | 직무 전환과 성장 단계의 보조 근거 |
| 이용조건 상태 | **파일 단위 재확인 필요**. 현재 import row에는 원 다운로드 URL과 개별 공공누리 표지가 없습니다. |

경력개발경로 CSV는 사람이 검토하지 않은 행을 자동으로 `reviewed` 또는 `human_reviewed`로 승격하지
않습니다.

## 운영자가 추가할 수 있는 보조 파일

다음 파일은 API에서 자동 수집하는 원천이 아니라 운영자가 제공한 CSV를 별도 테이블로 가져오는
선택적 보조 자료입니다.

| 보조 원천 | 제공기관·현재 파일 | 코드 연결 | 저장 테이블 | 제품 사용 | 이용조건 상태 |
| --- | --- | --- | --- |
| 국가직무능력표준 정보 CSV | 한국산업인력공단, `한국산업인력공단_국가직무능력표준 정보_20251231 (2).csv` | [`supplemental_data.py`](src/ncs_mcp/supplemental_data.py) | `ncs_unit_standard_training` | `context_only`, 추천 점수 미사용 | 원 파일의 공식 URL·공공누리 유형을 운영자가 확인·기록해야 함 |
| 직업능력 코드매핑정보 CSV | 한국고용정보원, `한국고용정보원_직업능력_코드매핑정보_20251126.csv` | [`supplemental_data.py`](src/ncs_mcp/supplemental_data.py) | `ncs_occupation_code_mappings` | `context_only`, 추천 점수 미사용 | 원 파일별 출처와 이용조건 확인 필요 |
| 훈련과정 ZIP 데이터목록 CSV | 한국산업인력공단, `한국산업인력공단_훈련과정zip데이터목록_20260601.csv` | [`supplemental_data.py`](src/ncs_mcp/supplemental_data.py) | `ncs_external_training_zip_courses` | `context_only`, 추천 점수 미사용 | 다운로드 URL·이용조건 확인 전 재배포 금지 |

이 자료들은 canonical NCS 원천을 대체하지 않고 추천의 보조 근거로만 사용합니다.

## 방법론 참고문서

`2026년도 인사담당자 NCS 활용 실무 가이드`는 교육훈련체계 설계 단계와 검증 rubric을 정하는
`framework_reference`입니다. 샘플 과정명·조직명·행을 공식 훈련 데이터나 추천 점수로 승격하지
않습니다. 프로젝트 내 변환본은 [`docs/NCS_HRD_GUIDE_REFERENCE.md`](docs/NCS_HRD_GUIDE_REFERENCE.md)에
설명되어 있으며, 외부 공유 전 원 PDF의 배포 페이지와 개별 이용조건을 다시 확인해야 합니다.

## 레거시·참조 전용 원천

현재 공개 HRMCP의 기본 추천 경로는 SQF와 NCS 학습모듈에 의존하지 않습니다. 아래 코드는 과거
호환·조사·참조 목적으로 남아 있습니다.

| 원천 | 공식 레코드·사이트 | 코드 연결 | 저장 위치 | 현재 상태 | 이용조건 주의 |
| --- | --- | --- | --- | --- | --- |
| NCS 관련 정보 서비스 — CQ-Net | [공공데이터포털 15063879](https://www.data.go.kr/data/15063879/openapi.do), `https://c.q-net.or.kr/openapi/Ncs1info/ncsinfo.do` | [`collect_api.py`](src/ncs_mcp/collect_api.py)의 `ncs1info` 호환 경로 | `api_raw_responses`, `api_competency_units`, `competency_units`, `quality_issues` | 레거시 호환; 현재 snapshot의 CQ-Net raw response 없음 | 공식 레코드는 무료·`이용허락범위 제한 없음`; 현재 활성 제품 원천으로 표시하지 않음 |
| NCS 학습모듈정보 API | [공공데이터포털 15086442](https://www.data.go.kr/data/15086442/openapi.do), `https://apis.data.go.kr/B490007/ncsStudyModule/openapi21` | [`study_module_api.py`](src/ncs_mcp/study_module_api.py) | `ncs_learning_modules`, `learning_module_unit_links`, `learning_module_concept_links` | 레거시 | 포털 레코드는 `이용허락범위 제한 없음`으로 표시하지만 NCS 저작권정책은 학습모듈을 공공누리 제2유형·상업적 이용 불가로 명시하므로 더 엄격한 조건을 적용 |
| NCS 학습모듈 파일검색 | `https://www.ncs.go.kr/unity/th03/ncsModuleFileSearch.do` | [`ncs_learning_module_file_index.py`](scripts/ncs_learning_module_file_index.py) | HTML cache, JSONL index·summary; DB 직접 저장 없음 | 레거시 조사 | 문서 안의 제3자 그림·사진·삽화 권리까지 별도 확인 필요 |
| NCS 학습모듈 파일 다운로드 | `https://www.ncs.go.kr/unity/hth01/hth0101/downloadFile.do` | [`ncs_learning_module_pdf_download.py`](scripts/ncs_learning_module_pdf_download.py) | PDF, 정제된 header JSON, JSONL manifest; downloader 단계의 DB 저장 없음 | 레거시 조사 | 다운로드 파일별 이용조건과 제3자 권리를 확인해야 함 |
| SQF 분야·직종 API | [공공데이터포털 15134116](https://www.data.go.kr/data/15134116/openapi.do), `https://apis.data.go.kr/B490007/ncsSqfDuty/openapi26` | [`collect_api.py`](src/ncs_mcp/collect_api.py) | `api_raw_responses`, `sqf_duties` 및 후속 SQF mapping·evidence 테이블 | 레거시·참조 | API 레코드는 무료·`이용허락범위 제한 없음`; 활성 NCS 추천 점수에는 사용하지 않음 |
| SQF 자료실 게시물 | [NCS SQF 자료실](https://www.ncs.go.kr/sqf/sqf01/bbs_lib_list.do), 상세 `https://www.ncs.go.kr/sqf/sqf01/bbs_lib_view.do?libSeq=...` | [`collect_sqf_library.py`](src/ncs_mcp/collect_sqf_library.py) | `sqf_library_posts`, `sqf_library_files`, `sqf_document_sources` | 레거시·참조 | 게시물별 공공누리 및 제3자 권리 확인 필요 |
| SQF 자료실 첨부파일 | `https://www.ncs.go.kr/common/file/downloadFile.do` | [`collect_sqf_library.py`](src/ncs_mcp/collect_sqf_library.py), [`preprocess_sqf_documents.py`](src/ncs_mcp/preprocess_sqf_documents.py) | 원 파일과 `sqf_document_sources`, `sqf_document_assets`, `sqf_document_pages`, `sqf_document_chunks` | 레거시·참조 | 파일별 공공누리·제3자 권리 확인 및 원본 hash 보존 필요 |

`sqf_duties.duty_license`는 SQF 직무의 면허·자격 관련 업무 속성입니다. 저작권 또는 데이터
이용허락을 뜻하는 라이선스 메타데이터가 아니므로 출처 이용조건 판단에 사용하지 않습니다.

## 운영자 제공 문서 import

운영자는 NCS 기준문서 HTML·DOCX와 온톨로지 근거 PDF 등을 별도 import할 수 있습니다. 이 경로는
공식 API 자동수집과 다르며, 파일을 넣었다는 사실만으로 공개 재배포 권한이 생기지 않습니다.

| import 경로 | 코드 연결 | 저장 위치 | 공개 고지 원칙 |
| --- | --- | --- | --- |
| NCS 기준문서 HTML·DOCX | [`ncs_reference.py`](src/ncs_mcp/ncs_reference.py) | `ncs_reference_documents`, `ncs_reference_pages`, `ncs_reference_chunks` 및 선택적 entity·module 링크 | 개인 절대경로는 공개하지 않고 문서 제목·원 파일명·취득 URL·취득일·SHA-256·제품 역할을 기록 |
| 운영자 제공 온톨로지 근거 문서 | [`import_ontology_sources.py`](src/ncs_mcp/import_ontology_sources.py) | source/file metadata와 파생 asset·page·chunk | 원 권리자·이용조건 확인 전 공개 compact snapshot에 포함하지 않음 |

현재 로컬 DB에 문서가 있다는 사실과 Vercel compact snapshot에 그 문서·본문이 포함된다는 사실은
같지 않습니다. 릴리스 고지는 실제 snapshot manifest와 테이블 allowlist를 기준으로 작성합니다.

## 배포용 DB 전송 경로

`NCS_DB_URL`과 `NCS_SOURCE_DB_URL`은 새로운 의미론적 데이터 제공자가 아니라 운영자가 준비한 SQLite
snapshot을 빌드·배포 단계로 전달하는 HTTPS 전송 경로입니다. 허용 host를 제한하고, 원본 SHA-256,
취득 시각, 예상 크기, snapshot/build ID를 manifest에 기록해 검증한 뒤에만 사용합니다. URL에 인증
정보를 넣거나 로그에 남기지 않습니다.

- `NCS_DB_URL`: 함수 시작 시 사전 준비된 snapshot을 받아 `NCS_DB_PATH`의 읽기 전용 DB로 materialize할
  때 사용하는 선택 경로입니다.
- `NCS_SOURCE_DB_URL`: release workflow가 canonical source DB를 받아 compact snapshot Builder 입력으로
  사용할 때의 선택 경로입니다.

현재 `.env.example`의 `NCS_API_BASE_URL`은 설정 예시로 남아 있지만 실제 기준정보 수집기는 코드 상수를
사용합니다. 반대로 SQF·학습모듈 키 alias는 코드에서 인식해도 예제 env에 모두 드러나지 않습니다.
이 차이는 새 수집을 실행하기 전에 설정 문서와 구현을 맞춰야 하는 운영 점검 항목입니다.

## 출처 manifest에 보존할 최소 필드

새 DB 갱신부터 API·파일 원천별로 다음 provenance를 refresh manifest에 보존합니다.

- 제공기관과 공식 데이터셋명
- 공공데이터포털 레코드 ID 또는 공식 다운로드 페이지
- 실제 취득 URL과 operation
- 취득일시·원천 버전·기준일
- 원본 파일 또는 응답 묶음의 SHA-256과 크기
- 공공누리 유형 또는 공식 레코드의 이용허락 표시와 확인일
- active, context-only, framework, legacy 중 제품 사용 상태
- compact snapshot 포함 여부와 포함 테이블

기존 스키마가 source file/row, raw payload, fetched time, 일부 hash만 보존하는 경우가 있으므로, 필드가
없다고 권리 상태를 추정하지 않습니다. 배포 시에는 코드의 활성 여부뿐 아니라 실제 snapshot에 포함된
원천을 기준으로 고지합니다.

## NCS 누리집 저작권정책 적용

[NCS 누리집 저작권정책](https://www.ncs.go.kr/unity/th01/selectPolicyPopView.do)은 다음 원칙을
안내합니다.

- 한국산업인력공단이 저작재산권 전부를 보유하고 공공누리 제1유형으로 개방한 저작물은 구체적인
  출처표시 후 이용합니다.
- 학습모듈은 공공누리 제2유형으로 안내되므로 출처를 표시하고 상업적으로 이용하지 않습니다.
- 공단이 권리 전부를 보유하지 않은 자료는 단순 열람을 넘어 변경·복제·배포·개작하기 전에 권리자의
  허락을 확인합니다.

[공공데이터포털 이용정책](https://www.data.go.kr/ugs/selectPortalPolicyView.do)도 제3자 권리가 포함된
공공데이터는 권리자의 이용허락을 확보해야 하고, 저작물이 포함된 데이터는 공공누리 유형으로 범위를
표시하도록 안내합니다.

## 권장 출처표시

HRMCP 결과나 검토용 산출물에 원천 근거를 표시할 때는 최소한 다음 정보를 남깁니다.

> 자료: 한국산업인력공단 국가직무능력표준(NCS) 및 공공데이터포털의 해당 API 레코드  
> 취득·확인일: YYYY-MM-DD  
> 가공: HRMCP(원천을 정규화·연결한 비공식 검토용 산출물)  
> 공식 NCS 정의·자격 인정·채용 판정 결과가 아님

가능하면 API 명·공공데이터포털 레코드 ID·NCS 코드·버전·취득일·원본 해시를 함께 기록합니다.
한국산업인력공단 또는 공공데이터포털이 HRMCP를 보증하거나 승인한 것으로 표현하지 않습니다.

## 배포 전 운영자 체크리스트

1. 위 공식 레코드의 `이용허락범위`, 승인 단계, 트래픽 정책이 바뀌지 않았는지 확인합니다.
2. Excel·CSV·PDF·자료실 첨부파일은 개별 다운로드 페이지의 공공누리 유형을 확인합니다.
3. 학습모듈과 제3자 이미지·도표가 active DB나 공개 배포 패키지에 포함되지 않았는지 확인합니다.
4. source URL, 레코드 ID, 취득일, 원본 SHA-256을 refresh manifest에 보존합니다.
5. API 키·세션 쿠키·인증 헤더가 DB, 로그, 리포트, Git 이력에 포함되지 않았는지 확인합니다.
6. compact DB를 외부에 직접 배포할 때는 모든 포함 원천의 재배포 조건을 별도로 확인합니다.
