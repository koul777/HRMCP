# HRMCP — Vercel HTTPS 배포 가이드

HRMCP(NCS 기반 HR MCP)를 Vercel 서버리스(Streamable-HTTP)로 배포해
**하나의 HTTPS URL** 로 ChatGPT·Claude 등 원격 MCP 클라이언트에 연결하는 방법을
정리합니다. (`HRMCP` 는 표시 이름이며 내부 패키지는 `ncs_mcp` 로 유지됩니다.)

- MCP 엔드포인트: `https://ncs-mcp-bridge-mini2.vercel.app/api/mcp`
- Health: `https://ncs-mcp-bridge-mini2.vercel.app/api/health`
- Readiness: `https://ncs-mcp-bridge-mini2.vercel.app/api/ready`

읽기 전용·운영도구 비활성화가 기본값입니다. 추천 결과는 교육 계획용 참고
자료이며 공식 자격·채용·법적 판단이 아닙니다.

---

## 1. 구성 요소

| 파일 | 역할 |
|------|------|
| `vercel.json` | 함수 엔트리(`api/index.py`), 라우팅, 기본 환경변수, 서빙 DB 포함 규칙 |
| `api/index.py` | `/api/mcp`, `/api/health`, `/api/ready` 를 멀티플렉싱하는 ASGI 진입점 |
| `api/mcp.py` | `ncs_mcp.server` 를 Streamable-HTTP ASGI 앱으로 노출 + 서빙 DB 부트스트랩 |
| `api/health.py`, `api/ready.py` | 상태 점검 엔드포인트 |
| `src/ncs_mcp/**` | MCP 서버 본체 |
| `requirements.txt` | 런타임 의존성 |

배포 시 실행 흐름은 `api/mcp.py` 의 `_configure_for_vercel()` 이 담당합니다:

1. Streamable-HTTP 트랜스포트 구성(공개 호스트이므로 DNS rebinding 보호 해제).
2. **로컬 스냅샷 부트스트랩** — 리포/패키지에 서빙 DB가 있으면 `/tmp` 로 준비.
3. **원격 부트스트랩** — `NCS_DB_URL` 이 있으면 배포 런타임에서 자동 다운로드
   (권장 방식).

---

## 2. 데이터 정책 — 서빙 DB는 리포지토리에 커밋하지 않습니다

Vercel HTTPS 버전이 실제로 쓰는 데이터는 **전체 원본 DB(`data/processed/ncs.db`,
수 GB~수십 GB)가 아니라**, 필요한 7개 테이블만 뽑은 **읽기 전용 서빙 슬라이스**
입니다.

- 커밋 대상: 소스 코드, 문서, 스크립트, 테스트, 소형 매니페스트만.
- **커밋 금지**: `.db`, `.db-wal`, `.db-shm`, `.xlsx`, `tmp/` 산출물.
  전체 `ncs.db` 와 `api/ncs_interview_serving_release.db` 는 `.gitignore` 로
  제외되어 있습니다.
- 서빙 DB(약 117MB)는 GitHub **단일 파일 100MB 한도**를 넘으므로 일반 커밋으로
  올릴 수 없습니다. → **GitHub Release 자산**으로 배포하고 Vercel 이
  `NCS_DB_URL` 로 내려받습니다.

자세한 근거와 테이블 스키마는 `docs/INTERVIEW_SERVING_DB.md` 참고.

---

## 3. 서빙 DB 만들기 (Export)

리포지토리 루트(`C:\workspace\NCS_MCP`)에서 실행합니다.

```powershell
python scripts\export_interview_serving_db.py `
  --source data\processed\ncs.db `
  --destination tmp\ncs_interview_serving.db `
  --report reports\ncs_interview_serving.json
```

- `--source` : 로컬 전체 빌드 DB.
- `--destination` : 배포용 서빙 슬라이스(약 117MB).
- `--report` : 테이블 카운트·파일 크기 매니페스트(릴리스 노트에 사용).

서빙 DB에 담기는 테이블: `classifications`, `competency_units`,
`competency_elements`, `performance_criteria`, `ksa_items`,
`ncs_training_courses`, `ncs_query_aliases`.

---

## 4. 서빙 DB를 GitHub Release 자산으로 업로드

> 현재 배포용 서빙 DB는 이미 릴리스로 게시돼 있습니다:
> <https://github.com/koul777/HRMCP/releases/tag/ncs-serving-2026-02>
> 자산 URL:
> `https://github.com/koul777/HRMCP/releases/download/ncs-serving-2026-02/ncs_interview_serving_release.db`

