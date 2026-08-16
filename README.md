# HRMCP

HRMCP (NCS-based HR MCP) normalizes Korean National Competency Standards (NCS)
source data and public API responses into a SQLite knowledge graph, then exposes
that graph through MCP tools for HR ontology search, job-description
(채용 직무 설명자료) drafting, and education/training recommendations.

> Naming note: "HRMCP" is the product/display name and MCP connection label.
> Internal Python identifiers (the `ncs_mcp` package, `ncs_*` tool names) are
> unchanged for compatibility.

The active product scope is NCS-centered. SQF and NCS learning-module flows may
remain in historical tables or compatibility code, but they are legacy/reference
surfaces unless an operator explicitly reactivates them.

Recommendations are education-planning guidance, not official qualification,
licensing, hiring, legal, or compliance decisions.

## Publication Status

This repository can be published as a private or draft developer preview when
lint, smoke, unit tests, dashboard verification, and release-readiness evidence
are current. Do not describe it as a stable public release until
`release_ready=true` in the active release-readiness report.

CI enforces encoding, unit, lint, and smoke checks. The source-boundary audit is
available as a `workflow_dispatch` deployment gate and must be run with
`enforce_source_boundary=true` before pushing or sharing a source-only preview
branch. Dashboard verification and release-readiness evidence are manual
release gates that must be regenerated and attached or linked from the private
preview note before sharing the preview.

Known non-preview blockers must be disclosed in preview notes: pending human
review for ontology concepts, training-goal links, and task-KSA relations;
qualification collection coverage below the release target; and any provenance
reconfirmation packet that still requires a human decision.

## What It Does

- Preserves NCS hierarchy, competency units, elements, performance criteria, and
  raw KSA rows from source files.
- Builds KSA/task ontology tables without overwriting source KSA text.
- Links training-course goals, hours, methods, facilities, and NCS unit evidence
  to task/KSA recommendation evidence.
- Supports career-transition and task-based training recommendations with
  compact evidence summaries.
- Adds supporting evidence from NCS career paths, qualification-item APIs, and
  job-base competency APIs.
- Exposes a small MCP tool surface for NCS structure search, ontology lookup,
  training-course search, and AI-HR education-path planning.
- Includes a separate read-only institutional chat reference UI/API that routes
  natural-language requests to public tools and blocks operator workflows.

## Data Flow

```text
NCS Excel/source data
  -> classifications
  -> competency_units
  -> competency_elements
  -> performance_criteria
  -> ksa_items

KSA ontology preprocessing
  -> ksa_atomic_items
  -> ontology_concepts
  -> ksa_concept_links
  -> criteria_concept_links
  -> task_ksa_concept_relations
  -> task_similarity_links

Training-course API
  -> ncs_training_courses
  -> ncs_training_course_unit_links
  -> ncs_training_course_concept_links
  -> ncs_training_course_element_links
  -> training_goal_concept_links
  -> training_delivery_relations

MCP / harness
  -> query routing
  -> KSA gap and transfer analysis
  -> training recommendation with evidence
```

## Quick Start In 5 Steps

Run commands from the repository root.

1. Create and activate a Python 3.11+ environment.

