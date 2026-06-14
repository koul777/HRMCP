# NCS-SQF Ontology MCP

이 저장소는 NCS정보망 Excel DB, NCS 기준정보 API, SQF API, SQF 자료실 보고서를 하나의 SQLite 기반 지식베이스로 정규화하고 MCP 서버로 노출하는 프로젝트다.

목표는 단순 문서 검색이 아니다. 사용자가 원하는 업무를 물었을 때 관련 SQF 직무수준, NCS 능력단위, 능력단위요소, 수행준거, KSA, 교육훈련·학위·자격·현장경력 근거를 연결해 교육 추천과 역량 갭분석에 활용하는 것이다.

## 프로젝트 취지

`미래 교육 품질, NCS에서 길을 찾다.pdf`의 관점에 따르면 NCS는 학벌이나 연공서열이 아니라 직무능력과 성과 중심의 능력사회를 구현하기 위한 정책 수단이다. NCS는 교육훈련, 자격, 채용으로 확산되고, 궁극적으로 국가역량체계와 연결된다.

KQF와 SQF의 연결 취지는 다음과 같이 정리한다.

- KQF는 NCS 등을 바탕으로 학력, 자격, 현장경력, 교육훈련 이수 결과가 상호 연계될 수 있도록 하는 국가 수준 체계다.
- SQF는 산업별 현장에서 통용되는 직무를 도출·표준화하고, 직무수행에 필요한 능력을 구조화하는 산업 수준 체계다.
- SQF는 교육훈련, 학위, 자격, 현장경력을 산업별 직무와 수준에 장착하기 위한 골격이다.
- SQF 직무수준은 인사관리, 채용, 배치, 교육 추천, 경력경로 설계의 실무 단위로 본다.
- 이 프로젝트는 공식 인정 판정기가 아니라 근거 기반 추천·탐색·갭분석 보조 시스템이다.

## 현재 구축 상태

현재 `data/processed/ncs.db`에는 다음 레이어가 들어 있다.

```text
NCS Excel/API
  -> classifications
  -> competency_units
  -> competency_elements
  -> performance_criteria
  -> ksa_items

SQF API
  -> sqf_duties
  -> sqf_industry_sectors
  -> sqf_jobs_normalized
  -> sqf_levels
  -> sqf_job_levels_normalized
  -> sqf_recognition_evidence

SQF 자료실 보고서
  -> sqf_library_posts
  -> sqf_library_files
  -> sqf_document_sources
  -> sqf_document_assets
  -> sqf_document_pages
  -> sqf_document_chunks
  -> sqf_chunk_job_level_matches

NCS-SQF 연결
  -> sqf_ncs_matches
  -> MCP tools
  -> dashboard review
  -> JSON-LD export
```

대표 수치:

- NCS 능력단위: 13,435건
- NCS 능력단위요소: 47,620건
- 수행준거: 196,658건
- KSA: 574,279건
- SQF API 직무/직무수준 원천: 2,397건
- 정규화 SQF 직무수준: 2,397건
- SQF/온톨로지 원천 문서: 106건
- 추출 문서 자산: 126개, 모두 extracted
- PDF/OCR/HWP 기반 문서 청크: 9,108건
- 문서 청크와 SQF 직무수준 후보 근거: 49,940건
- SQF-NCS 후보 매핑: 22,642건

## 설치

```powershell
cd C:\workspace\NCS_MCP
python -m pip install -e .
Copy-Item .env.example .env
```

`.env`에는 실제 키를 넣는다. 키는 커밋하지 않는다.

```text
NCS_EXCEL_PATH=C:/workspace/NCS_MCP/data/raw/ncs_info_network_db_2026_02.xlsx
NCS_DB_PATH=C:/workspace/NCS_MCP/data/processed/ncs.db
NCS_SERVICE_KEY=your_data_go_kr_service_key
NCS_SQF_SERVICE_KEY=your_decoded_sqf_data_go_kr_service_key
```

## 핵심 명령

상태 확인:

```powershell
$env:PYTHONPATH="C:\workspace\NCS_MCP\src"
python scripts\ncs_harness.py inspect
```

검증:

```powershell
python -m unittest discover -s tests -v
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
python scripts\ncs_harness.py ontology validate
```

NCS 전처리:

```powershell
python scripts\ncs_harness.py pipeline --preprocess --reset --quality --smoke
python scripts\ncs_harness.py pipeline --api-standards --api-subd --smoke
python scripts\ncs_harness.py pipeline --api-elements-hr --smoke
```

SQF API 수집:

```powershell
python scripts\ncs_harness.py pipeline --api-sqf --sqf-major-code 02
```

SQF 자료실 수집과 문서 전처리:

```powershell
python scripts\ncs_harness.py collect-sqf-library --download --timeout 60
python scripts\ncs_harness.py build-sqf-sqlite-model
python scripts\ncs_harness.py preprocess-sqf-documents --ocr-empty --ocr-lang kor+eng --ocr-dpi 160
python scripts\ncs_harness.py build-sqf-precision-matches --min-score 9 --max-matches-per-chunk 8
```

증분 문서 처리:

```powershell
python scripts\ncs_harness.py preprocess-sqf-documents --only-unprocessed --ocr-empty
python scripts\ncs_harness.py build-sqf-precision-matches --asset-id 122
```

전체 SQF-NCS 후보 매핑:

```powershell
python scripts\ncs_harness.py build-sqf-mappings --all-sqf --duty-limit 5000 --limit-per-duty 10
```

JSON-LD export:

```powershell
python scripts\ncs_harness.py ontology export-jsonld --out exports\ncs_sqf_ontology.jsonld
```