새 슬라이스를 새로 게시할 때는 빌드 날짜 또는 NCS 소스 버전을 태그에 명시합니다.

```powershell
gh release create ncs-serving-2026-02 `
  tmp\ncs_interview_serving.db `
  --title "NCS interview serving DB (2026-02)" `
  --notes-file reports\ncs_interview_serving.json
```

업로드 후 자산 다운로드 URL을 확인합니다.

```powershell
gh release view ncs-serving-2026-02 --json assets --jq ".assets[].url"
```

이 URL을 다음 단계에서 `NCS_DB_URL` 로 사용합니다.

---

## 5. Vercel 환경변수 설정

`vercel.json` 에 읽기 전용 기본값이 이미 들어 있습니다:

```text
NCS_MCP_READ_ONLY=1
NCS_MCP_ENABLE_OPERATOR_TOOLS=0
NCS_MCP_DISABLE_DNS_REBINDING_PROTECTION=1
NCS_MCP_STREAMABLE_HTTP_PATH=/mcp
NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS=2
NCS_DB_PATH=/tmp/ncs_interview_serving.db
```

서빙 DB를 Release에서 받도록 **`NCS_DB_URL` 만 추가**하면 됩니다.

```powershell
vercel env add NCS_DB_URL production
# 프롬프트에 4단계에서 확인한 Release 자산 URL 붙여넣기
```

> 런타임 동작: `NCS_DB_URL` 이 있으면 `api/mcp.py` 의 `_bootstrap_db_from_url()`
> 이 `NCS_DB_PATH`(`/tmp/...`)에 DB를 내려받아 read-only로 엽니다. 이미 받은
> 파일이 있으면 다시 받지 않습니다.

---

## 6. 배포

```powershell
vercel deploy --prod
```

배포 후 상태 확인:

```powershell
curl https://ncs-mcp-bridge-mini2.vercel.app/api/health
curl https://ncs-mcp-bridge-mini2.vercel.app/api/ready
```

`health` 응답의 `runtime.database.ready` 가 `true` 면 서빙 DB가 정상 로드된
것입니다.

---

## 7. ChatGPT / 원격 MCP 클라이언트 연결

MCP URL 한 줄만 등록하면 됩니다.

```text
https://ncs-mcp-bridge-mini2.vercel.app/api/mcp
```

ChatGPT Custom GPT(Agent/Tools) 설정용 최소 payload:

```json
{
  "mcpServers": {
    "hrmcp": {
      "url": "https://ncs-mcp-bridge-mini2.vercel.app/api/mcp"
    }
  }
}
```

로컬에서 같은 서버를 시험하려면 `mcp/ncs-mcp-http.json` 의
`http://127.0.0.1:8766/mcp` 를 쓰고, HTTP 런처는 `.\run_ncs_mcp_http.cmd` 로
띄웁니다.

---

## 8. 노출되는 MCP 도구

읽기 위주의 공개 표면만 활성화됩니다.

- NCS 구조·온톨로지 조회
- 훈련과정 검색·상세
- 과업 기반 / 전직 기반 훈련 추천
- AI-HR 교육경로 계획(`query_route`, `recommended_path`,
  `training_system_matrix`, 근거 트레이스)

운영·리뷰 도구는 공개 실행 표면에 포함되지 않으며, 메타 실행으로 호출되는 추천
도구는 no-save 로 동작합니다.

---

## 9. 보안 메모

- 배포 서버리스 트랜스포트는 공개 호스트 수용을 위해 DNS rebinding 보호가
  꺼져 있습니다(`NCS_MCP_DISABLE_DNS_REBINDING_PROTECTION=1`). 인증·TLS·사용자
  단위 인가는 기관 게이트웨이에서 담당합니다
  (`docs/INSTITUTIONAL_CHATBOT_SELF_HOST_GUIDE.md`).
- API 서비스 키(`NCS_*_SERVICE_KEY`)는 이 배포에 필요하지 않습니다. 서빙 DB는
  이미 오프라인에서 생성된 산출물이며, 키는 `/health`·`/ready`·MCP 응답에 절대
  노출되지 않습니다.
- Release 자산 URL을 사설로 관리해야 하면 공개 리포지토리 대신 프라이빗
  리포지토리의 Release 를 쓰고, 배포 파이프라인에서만 접근 가능한 토큰으로
  다운로드하도록 구성하세요.