```powershell
cd C:\workspace\NCS_MCP
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

2. Install the package in editable mode.

```powershell
python -m pip install -e .
```

3. Create local configuration.

```powershell
Copy-Item .env.example .env
```

Edit `.env` so paths point to your local NCS source file and generated SQLite
database:

```text
NCS_EXCEL_PATH=C:/workspace/NCS_MCP/data/raw/ncs_info_network_db_2026_02.xlsx
NCS_DB_PATH=C:/workspace/NCS_MCP/data/processed/ncs.db
NCS_SERVICE_KEY=<your_data_go_kr_service_key>
NCS_TRAINING_COURSE_SERVICE_KEY=<your_ncs_training_course_data_go_kr_service_key>
NCS_QUALIFICATION_SERVICE_KEY=<your_ncs_qualification_item_data_go_kr_service_key>
NCS_JOB_BASE_SERVICE_KEY=<your_ncs_job_base_competency_data_go_kr_service_key>
NCS_MCP_ENABLE_OPERATOR_TOOLS=0
NCS_MCP_READ_ONLY=1
NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS=2
NCS_MCP_RECOMMENDATION_QUEUE_TIMEOUT_SECONDS=30
NCS_API_BASE_URL=https://apis.data.go.kr/B490007/hrdkapi
NCS_API_TIMEOUT_SECONDS=30
```

4. Provide source data or a prepared SQLite DB.

Large source files and generated DBs are not part of the normal source package.
For a local run, place your NCS Excel/source files under `data/raw` and generate
`data/processed/ncs.db`, or mount/copy a prepared DB to the path configured by
`NCS_DB_PATH`. For a GitHub developer preview, keep generated SQLite databases
out of the source commit; if a prepared DB is needed, publish it as a controlled
private LFS/artifact handoff with the retrieval path documented in the preview
note.

Common preprocessing commands:

```powershell
$env:PYTHONPATH="C:\workspace\NCS_MCP\src"
python scripts\ncs_harness.py inspect
python scripts\ncs_harness.py preprocess-ncs-ontology --atomic-ksa
python scripts\ncs_harness.py preprocess-ncs-ontology --task-ksa-relations
python scripts\ncs_harness.py preprocess-ncs-ontology --task-similarity
python scripts\ncs_harness.py preprocess-ncs-ontology --training-course-links
```

5. Verify and run the MCP server.

```powershell
$env:PYTHONPATH="C:\workspace\NCS_MCP\src"
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
python -m unittest discover -s tests -v
python scripts\ncs_harness.py ontology validate
python scripts\benchmark_chatbot_readiness.py --db data\processed\ncs.db --out reports\institutional_chatbot_readiness_benchmark.json --markdown-out reports\institutional_chatbot_readiness_benchmark.md --current-query "HR manager" --target-query "HR planning"
```

STDIO mode:

```powershell
.\run_ncs_mcp_stdio.cmd
```

The STDIO launcher respects an existing `NCS_DB_PATH`; set it to a separately
mounted or handed-off SQLite DB when the generated DB is not inside the checkout.

HTTP mode:

```powershell
.\run_ncs_mcp_http.cmd
```

Default HTTP endpoints:

- MCP: `http://127.0.0.1:8766/mcp`
- Health: `http://127.0.0.1:8766/health`
- Readiness: `http://127.0.0.1:8766/ready`

The launchers default to read-only SQLite serving and suppress the operator MCP
surface. A hardened loopback-only container example is available at
`deploy/compose.internal.yml`; identity, TLS, and user-level authorization
still belong at the institution gateway described in
`docs/INSTITUTIONAL_CHATBOT_SELF_HOST_GUIDE.md`.
Non-loopback HTTP binding is rejected unless `--allow-remote-bind` is supplied
directly, or `NCS_MCP_ALLOW_REMOTE_BIND=1` is set for the Windows HTTP launcher.
That explicit opt-in does not provide authentication or TLS.

### Vercel MCP/ChatGPT quick run

Vercel has now been wired as a serverless Streamable-HTTP surface, so ChatGPT 연결은
주소 한 줄(`/api/mcp`)만 넣으면 됩니다. 전체 배포 가이드는
`docs/README_VERCEL_HTTPS.md` 를 참고하세요.

- `vercel.json` defines the function entrypoint (`api/index.py`).
- `api/mcp.py` exports `app` (ASGI) from `ncs_mcp.server` and bootstraps the
  serving DB.
- The serving DB (~117 MB) is **not committed**; it is published as a GitHub
  Release asset and fetched at runtime via `NCS_DB_URL`.

1. Prepare serving DB (**GitHub Release 방식, 권장**):
   - 릴리스: <https://github.com/koul777/NCS_MCP/releases/tag/ncs-serving-2026-02>
   - 자산 다운로드 URL을 `NCS_DB_URL`로 지정하면 배포 런타임에서 자동 다운로드:

     ```text
     https://github.com/koul777/NCS_MCP/releases/download/ncs-serving-2026-02/ncs_interview_serving_release.db
     ```

   - 새 슬라이스를 만들려면 `scripts/export_interview_serving_db.py` 로 export 후
     `gh release create ...` 로 자산을 올립니다 (`docs/README_VERCEL_HTTPS.md` 3~4단계).
2. Set read-only, public MCP defaults in Vercel environment
   (`NCS_DB_URL` 만 추가로 등록):

```text
NCS_MCP_READ_ONLY=1
NCS_MCP_ENABLE_OPERATOR_TOOLS=0
NCS_MCP_DISABLE_DNS_REBINDING_PROTECTION=1
NCS_MCP_STREAMABLE_HTTP_PATH=/mcp
NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS=2
NCS_DB_PATH=/tmp/ncs_interview_serving.db
NCS_DB_URL=https://github.com/koul777/NCS_MCP/releases/download/ncs-serving-2026-02/ncs_interview_serving_release.db
```

위 기본값 중 `NCS_DB_URL` 외에는 `vercel.json` 에 이미 포함돼 있습니다.

```powershell
vercel env add NCS_DB_URL production
# 위 Release 자산 URL 붙여넣기
```

3. Deploy via Vercel.

```powershell
vercel deploy --prod
```

4. Connect ChatGPT (or any remote MCP client) to this single MCP URL:

