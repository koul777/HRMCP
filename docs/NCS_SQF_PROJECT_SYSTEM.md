# NCS-SQF Ontology MCP Project System

이 프로젝트는 NCS Excel/API와 SQF API를 SQLite 지식베이스로 정규화하고, MCP 서버와 대시보드에서 교육 추천, 역량 갭분석, 후보 매핑 검토를 제공한다.

## 제품 경계

- 공식 SQF 수준 인정, 자격 부여, 평가 판정은 하지 않는다.
- 모든 추천과 갭분석은 근거 기반 보조 정보다.
- 원문 데이터는 수정하지 않고, 보정값은 refined 필드와 review workflow에만 저장한다.

## 전체 파이프라인

```text
NCS Excel/API + SQF API
  -> SQLite 정규화
  -> quality_issues 진단
  -> refinement_jobs 정제 큐
  -> SQF-NCS 후보 매핑 생성
  -> mapping quality gate
  -> 교육 추천 / 갭분석 / MCP
  -> dashboard human review
  -> evaluation report
```

## 기본 정책

매핑 필터 기본값:

```text
score >= 7
relation != related
review_status != rejected
```

기본 추천/갭분석에는 다음 매핑을 사용한다.

```text
accepted
reviewed
human_reviewed
candidate 중 score/relation 필터를 통과한 후보
```

정제 데이터 기본 조회 정책:

```text
refined_if_approved
```

`model_refined`는 기본 MCP 조회에는 사용하지 않고, 검토/비교 화면에서 확인한다.

## 범위 전략

1차 MVP:

```text
SQF 02 경영·회계·사무 > 경영관리 > 경영지원
NCS 02 경영·회계·사무
```

확장 범위:

```text
business_accounting_office_02
```

02 대분류 전체로 확장하되, 품질 게이트를 통과하지 못한 후보는 기본 추천/갭분석에 포함하지 않는다.

현재 기준선:

```text
SQF API 원천 직무수준: 2,397건
정규화 SQF 직무수준: 2,397건
SQF 자료실 문서: 105건
PDF/OCR/HWP 추출 자산: 125개 extracted
문서 청크: 9,105건
문서 청크-SQF 직무수준 근거 후보: 49,940건
SQF-NCS 후보 매핑: 22,642건
eligible closeMatch: 5,131건
eligible partiallyCovers: 11,298건
excluded related: 6,213건
SQF 직접 교육훈련 근거는 일부 직무에만 존재하므로 NCS KSA 기반 보완 추천이 기본 정책이다.
```

## 주요 명령

```powershell
python scripts\ncs_harness.py inspect
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
python scripts\ncs_harness.py build-sqf-mappings --all-sqf --major-code 02
python scripts\ncs_harness.py evaluate --scope-tag business_accounting_office_02
```

정제 JSONL 왕복:

```powershell
python scripts\ncs_harness.py refine export-jsonl --issue-types short_ksa,duplicate_text --limit 100 --out data/refinement/export.jsonl
python scripts\ncs_harness.py refine import-jsonl --input data/refinement/results.jsonl
```

## MCP 응답 원칙

추천/갭분석 도구는 다음 metadata를 포함한다.

```text
data_source
query_scope
mapping_filter
used_refined_policy
used_mapping_count
excluded_mapping_count
exclusion_reasons
candidate_based
caveats
```

SQF 직접 교육훈련, 자격, 경력 필드가 비어 있으면 NCS 능력단위, 능력단위요소, 수행준거, KSA를 학습목표로 변환한다. KSA는 단독 추천하지 않고 수행준거와 함께 제시한다.

## 안정화 기준

- 낮은 점수, `related`, `rejected` 매핑이 기본 추천에 섞이지 않는다.
- SQF 직접 교육훈련 필드가 비어 있어도 NCS-derived 학습목표가 생성된다.
- JSONL import는 원문을 덮어쓰지 않고 `refinement_jobs`에만 저장한다.
- review 상태 변경은 audit log에 남길 수 있는 스키마를 갖춘다.
- evaluation run은 DB에 저장되어 회귀 기준선으로 쓸 수 있다.
## SQF Library Reports As Ontology Sources

SQF API fields can be sparse, so the SQF library reports must be collected as an evidence layer for ontology work. The report layer is separate from `sqf_duties` because it is document evidence, not normalized job-level API rows.

```text
NCS SQF library pages
  -> sqf_library_posts
  -> sqf_library_files
  -> data/raw/sqf_docs/*
  -> sqf_document_sources
  -> future PDF/HWP text extraction
  -> ontology evidence and mapping review
```

Collect metadata only:

```powershell
python scripts\ncs_harness.py collect-sqf-library --start-page 0 --end-page 10
```

Collect metadata and download all attachments into one folder:

```powershell
python scripts\ncs_harness.py collect-sqf-library --start-page 0 --end-page 10 --download --timeout 60
```

Pipeline form:

```powershell
python scripts\ncs_harness.py pipeline --collect-sqf-library --download-sqf-library --timeout 60
```

The download endpoint is `/common/file/downloadFile.do` with POST parameters `sysDstinCd`, `fileMstky`, `filedetlSeq`, and `downlDstinCd`. Downloaded files are stored under `data/raw/sqf_docs` and should not be committed.
