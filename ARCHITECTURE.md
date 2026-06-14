# NCS-SQF Ontology MCP 아키텍처

## 목적

이 프로젝트는 NCS정보망 Excel DB, NCS 기준정보 API, SQF API, SQF 자료실 보고서를 SQLite 지식베이스로 정규화하고 MCP 서버를 통해 AI가 사용할 수 있게 만든다.

핵심은 PDF 문장 검색이 아니라 다음 관계를 구조적으로 조회하는 것이다.

```text
SQF 산업분야
  -> SQF 직무
  -> SQF 직무수준
  -> SQF 직무역량/인정근거
  -> NCS 능력단위
  -> 능력단위요소
  -> 수행준거
  -> KSA
  -> 교육 추천 / 갭분석 / 근거 설명
```

## 정책적 해석

`미래 교육 품질, NCS에서 길을 찾다.pdf`의 관점은 이 아키텍처의 설계 기준이다.

- NCS는 교육훈련, 자격, 채용으로 확산되어 직무능력 중심의 능력사회를 지원한다.
- KQF는 NCS 등을 바탕으로 학력, 자격, 현장경력, 교육훈련 이수 결과가 상호 연계될 수 있도록 하는 국가 수준 체계다.
- SQF는 산업별 현장에서 통용되는 직무를 도출·표준화하고, 직무수행에 필요한 능력을 구조화하는 산업 수준 체계다.
- SQF는 교육훈련-학위-자격-현장경력을 산업별 직무와 수준에 장착하기 위한 골격이다.
- 따라서 이 프로젝트의 추천은 공식 인정 판정이 아니라 직무역량 탐색과 교육품질 개선을 위한 근거 기반 보조 판단이다.

## 데이터 소스

- `data/raw/ncs_info_network_db_2026_02.xlsx`: NCS 계층, 능력단위, 능력단위요소, 수행준거, KSA 원천.
- NCS API `/NCS005`: 능력단위명, 수준, 정의 검증·보강.
- NCS API `/NCS004`: 세분류/직무 정의 보강.
- NCS API `/NCS006`: 능력단위요소명과 요소수준 검증.
- SQF API `/openapi26`: 산업분야, 직무, 직무수준, 직무정의, 교육훈련, 자격, 경력 등.
- SQF 자료실: 직무역량체계도, 개발 매뉴얼, 연구보고서 PDF/HWP/ZIP.

## 처리 파이프라인

```text
NCS Excel
  -> preprocess_excel.py
  -> classifications / competency_units / competency_elements / performance_criteria / ksa_items

NCS API
  -> collect_api.py
  -> api_competency_units / classifications API 보강 / element API 보강

SQF API
  -> collect_api.py --mode sqf
  -> sqf_duties
  -> sqf_sqlite.py
  -> sqf_industry_sectors / sqf_jobs_normalized / sqf_levels / sqf_job_levels_normalized

SQF 자료실
  -> collect_sqf_library.py
  -> sqf_library_posts / sqf_library_files / sqf_document_sources
  -> preprocess_sqf_documents.py
  -> sqf_document_assets / sqf_document_pages / sqf_document_chunks
  -> sqf_precision_matching.py
  -> sqf_chunk_job_level_matches

NCS-SQF 연결
  -> ontology.py
  -> sqf_ncs_matches
  -> mapping_policy.py
  -> recommend / gap / explain

외부 사용
  -> server.py MCP tools/resources/prompts
  -> ncs_dashboard.py human review
  -> ontology_export.py JSON-LD export / readiness validation
```

## 핵심 스키마

NCS:

- `classifications`: 대·중·소·세분류 코드와 API 직무정의.
- `competency_units`: 능력단위 원문 정보와 API 정의.
- `competency_elements`: 능력단위요소 원문 정보와 API 검증 상태.
- `performance_criteria`: 수행준거 원문/정제본.
- `ksa_items`: KSA 원문/정제본.
- `element_criteria_ksa_links`: Excel 원행의 수행준거-KSA 조합 보존.

SQF:

- `sqf_duties`: SQF API 원천 직무수준 행.
- `sqf_industry_sectors`: SQF 산업/분야 노드.
- `sqf_jobs_normalized`: SQF 직무 노드.
- `sqf_levels`: SQF 수준 노드.
- `sqf_job_levels_normalized`: 추천과 갭분석의 핵심 단위.
- `sqf_recognition_evidence`: 교육훈련, 학위, 자격, 경력, 면허, 비고 등 직접 근거.

문서 근거:

- `sqf_library_posts`: SQF 자료실 게시글.
- `sqf_library_files`: 다운로드 첨부파일.
- `sqf_document_sources`: 온톨로지 원천 문서.
- `sqf_document_assets`: PDF/HWP/ZIP 내부 파일.
- `sqf_document_pages`: 페이지 또는 섹션 단위 추출 텍스트.
- `sqf_document_chunks`: RAG와 근거 매칭용 청크.
- `sqf_chunk_job_level_matches`: 청크와 SQF 직무수준 간 후보 근거.

매핑:

- `sqf_ncs_matches`: SQF 직무수준과 NCS 능력단위 간 후보 매핑 객체.
- `review_audit_log`: 사람 검토 이력.
- `evaluation_runs`: 매핑/추천 품질 평가 결과.
- `refinement_jobs`: LLM 또는 수작업 정제 큐.

## 모델링 원칙

- 수행준거와 KSA는 모두 능력단위요소에 귀속된다.
- SQF 직무수준은 인사관리, 추천, 갭분석의 기본 단위다.
- SQF API의 교육훈련/자격/경력 필드가 비어 있으면 NCS 능력단위와 KSA를 학습목표로 변환한다.
- NCS-SQF 연결은 `sameAs`로 단정하지 않는다.
- 기본 관계는 `closeMatch`, `partiallyCovers`, `related`, `strongEvidence`, `supportingEvidence`를 사용한다.
- 모든 후보 매핑은 `score`, `method`, `evidence_text`, `review_status`, `filter_status`를 보존한다.
- `candidate`는 공식 인정이 아니며, 대시보드에서 사람 검토 후 `accepted`, `reviewed`, `human_reviewed`, `rejected`로 바꾼다.

## 불변조건

- Excel 원문은 삭제하거나 덮어쓰지 않는다.
- API 데이터는 보강·검증용이며 원천 데이터를 무비판적으로 대체하지 않는다.
- 원문 필드와 refined 필드는 분리한다.
- API 키는 `.env`에만 두고 출력하거나 커밋하지 않는다.
- 생성 DB와 OCR 모델은 Git LFS로 관리한다.
- 추천 결과는 공식 인정·자격 부여·평가 판정으로 표현하지 않는다.

## 검증 루프

```powershell
$env:PYTHONPATH="C:\workspace\NCS_MCP\src"
python -m unittest discover -s tests -v
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
python scripts\ncs_harness.py ontology validate
```

JSON-LD 산출:

```powershell
python scripts\ncs_harness.py ontology export-jsonld --out exports\ncs_sqf_ontology.jsonld
```

완료 기준:

- `sqf_document_assets`의 모든 자산이 `extracted`.
- `sqf_duties`와 `sqf_job_levels_normalized`의 행 수가 일치.
- 전체 SQF 범위에 대해 `sqf_ncs_matches` 후보가 생성.
- SQF 자료실 보고서 기반 `sqf_chunk_job_level_matches` 후보가 생성.
- MCP 추천/갭분석 도구가 샘플 질의에 응답.
- JSON-LD export가 생성.
