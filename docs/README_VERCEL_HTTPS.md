# HRMCP — Vercel HTTPS 배포 가이드

HRMCP(NCS 기반 HR MCP)를 Vercel Serverless(Streamable HTTP)로 배포해 하나의
HTTPS MCP URL로 연결하는 운영 가이드입니다. `HRMCP`는 표시 이름이고, 내부
패키지는 `ncs_mcp`입니다.

- MCP: `https://ncs-mcp-bridge-mini2.vercel.app/api/mcp`
- Health: `https://ncs-mcp-bridge-mini2.vercel.app/api/health`
- Readiness: `https://ncs-mcp-bridge-mini2.vercel.app/api/ready`
- 현재 production deployment: `dpl_94usxf3AP6AjSdN8cySr1bu9fJK7`
- 측정된 production function bundle: 131.54MB

이 서비스는 읽기 전용입니다. 추천은 교육·업무 설계 참고자료이며 공식 자격, 채용,
법적 판단이 아닙니다.

---

## 1. 배포 구조

```text
canonical data/processed/ncs.db (12,648,931,328 bytes)
  -> deterministic stage + verify + Publisher
compact SQLite (425,758,720 bytes)
  -> ZIP package
deploy/vercel_mcp_app/api/ncs_ontology_compact.zip (120,785,873 bytes)
  -> Vercel startup verification + materialization
/tmp/ncs_ontology_compact.db (read-only runtime database)
```

Publisher는 고정된 export, package, verify 단계를 stage에서 실행한 뒤, 검증된 ZIP과
manifest만 원자적으로 publish합니다. 실패하면 기존 pair를 rollback합니다. AI 모델을
포함하거나 호출하지 않으며, canonical DB를 변경하지 않고 NCS API도 수집하지 않습니다.
Vercel 런타임도 요청 처리 중 AI 모델이나 외부 NCS API를 호출하지 않습니다.

API 갱신은 이 배포와 분리된 upstream scheduled/guarded 파이프라인의 책임입니다.
그 파이프라인은 체크포인트, 재시도/품질 게이트, 그리고 자격 API에 대한 운영자 승인을
거친 뒤 하나의 canonical DB를 만들고, 그 DB만 Builder 입력으로 넘깁니다.

`NCS_DB_URL`은 표준 배포에 필요하지 않습니다. 현재 함수는 번들 안의 ZIP과 manifest를
검증해 `/tmp`에 DB를 materialize하고 read-only로 엽니다. 외부 DB override는 기본적으로
꺼진 별도 운영 기능이며, 표준 배포 경로로 문서화하지 않습니다.

## 2. 배포 루트와 구성 파일

Vercel CLI는 반드시 다음 디렉터리에서 실행합니다.

```text
deploy/vercel_mcp_app
```

| 경로 | 역할 |
| --- | --- |
| `deploy/vercel_mcp_app/vercel.json` | 함수 진입점, 라우팅, 읽기 전용 환경값, ZIP/manifest 포함 규칙 |
| `deploy/vercel_mcp_app/api/index.py` | `/api/mcp`, `/api/health`, `/api/ready` 라우팅 |
| `deploy/vercel_mcp_app/api/mcp.py` | transport 구성 및 verified snapshot 부트스트랩 |
| `deploy/vercel_mcp_app/api/ncs_ontology_compact.zip` | 배포되는 압축 SQLite snapshot |
| `deploy/vercel_mcp_app/api/ncs_ontology_compact.manifest.json` | 크기·SHA-256·스키마·행 수 검증용 manifest |
| `deploy/vercel_mcp_app/src/ncs_mcp/vercel_snapshot.py` | 안전한 archive 검사, `/tmp` materialization, SQLite/readiness 검증 |

`vercel.json`의 `includeFiles`는 필요한 Python 코드·의존성·ZIP·manifest를 함수에
포함합니다. raw SQLite는 제외됩니다.

## 3. 현재 compact snapshot 내용

manifest 기준 주요 서빙 건수는 다음과 같습니다.

| 데이터 | 건수 |
| --- | ---: |
| 능력단위 / 수행준거 | 13,435 / 196,658 |
| 원천 KSA / 원자 KSA | 574,279 / 644,384 |
| 온톨로지 개념 / 별칭 / 라벨 후보 | 533,909 / 1,795 / 755 |
| 수행준거-개념 논리 연결 / 온톨로지 관계 논리 건수 | 3,025,498 / 3,235,434 |
| 교육과정 / 과정-능력단위 | 11,819 / 11,816 |
| 과정-개념 / 과정-능력단위요소 / 훈련목표-개념 | 479,583 / 100,659 / 348,877 |
| 훈련 운영·전달 관계 | 69,162 |
| 경력개발경로 / 자격 종목 / 직업기초능력 링크 | 12,864 / 1,039 / 230,920 |
| 전환 gold scenario / 사람 검토 review | 100 / 11 |

사람 검토를 거친 라벨 별칭 742건만 병합되었습니다. 이를 자동 라벨 병합이나 자동 승인으로
해석하면 안 됩니다.

## 4. 새 canonical DB에서 snapshot 만들기

새 `ncs.db`를 받으면 먼저 변경 인식형 Refresh Builder를 실행합니다. 기본 실행은 계획만
만들며 원본과 기준본을 수정하지 않습니다. `--apply`를 명시해야 별도의 준비본이 생성됩니다.