```text
https://<your-vercel-domain>/api/mcp
```

ChatGPT/Custom GPT 에서는 아래 JSON의 `url`만 넣으면 바로 등록됩니다.

For ChatGPT Custom GPT (Agent/Tools config), a minimum config payload is:

```json
{
  "mcpServers": {
    "hrmcp": {
      "url": "https://<your-vercel-domain>/api/mcp"
    }
  }
}
```

Health/ready checks:

```text
https://<your-vercel-domain>/api/health
https://<your-vercel-domain>/api/ready
```

Reference chat mode:

```powershell
.\run_ncs_institutional_chat.cmd
```

- Chat UI: `http://127.0.0.1:8780/`
- Chat API: `http://127.0.0.1:8780/api/chat`
- Readiness: `http://127.0.0.1:8780/ready`

The local reference chat requires read-only mode and disabled operator tools.
For an institution gateway deployment, configure authenticated identity/group
headers, Origin enforcement, and pseudonymous audit logging as documented in
`docs/INSTITUTIONAL_CHATBOT_SELF_HOST_GUIDE.md`. The checked-in integration
evidence template remains unverified until the institution supplies target-host
SSO, TLS, privacy, backup, and operations evidence.

## API Key Issuance

The NCS API keys are issued outside this repository. Use the Korean public data
portal that hosts the HRDK/NCS APIs, apply for access to the required services,
and copy the issued service keys into your local `.env`.

Required or commonly used keys:

- `NCS_SERVICE_KEY`: NCS reference API key.
- `NCS_TRAINING_COURSE_SERVICE_KEY`: NCS training-course API key.
- `NCS_QUALIFICATION_SERVICE_KEY`: NCS unit qualification-item API key.
- `NCS_JOB_BASE_SERVICE_KEY`: NCS job-base competency API key.

Operational notes:

- Do not commit `.env`.
- Do not paste real keys into reports, logs, issues, or screenshots.
- Keep key values out of `/health`, `/ready`, and MCP responses; only key
  presence booleans are safe to expose.
- Some broad collection jobs are rate limited. Use guarded batch commands with
  request delays, retry limits, and `--stop-after-rate-limit-errors`.

## Collection Scope

Production collection and preprocessing should cover the full NCS scope, not a
single major code. Code `02` is acceptable only for examples, smoke tests, and
API connectivity checks.

```powershell
python scripts\ncs_harness.py collect-training-courses --all-majors --num-of-rows 500
python scripts\ncs_harness.py collect-job-base --all-majors --num-of-rows 500
python scripts\ncs_harness.py qualification-retry-hygiene --ncs006-checkpoint-path reports\checkpoint_ncs006_element_api_status_<DATE>_current.json --out reports\qualification_retry_hygiene_<DATE>.json --markdown-out reports\qualification_retry_hygiene_<DATE>.md
python scripts\ncs_harness.py qualification-coverage-plan --target-ratio 0.9 --batch-size 100 --ncs006-checkpoint-path reports\checkpoint_ncs006_element_api_status_<DATE>_current.json --out reports\qualification_collection_coverage_plan_<DATE>.json --markdown-out reports\qualification_collection_coverage_plan_<DATE>.md --csv-out reports\qualification_collection_coverage_plan_<DATE>.csv
python scripts\ncs_harness.py agent-queue-status --queue reports\aihr_agent_queue_<DATE>.json --out reports\aihr_agent_queue_status_<DATE>.json --markdown-out reports\aihr_agent_queue_status_<DATE>.md
python scripts\ncs_harness.py collect-qualification-items --all-units --limit-units 100 --num-of-rows 50 --max-pages 1 --request-delay 2 --max-retries 1 --retry-backoff-seconds 30 --stop-after-rate-limit-errors 3 --ncs006-checkpoint-path reports\checkpoint_ncs006_element_api_status_<DATE>_current.json
```

Qualification collection records unit-level status and is designed to resume.
Run the final collection command only after the queue status shows the guarded
item is operator-ready and there are no safety violations. Use `--refresh` only
when deliberately recollecting completed or empty units.

## Example Recommendation Commands

```powershell
python scripts\ncs_harness.py recommend-training-transition --current-query "labor management" --target-query "HR planning" --limit 5 --compact --no-save
python scripts\ncs_harness.py recommend-training-for-task --query "recruitment" --limit 5 --compact --no-save
python scripts\ncs_harness.py route-ncs-query "labor management to HR planning education path"
```

Recommendation ranking distinguishes stronger direct training-goal evidence from
weaker token, element-implied, or inherited evidence. Broad/generic KSA links are
down-weighted so they do not dominate specialized recommendations.

## 직무기술서(NCS 기반 채용 직무 설명자료) 생성 방법