Local ontology source import:

```powershell
$env:PDF_PATH=(Get-Item 'C:\Users\dd\Desktop\미래+교육+품질,+NCS에서+길을+찾다.pdf').FullName
python scripts\ncs_harness.py import-ontology-source --input "$env:PDF_PATH" --title "미래 교육 품질, NCS에서 길을 찾다" --role framework_reference
python scripts\ncs_harness.py preprocess-sqf-documents --only-unprocessed --ocr-empty --ocr-lang kor+eng --ocr-dpi 160
python scripts\ncs_harness.py ontology validate
```

This path is for policy or conceptual sources such as KQF/SQF purpose PDFs. The importer copies the file into `data/raw/ontology_sources`, registers it in `sqf_library_posts`, `sqf_library_files`, and `sqf_document_sources`, then the normal PDF/OCR/chunk pipeline can extract it as graph evidence.

대시보드:

```powershell
python scripts\ncs_dashboard.py --host 127.0.0.1 --port 8765
```

## MCP 실행

```powershell
python -m ncs_mcp.server
```

Claude Desktop 또는 MCP host 설정 예시:

```json
{
  "mcpServers": {
    "ncs-mcp": {
      "command": "python",
      "args": ["C:/workspace/NCS_MCP/src/ncs_mcp/server.py"],
      "env": {
        "NCS_DB_PATH": "C:/workspace/NCS_MCP/data/processed/ncs.db"
      }
    }
  }
}
```

## 주요 MCP 도구

NCS:

- `list_classifications`
- `get_competency_units`
- `get_unit_structure`
- `get_element_detail`
- `get_performance_criteria`
- `get_ksa`
- `search_ncs`

SQF/NCS-SQF:

- `search_sqf_jobs`
- `get_sqf_job_level`
- `map_sqf_to_ncs`
- `analyze_gap`
- `recommend_next_ncs_units`
- `recommend_education_for_duty`
- `explain_mapping`
- `get_sqf_ontology_summary`
- `search_sqf_document_chunks`
- `search_sqf_precision_matches`
- `get_sqf_ontology_job_level`

Resources/Prompts:

- `ontology://schema`
- `sqf://mvp/management-support`
- `sqf_gap_report_prompt`

## 온톨로지 모델

핵심 노드:

- `NCSCategory`
- `NCSCompetencyUnit`
- `NCSUnitElement`
- `PerformanceCriterion`
- `KSA`
- `SQFSector`
- `SQFJob`
- `SQFLevel`
- `SQFJobLevel`
- `RecognitionEvidence`
- `MappingCandidate`
- `DocumentEvidence`

핵심 관계:

- `NCSCategory -> NCSCompetencyUnit`
- `NCSCompetencyUnit -> NCSUnitElement`
- `NCSUnitElement -> PerformanceCriterion`
- `NCSUnitElement -> KSA`
- `SQFSector -> SQFJob`
- `SQFJob -> SQFJobLevel`
- `SQFJobLevel -> RecognitionEvidence`
- `SQFJobLevel -> MappingCandidate -> NCSCompetencyUnit`
- `SQFDocumentChunk -> DocumentEvidence -> SQFJobLevel`

매핑은 `sameAs`로 단정하지 않는다. 기본 관계는 `closeMatch`, `partiallyCovers`, `related`, `strongEvidence`, `supportingEvidence`로 둔다. `accepted`, `reviewed`, `human_reviewed`가 아닌 후보는 항상 `candidate`로 다룬다.

## 교육 추천 로직

SQF API의 `duty_education_training`, `duty_qualification`, `duty_career`, `duty_license`는 일부 직무에만 채워져 있다. 따라서 추천은 다음 순서로 만든다.

1. SQF 직무수준 직접 근거가 있으면 우선 사용한다.
2. 직접 근거가 부족하면 SQF-NCS 후보 매핑을 가져온다.
3. 연결된 NCS 능력단위의 요소, 수행준거, KSA를 학습목표로 변환한다.
4. SQF 보고서 청크 근거를 붙여 왜 이 직무수준과 연결되는지 설명한다.
5. 공식 인정 판정이 아니라 후보 기반 추천임을 명시한다.

예시:

```text
질문: 인사기획을 하려면 어떤 교육을 받아야 해?

해석:
NCS 인사기획(0202020101_23v3)은 SQF 경영관리 > 인사(6)에 closeMatch 후보로 연결된다.
SQF API의 교육훈련 필드는 비어 있으므로 NCS 인사기획의 능력단위요소, 수행준거, KSA를 학습목표로 변환한다.
보고서 근거는 2022년 SQF 개발 최종보고서(인사조직, 재무, 회계 분야)의 인사(6) 직무역량체계 청크를 사용한다.
```

## 데이터 정책

- `.env`와 API 키는 커밋하지 않는다.
- 원천 Excel과 생성 SQLite DB는 Git LFS로 관리한다.
- 원문 필드는 수정하지 않는다.
- 사람 또는 LLM이 보정한 값은 refined 계열 필드에 저장한다.
- 추천/갭분석은 근거 기반 보조 정보이며 공식 인정·자격 부여가 아니다.

## 관련 문서

- `AGENTS.md`
- `ARCHITECTURE.md`
- `docs/HARNESS_ENGINEERING.md`
- `docs/NCS_MCP_PRD.md`
- `docs/NCS_SQF_PROJECT_SYSTEM.md`
- `docs/NCS_SQF_ONTOLOGY.md`
- `docs/SQF_SQLITE_ONTOLOGY_SYSTEM.md`
- `docs/CHATGPT_PRO_PROGRAM_BRIEF.md`