```powershell
python scripts\refresh_ncs_ontology.py data\processed\ncs.db `
  --state-dir C:\ncs_mcp_state\ncs-ontology-refresh `
  --report reports\ncs_ontology_refresh_plan.json

python scripts\refresh_ncs_ontology.py data\processed\ncs.db `
  --state-dir C:\ncs_mcp_state\ncs-ontology-refresh `
  --output build\prepared\ncs.db `
  --report reports\ncs_ontology_refresh_apply.json `
  --apply
```

변화가 없으면 마지막 원격 검증까지 마친 기준본을 재사용합니다. 작은 추가 변화는 증분
구축하고, 수정·삭제·스키마 충돌·사람 검토 관계 충돌은 자동 배포하지 않고 차단합니다.
성공 보고서의 `publisher_source`만 아래 one-input Publisher에 넘깁니다.

```powershell
python scripts\publish_vercel_snapshot.py --source <publisher_source.path>
```

필요할 때만 `--deploy-root <path>`, `--dry-run`, `--report <path>`를 추가합니다.
Publisher는 임시 stage에서 아래 고정 단계를 수행해 source hash를 다시 확인하고, 검증된
ZIP과 manifest pair만 배포 루트 `api/`에 원자적으로 publish합니다. publish 중 실패하면
기존 complete pair를 rollback합니다.

1. `export_interview_serving_db.py --profile vercel-ontology-compact`
2. `package_vercel_compact_snapshot.py`
3. `verify_vercel_compact_package.py --skip-function-bundle-check`

`build_vercel_snapshot.py`는 custom output 경로가 필요한 경우에만 쓰는 low-level Builder입니다.
기존 출력을 덮어쓰지 않으며, API 수집·사람 검토 상태 변경·Vercel 배포를 수행하지 않습니다.

훈련과정·직업기초능력 API 자동 갱신은 `refresh_ncs_api_evidence.py`가 원본의 SQLite 온라인
백업 복사본에서만 수행합니다. 자격/NCS006은 기존 운영자 승인·재시도 절차를 유지합니다.
전체 자동 흐름은 `.github/workflows/vercel-snapshot-release.yml`에 있으며, 임시 Vercel
배포와 Remote MCP 검증이 모두 성공한 뒤에만 `promote_ncs_refresh_baseline.py`가 다음
비교 기준본을 승격합니다.

## 5. Preview와 Production 배포

수동 배포 시 Vercel CLI를 연결한 뒤 canonical deploy root에서 실행합니다. 아래 직접 배포
명령은 Vercel이 소스에서 다시 빌드하므로 `--prebuilt`를 붙이지 않습니다. 자동 워크플로는
`vercel build`로 만든 `.vercel/output`과 함수 번들을 먼저 검증하고, 그 동일 산출물을
`vercel deploy --prebuilt --prod --skip-domain`으로 올립니다. 고유 배포 URL의 MCP 검증이
성공한 뒤에만 `vercel promote`를 수행합니다.

```powershell
cd deploy\vercel_mcp_app
vercel deploy
vercel deploy --prod
```

첫 번째 명령은 preview deployment를 만들고 URL을 출력합니다. 두 번째 명령은 production
alias로 배포합니다. 배포 후 다음을 확인합니다.

```powershell
curl https://ncs-mcp-bridge-mini2.vercel.app/api/health
curl https://ncs-mcp-bridge-mini2.vercel.app/api/ready
```

`ready` 응답이 snapshot manifest와 필요한 테이블/최소 행 수 검증을 통과했는지 확인합니다.
검증에 실패하면 MCP 요청은 DB를 열지 않고 실패합니다.

`vercel.json`의 `git.deploymentEnabled=false`로 Git push는 production deployment를 만들지
않습니다. ignore된 ZIP이 Git 기반 배포에서 빠져 production을 덮어쓰는 일을 막기 위해,
검증된 snapshot pair를 publish한 뒤에만 위 Vercel CLI release를 실행합니다.

## 6. 런타임 보안과 공개 범위

`deploy/vercel_mcp_app/vercel.json`은 읽기 전용과 운영 도구 비활성화를 기본값으로 둡니다.

```text
NCS_MCP_READ_ONLY=1
NCS_MCP_ENABLE_OPERATOR_TOOLS=0
NCS_MCP_ENABLE_ADVANCED_TOOLS=0
NCS_MCP_STREAMABLE_HTTP_PATH=/mcp
NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS=2
NCS_MCP_ALLOW_EXTERNAL_DB_OVERRIDE=0
```

공개 호스트를 위해 DNS rebinding 보호를 해제하는 설정도 포함됩니다. 조직용 인증, 접근
통제, TLS 경계는 기관 게이트웨이가 책임져야 합니다. API 키는 Vercel serving runtime에
필요하지 않으며, health/readiness/MCP 응답에 노출되어서는 안 됩니다.

## 7. MCP 클라이언트 연결

ChatGPT 또는 원격 MCP 클라이언트에 아래 URL을 등록합니다.

```text
https://ncs-mcp-bridge-mini2.vercel.app/api/mcp
```

최소 설정 예시는 다음과 같습니다.

```json
{
  "mcpServers": {
    "hrmcp": {
      "url": "https://ncs-mcp-bridge-mini2.vercel.app/api/mcp"
    }
  }
}
```

로컬 시험은 `mcp/ncs-mcp-http.json`의 `http://127.0.0.1:8766/mcp`와
`run_ncs_mcp_http.cmd`를 사용합니다.