채용 공고문을 입력하면, 이 MCP를 통해 NCS 근거로 교차 확인된 **채용 직무
설명자료(직무기술서)** 를 표 형식으로 만들고 워드(.docx) 파일로 저장할 수
있습니다. 별도의 "생성 전용" 도구가 있는 것이 아니라, 아래 읽기 도구들이
반환한 NCS 근거를 정해진 템플릿에 채워 넣는 방식입니다.

### 1) 연결

먼저 이 MCP를 ChatGPT(또는 다른 MCP 클라이언트)에 연결합니다.

- Vercel HTTPS: `https://<your-vercel-domain>/api/mcp` (위 Vercel 섹션 참고)
- 로컬 HTTP: `http://127.0.0.1:8766/mcp`

### 2) 프롬프트

공고문(PDF/텍스트)과 아래 지시를 함께 전달합니다.

```text
HRMCP 공고문을 보고 직무기술서를 만들어줘.
아래 예시와 동일한 표 형식으로, NCS 분류체계 → 능력단위 → 직무수행내용
→ 필요지식 → 필요기술 → 직무수행태도 → 필요자격 → 직업기초능력 →
참고사이트 순서로 2페이지 표를 만들고 워드 파일로 저장해줘.
```

### 3) 출력 템플릿

`[NCS 기반 채용 직무 설명자료 : <채용분야>]` 제목의 2페이지 표.

| 항목 | 내용 | 채우는 NCS 근거 / 도구 |
|------|------|------------------------|
| 채용분야 | 공고의 직무명 | 공고문 + `ncs_search` |
| 분류체계 | 대분류·중분류·소분류·세분류 | `ncs_search`(classifications) |
| 능력단위 | 관련 NCS 능력단위 목록 | `ncs_search` scope=`unit`, `ncs_unit_detail` |
| 직무수행내용 | 능력단위요소(수행 업무) | `ncs_unit_detail` include=`elements`(+`criteria`) |
| 필요지식(K) | 요구 지식 | `ncs_unit_detail` include=`ksa` → K 항목 |
| 필요기술(S) | 요구 기술 | `ncs_unit_detail` include=`ksa` → S 항목 |
| 직무수행태도(A) | 요구 태도 | `ncs_unit_detail` include=`ksa` → A 항목 |
| 필요자격 | 관련 국가자격 | `ncs_analysis` mode=`qualification` |
| 직업기초능력 | NCS 10대 직업기초능력 | 능력단위 근거 + 표준 직업기초능력 |
| 참고사이트 | 근거 출처 | NCS(ncs.go.kr), Work24 등 |

각 능력단위요소의 지식/기술/태도는 원문(raw KSA)을 보존해 채우고, 자격·훈련
근거는 `ncs_analysis`(qualification) 및 `ncs_unit_detail` include=`qualification`
/`training` 으로 교차 확인합니다.

### 4) 워드 파일 저장

클라이언트에게 위 표를 `NCS_기반_채용_직무_설명자료_<채용분야>.docx` 형식의
워드 파일로 저장하도록 지시하면 됩니다. (예: `기능직(전기)` → 채용분야
`기능직(전기)`, 대분류 `19.전기·전자` 등으로 채워진 2페이지 표.)

추천 결과·직무기술서는 교육/채용 실무 참고 자료이며, 공식 자격·채용·법적
판단을 대체하지 않습니다.

## MCP Tool Surface

The active public surface focuses on read-heavy NCS search and recommendation:

- NCS structure and ontology lookup.
- Training-course search and detail.
- Task-based and transition-based training recommendations.
- AI-HR education-path planning with `query_route`, `recommended_path`,
  `training_system_matrix`, and guide-trace evidence.

Operator and review tools are not part of the default public execution surface.
Recommendation tools executed through meta execution must use no-save behavior.

## Release And Operations References

- Architecture: `ARCHITECTURE.md`
- Vercel HTTPS deployment guide: `docs/README_VERCEL_HTTPS.md`
- Interview serving DB policy: `docs/INTERVIEW_SERVING_DB.md`
- Harness and validation: `docs/HARNESS_ENGINEERING.md`
- MCP release checklist: `docs/MCP_RELEASE_CHECKLIST.md`
- Productization strategy: `docs/AIHR_PRODUCTIZATION_STRATEGY.md`
- Deployment runbook: `docs/AIHR_DEPLOYMENT_RUNBOOK.md`
- Institutional chatbot self-host guide: `docs/INSTITUTIONAL_CHATBOT_SELF_HOST_GUIDE.md`

The generated SQLite graph is a local prepared knowledge graph. Current full
builds can be large, so Docker/internal deployments should mount
`data/processed` as an external volume rather than copying DB files into an
image.
