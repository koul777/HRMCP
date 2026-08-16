# NCS MCP Release Checklist

이 문서는 NCS 기반 훈련 추천 MCP를 로컬, Docker, 또는 외부 MCP 클라이언트에
등록하기 전에 확인할 항목이다.

## 1. 비밀값과 데이터 경계

- `.env`는 커밋하지 않는다.
- API 키 값은 로그, 리포트, `/health`, `/ready`, MCP 응답에 출력하지 않는다.
- 원천 CSV/Excel, 생성 SQLite DB, 대용량 리포트는 Docker 이미지에 넣지 않는다.
- Docker 실행 시 DB는 volume으로 마운트한다.

필수 또는 선택 키:

- `NCS_SERVICE_KEY`
- `NCS_TRAINING_COURSE_SERVICE_KEY`
- `NCS_QUALIFICATION_SERVICE_KEY`
- `NCS_JOB_BASE_SERVICE_KEY`

키 존재 여부만 확인:

```powershell
python scripts\mcp_http_health_smoke.py --timeout 20
```

## 2. 로컬 검증

저장소 루트에서 실행한다.

```powershell
python -m py_compile src\ncs_mcp\server.py src\ncs_mcp\tool_registry.py src\ncs_mcp\error_codes.py src\ncs_mcp\helpers.py scripts\ncs_harness.py scripts\mcp_stdio_smoke.py scripts\mcp_http_health_smoke.py scripts\export_mcp_tool_contract.py
python -m unittest tests.test_http_client tests.test_training_recommendation tests.test_ncs_mcp tests.test_harness -v
python scripts\ncs_harness.py lint
python scripts\export_mcp_tool_contract.py --check --out mcp\ncs-tool-contract.json
python scripts\mcp_stdio_smoke.py --timeout 15
python scripts\mcp_http_health_smoke.py --timeout 20
python scripts\ncs_harness.py smoke
```

온톨로지/추천 근거를 바꿨다면 추가로 실행한다.

```powershell
python scripts\ncs_harness.py ontology validate
python scripts\ncs_harness.py quality-gates --include-transition-eval --transition-limit 5
```

## 3. HTTP 실행

```powershell
.\run_ncs_mcp_http.cmd
```

기본 엔드포인트:

- MCP: `http://127.0.0.1:8766/mcp`
- health: `http://127.0.0.1:8766/health`
- readiness: `http://127.0.0.1:8766/ready`

`/health`와 `/ready`는 다음만 노출한다.

- MCP tool count
- legacy tool exposure count
- DB configured/exists/openable/ready boolean and core table row counts
- API key presence boolean

`/health`는 프로세스 liveness 메타데이터를 반환하고 DB가 준비되지 않으면
`status=degraded`를 반환한다. `/ready`는 DB가 없거나 핵심 테이블이 비어 있으면
503을 반환한다. 키 값과 DB 경로 원문은 노출하지 않는다.

## 4. Docker 실행

```powershell
docker build -t ncs-mcp:local .
docker run --rm -p 8766:8766 -v ${PWD}\data\processed:/data ncs-mcp:local
```

최소 readiness smoke DB로 컨테이너를 확인할 수도 있다.

```powershell
mkdir docker-smoke
docker run --rm -v ${PWD}\docker-smoke:/data ncs-mcp:local python -m ncs_mcp.smoke_data --out /data/ncs.db
docker run --rm -p 8766:8766 -v ${PWD}\docker-smoke:/data ncs-mcp:local
```

Docker CLI가 없는 환경에서는 CI의 Docker build 및 container readiness smoke job으로 확인한다.

## 5. MCP 클라이언트 등록

STDIO 클라이언트:

```json
{
  "mcpServers": {
    "ncs-training": {
      "command": "C:\\workspace\\NCS_MCP\\run_ncs_mcp_stdio.cmd"
    }
  }
}
```

HTTP 클라이언트:

```json
{
  "mcpServers": {
    "ncs-training-http": {
      "url": "http://127.0.0.1:8766/mcp"
    }
  }
}
```

공개 tool contract:

```powershell
python scripts\export_mcp_tool_contract.py --out mcp\ncs-tool-contract.json
```

## 6. 활성 범위 확인

활성 제품 범위는 NCS 중심이다.

- 활성: NCS 구조 검색, KSA/과업 온톨로지, 훈련과정 추천, 경력전환 추천, 자격/직업기초능력 근거.
- 숨김/레거시: SQF와 NCS 학습모듈 tool surface.
- operator/review tools는 `ncs_execute_tool`로 실행되지 않는다.
- `ncs_execute_tool`로 추천 도구를 실행하면 `save=false`가 강제된다.

## 7. 클라이언트 응답 사용 원칙

- 추천 도구는 기본적으로 `compact=true`를 사용한다.
- not-found는 `error.code` 또는 `error.category`를 기준으로 처리한다.
- `[NOT_FOUND]` 텍스트 마커는 LLM 안내용이며, 클라이언트 분기 조건으로 쓰지 않는다.
- `external_dependency`이면서 `retryable=true`인 오류만 자동 재시도 후보로 본다.

## 8. Qualification API collection guard

Before release, qualification evidence may be partial, but collection jobs must
be resumable and must not hammer the upstream API.

Required checks:

- Run `python scripts\ncs_harness.py qualification-summary --limit 10` and keep
  the current coverage/error concentration in the release report.
- Run `python scripts\ncs_harness.py qualification-retry-hygiene --limit 50`
  before retrying cached errors.
- Use `--stop-after-rate-limit-errors` for every broad
  `retry-qualification-errors` or `collect-qualification-items --all-units`
  batch.
- Treat `stopped_early=true` or `stop_reason=rate_limited` as a hard stop for
  that collection wave.
- Do not use `--include-not-due`, `--refresh`, or very large `--limit-units`
  values during routine recovery.

Safe retry template:

```powershell
python scripts\ncs_harness.py retry-qualification-errors --limit-units 50 --num-of-rows 50 --max-pages 1 --request-delay 2 --max-retries 1 --retry-backoff-seconds 30 --stop-after-rate-limit-errors 3 --report-path reports\qualification_error_report.md
```

Safe new-coverage template:

```powershell
python scripts\ncs_harness.py collect-qualification-items --all-units --limit-units 100 --num-of-rows 50 --max-pages 1 --request-delay 2 --max-retries 1 --retry-backoff-seconds 30 --stop-after-rate-limit-errors 3
```

Release readiness remains false until
`scripts\release_readiness_report.py` reports the configured qualification
coverage threshold as satisfied.
