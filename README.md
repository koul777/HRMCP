# HRMCP — NCS 기반 HR 실무용 MCP

## 📅 업데이트 내역

| 날짜 | 내용 |
| --- | --- |
| 2026-09-18 | 검색 품질 회귀 게이트 추가 — 기록된 기준선보다 지표가 떨어지면 릴리스 전에 실패 |
| 2026-09-18 | 검색 튜닝용 개발 세트 30문항 추가(HR 15 + 비HR 통제 15), 기준 Hit@3 `0.733`이며 HR 질의가 더 모호함을 확인 |
| 2026-09-18 | 공개 검색 `ncs_training`의 LIKE 와일드카드를 리터럴로 처리하고, SQL 조립 803곳을 전수 점검해 인젝션 경로가 없음을 확인 |
| 2026-09-18 | 31회 연속 실패하던 CI 복구(8.3 단축 경로·Python 3.11 junction 탐지·드라이브 간 경로), 테스트 3분할로 41분 → 19.7분 단축 |
| 2026-09-17 | 오염되지 않은 40문항 holdout v2를 고정 후 1회 측정, Hit@3 `0.500`으로 기존 지표의 과대평가 확인 |
| 2026-09-14 | 직무 범위를 원천 분류체계로만 확정하는 안전장치 추가, 모호한 범위는 추측 대신 명확화 요청 |
| 2026-09-13 | 띄어쓰기·조사 변형 복합어 검색 보강, 경량 DB 용량 절감(478MB → 457MB)과 용량 가드 추가 |
| 2026-09-11 | Gold LPG·Neo4j 적재와 시맨틱 임베딩 경로 연결, `ncs_analysis`에 사내 직무·의미 검색 모드 추가 |
| 2026-09-09 | Windows NCS DB 업데이트 빌더 추가(원본 변경분 → API 갱신 → 경량 DB → Vercel 반영) |
| 2026-08-30 | 공개 MCP 기준 URL을 `ncs-mcp-bridge-mini2`로 일원화 |

자세한 내용은 아래 [변경 이력](#변경-이력)에 있습니다.

---

## 2026-09-14 scope-safety and release update

- Added a source-backed hierarchy gate for every explicit job scope. Exact and unique NCS paths are promoted to a hard containment filter; unresolved, fuzzy, cross-major, cross-type, and Unicode-equivalent collisions fail closed with bounded clarification candidates.
- Applied the same rule to task-transition recommendations so partial job labels can never fall through to a historical `LIKE ... LIMIT 1` choice.
- Added all-major execution-gate and collision-audit scripts. The latest gate covers 24/24 majors, 96 hierarchy samples, and 288 calls with zero unexpected results and no holdout inspection or database mutation.
- Hardened scope resolution against homographs: exact official competency-unit titles now bind to their complete source path, while one-sided prefixes (for example `인사하기` → `인사`) are not promoted to a classification. A 120-case stratified prefix/internal/exact off-path execution gate passes with zero leakage.
- Added Builder safeguards for code-only releases: a missing deployed-version pointer is rejected, and the operation is journaled as `copy_current` with restart-safe version persistence. Superseded Builder versions are recoverably archived off the system drive; the active production pointer is unchanged.
- Verified the canonical source and `deploy/vercel_mcp_app` mirror remain synchronized. Lint, smoke, scope/recommendation, public payload, Builder, and deployment-source tests pass.
- Stabilized agent-queue lineage checks by hashing queue state independently of report-delivery paths; continuation demo artifacts now use the same internal-file naming contract as the generator. Read-only queue regeneration produced four successful report jobs with zero acceptance failures; one transition seedpack remains explicitly human-gated.
- Treats candidate-alias and other unresolved scope responses as an explicit, bounded clarification in the AI-HR demo contract. The alias demo may therefore show a safe fail-closed choice request instead of fabricating a training plan; baseline plans still require the full matrix/guide contract.
- Production deployment completed through the Windows Builder for project `ncs-mcp-bridge-mini2`: Builder version `20260913_230853_59362889`, build ID `173fa70c69ab455db6fd0be19d5a7727`, deployment `dpl_7rfof6Y7b6keyWj7WZrtzvXJvXLh`. Staged and production health, readiness, MCP transport, and build-identity checks all passed.

> 운영 원칙(2026-09-11): production DB/API 갱신, 온톨로지 재구축, 경량
> 패키지 생성, Vercel 반영, 기준본 승격은 Windows NCS Data Builder
> (`run_ncs_builder.bat`) 한 경로로만 수행합니다. GitHub Actions는 CI
> 검증만 수행하며, 별도 snapshot refresh/deploy workflow와 self-hosted
> runner는 운영하지 않습니다. 자격/NCS006 수집만 기존 운영자 승인·재시도
> 가드를 따르는 별도 예외이며 자동 Builder 단계가 아닙니다.

> **HR 실무에서 NCS를 활용하는 가장 빠른 길.**
> 채용 직무에 맞는 NCS 분류부터 능력단위 → 능력단위요소 → 수행준거 → 지식(K)·기술(S)·태도(A)까지,
> 사람이 일일이 찾아 정리하던 정보를 이제 AI가 구조화된 NCS 데이터베이스에서 직접 조회해 활용합니다.

<div align="center">

<img src="docs/hrmcp_promo.gif" alt="HRMCP 소개 영상: NCS 데이터를 조회해 직무기술서와 면접 질문을 만드는 과정" width="760">

<br>

**[🎬 원본 영상(MP4) 내려받기](https://github.com/koul777/HRMCP/raw/main/docs/hrmcp_promo.mp4)** · **[🔌 연결 방법 바로가기](#-hrmcp-연결-방법)**

<br>
<br>

<img src="docs/images/hrmcp_readme_overview.png" alt="민간 인사담당자를 위한 HRMCP 주요 기능 소개: 직무기술서, 구조화 면접, 교육훈련 추천, 경력개발·배치, 조직 역량관리" width="900">

</div>

---

## 🚀 HRMCP를 공개합니다

HR 실무에서 NCS를 활용하려면 생각보다 많은 시간과 손이 필요합니다. 채용 직무에 적합한 NCS
분류를 찾고, **능력단위 → 능력단위요소 → 수행준거 → 지식(K)·기술(S)·태도(A)** 를 하나씩 확인한
뒤 다시 정리해야 하기 때문입니다.

HRMCP는 이러한 NCS 데이터를 구조화해, ChatGPT가 필요한 정보를 직접 조회하고 HR 업무에
활용할 수 있도록 만든 **HR 실무용 MCP** 입니다. 쉽게 말하면, 사람이 NCS 사이트에서 일일이
찾고 정리하던 정보를 이제는 AI가 **구조화된 NCS 데이터베이스**에서 직접 찾아 활용할 수 있도록
만든 것입니다.

> 이름 안내: "HRMCP"는 제품/표시 이름이자 MCP 연결 라벨입니다. 내부 파이썬 식별자
> (`ncs_mcp` 패키지, `ncs_*` 도구 이름)는 호환성을 위해 그대로 유지됩니다.

---

## ✨ 핵심 기능 한눈에 보기

| HR 업무 | HRMCP가 제공하는 지원 | 연결되는 NCS 근거 |
| --- | --- | --- |
| **직무기술서 작성** | 공고문·업무 표현을 관련 NCS 직무와 능력단위로 찾아 JD 초안을 구성 | 분류, 능력단위, 능력단위요소, 수행준거 |
| **구조화 면접 설계** | 수행준거와 KSA를 기반으로 질문·추가질문·평가요소 초안을 정리 | 수행준거, 지식(K)·기술(S)·태도(A) |
| **교육훈련 추천** | 목표 과업의 부족 KSA와 연결된 과정 및 학습경로를 근거와 함께 제안 | 훈련목표, 수준, 시간, 방법, 시설 |
| **경력개발·배치 지원** | 직무 사이의 공통·추가 역량을 비교해 전환 및 육성 검토안을 작성 | KSA 관계, 경력개발경로, 자격·직업기초능력 |
| **조직 역량관리** | 직무군의 공통역량과 팀 역량 구조를 탐색 가능한 형태로 정리 | NCS 계층, KSA 온톨로지, 과업 유사도 |

```text
실무 질의 → NCS 범위 검색 → 능력단위·수행준거 확인 → KSA 근거 연결
          → 직무·면접·교육·경력개발 초안 → HR 담당자 검토
```

HRMCP는 인사결정을 자동화하지 않습니다. AI가 근거 기반 초안과 탐색 결과를 만들고, 적용 범위와
최종 판단은 HR 담당자가 확인하는 구조입니다.

---

## 🧱 핵심은 데이터 전처리입니다

이번 공개까지 약 한 달 동안 NCS 원천 데이터를 정리하고 전처리했습니다. 현재 배포 데이터
기준으로 아래 규모의 데이터를 서로 연결해 **AI가 조회할 수 있는 관계형 구조**로 재구성했습니다.

| 구분 | 규모 |
| --- | --- |
| 능력단위 | **13,435개** |
| 능력단위요소 | **47,620개** |
| 수행준거 | **196,658개** |
| 지식·기술·태도(KSA) | **57만 건 이상** |

공개 서버에는 이 핵심 전처리 결과를 유지한 **경량화 DB**가 탑재돼 있습니다. 일부 직무나
데이터를 샘플로 넣은 것이 아니라, **직무기술서·면접·교육훈련 설계에 필요한 핵심 NCS 데이터는
그대로 유지**하고, 공개 서비스에 불필요한 확장·운영 테이블만 덜어냈습니다.

사용자는 **HTTPS 주소 하나만 연결**하면 되지만, 그 뒤에서는 한 달 동안 구조화한 NCS 데이터가
AI의 검색과 결과물 작성을 뒷받침합니다.

---

## 📌 최근 운영 업데이트

### NCS DB 업데이트 빌더 사용하기

이 빌더는 정기적으로 새로 제공되는 NCS 정보망 Excel을 기존 데이터와 비교해 **추가·삭제·변경된 능력단위를 반영하고, 관련 온톨로지와 배포용 경량 DB를 다시 구성하는 Windows 관리 도구**입니다. 온톨로지 편집만을 위한 도구가 아니므로 표시 이름을 **NCS DB 업데이트 빌더**로 정리했습니다.

**실행:** 저장소의 `run_ncs_builder.bat`를 더블클릭합니다. 기존 프로젝트의 Python `.venv`와 설치된 의존성이 필요하며, 독립 설치형 EXE는 아닙니다. API 갱신에는 프로젝트에 설정된 해당 API 키가, Vercel 반영에는 Vercel CLI 로그인과 기존 MCP 프로젝트 연결 권한이 필요합니다. 키는 화면이나 보고서에 붙여 넣지 않습니다.

이 실행 파일이 production lifecycle의 유일한 운영 진입점입니다. 개별 Python
Builder/Publisher 스크립트와 Vercel CLI는 구현 구성요소이지 운영자가 우회 실행할
두 번째 경로가 아닙니다. Preview 생성과 원격 상태 조회도 Builder가 선택 버전의
검증·복구 기록 안에서 수행하며, 읽기 전용 상태 조회는 장애 진단용 비운영 작업으로만
사용합니다.

| 단계 | 사용자가 할 일 | 완료 결과 |
| --- | --- | --- |
| ① 원본 · 온톨로지 | 새 **전체** Excel을 선택하고 구조를 확인합니다. 비교 기준 DB를 확인한 뒤 전체 원본 체크 → `변경분 검토 · 온톨로지 만들기`를 누릅니다. | 추가·변경·제외·유지 건수와 검증된 후보 DB가 저장됩니다. |
| ② API 갱신 | 필요한 API를 선택하고 `선택 API 점검 · 갱신`을 누릅니다. | 선택 버전에 전체 NCS 범위의 API 근거와 연결 관계를 갱신합니다. |
| ③ 경량 DB 생성 | 자동으로 표시된 MCP 프로젝트 이름을 확인하고 `경량 DB 만들기 · 검증`을 누릅니다. | 온톨로지를 포함한 경량 DB·ZIP과 검증 결과가 저장됩니다. 아직 운영 반영 전입니다. |
| ④ Vercel 반영 | 선택 버전·대상 프로젝트·운영 URL을 확인하고 `Vercel MCP 업데이트`를 누릅니다. | 검증용 배포 → 연결 검사 → 운영 전환 → 운영 MCP 검사까지 수행합니다. |

**NCS 데이터는 그대로이고 MCP 코드만 바뀐 경우**에는 ③ 탭에서
`현재 운영 DB로 코드 배포 버전 준비`를 먼저 누릅니다. 마지막 성공 배포의
검증된 DB를 새 Builder 버전으로 로컬 복사하므로 API 재수집이나 온톨로지
재계산은 하지 않습니다. 이어서 ③·④를 실행합니다. 이 대용량 로컬 복사본은
compact snapshot 생성·검증용이며 Vercel에는 업로드되지 않습니다.

**3·4단계 폴더는 자동 입력됩니다.** 마지막 성공 배포의 연결 폴더를 먼저 확인하고, 없거나 유효하지 않으면 이 저장소의 `deploy/vercel_mcp_app/.vercel/project.json`을 확인합니다. 프로젝트 이름으로 운영 MCP URL도 채웁니다. `기존 연결 자동 찾기`로 다시 탐색할 수 있습니다. 찾지 못할 때만 `찾아보기`에서 기존 MCP 연결 폴더를 선택하세요. 저장소 루트는 별도 Vercel 프로젝트일 수 있으므로 임의 선택하지 않습니다. 현재는 기본 `https://<projectName>.vercel.app/api/mcp` 주소를 지원합니다.

**여러 번에 나누어 진행할 수 있습니다.** 각 단계는 별도 버튼으로 실행하며 다음 단계가 자동 시작되지 않습니다. 완료한 버전·실패 시도·마지막 진행 상황은 `.state/ncs-data-builder/`에 누적됩니다. 창을 다시 열면 재개 가능한 중단 작업을 우선 불러옵니다. `버전 · 실행 기록`에서 필요한 버전을 선택하고 남은 단계만 실행하세요. 예를 들어 ③까지 완료했다면 다음 실행에서 ④부터 진행할 수 있습니다. **중단한 작업 이어하기**를 누르면 API 수집은 완료한 API·대분류를 건너뛰고, 수집 완료 후에는 API를 다시 호출하지 않고 후보 DB 검증부터 재개합니다. 최종 검증은 완료한 검사와 테이블 건수를 저장해 재사용합니다. 재개 전에 원본·후보 DB 동일성을 확인합니다. 중단된 대분류의 개별 페이지, 실행 중이던 SQL 문장, Excel 변경 처리 도중, 경량 DB 생성 도중은 각각의 작업을 다시 실행합니다. 실패 시도와 미확정 실행은 완료로 계산하지 않습니다.

**진행률 읽기:** 현재 작업의 바이트·페이지·행·능력단위·완료 공정 수를 측정하며, 작업이 바뀌면 해당 작업 기준으로 퍼센트가 바뀝니다. 전체량이 확인되지 않은 작업은 작업명과 경과시간을 보여줍니다. 아래의 전체 진행률은 선택 버전의 **완료 단계 수 ÷ 4**입니다(`3/4 = 75%`). 남은 시간의 비율이 아니며, 생략한 단계는 완료로 계산하지 않습니다.

**업데이트 범위:** 이전 Excel·DB를 보존하고 별도 후보를 만듭니다. 원천의 변경 능력단위만 갱신하되 과업 유사도·훈련 연결 등 공통 관계는 정합성을 위해 전체 재계산하고, 경량 DB도 전체 스냅샷으로 재생성합니다. 일부 시트만 업로드하면 누락된 단위를 제외 대상으로 판단하므로 반드시 전체 파일을 사용하세요. 성공한 운영 배포 버전이 다음 비교 기준이며, 기존 `data/processed/ncs.db` 파일 자체를 덮어쓰는 방식은 아닙니다.

오류가 나면 `선택 버전 보고서 열기`와 실패 버전의 `build.json`·`api-refresh.json`·`release.json`을 확인합니다. `failed_no_reconcile`은 실패한 API 결과가 배포용으로 반영되지 않았다는 뜻입니다. 진행 중인 기존 Builder는 작업 종료 후 다시 열어야 이번 UI 변경을 사용할 수 있습니다. 자세한 보관·복구·API 수집 제한은 [빌더 운영 가이드](docs/NCS_DATA_BUILDER.md)를 참고하세요.

### 변경 이력

- **2026-09-18 공개 검색의 LIKE 와일드카드 이스케이프·SQL 조립 전수 점검**: 공개 도구 `ncs_training`이 사용자 입력의 `%`와 `_`를 LIKE 와일드카드로 그대로 넘기던 문제를 고쳐, `ncs_search`와 동일하게 리터럴로 매칭하도록 맞췄습니다. 이제 `100%` 검색은 해당 문자열을 포함한 과정을 찾습니다. 함께 `src/ncs_mcp`·`api` 전체의 SQL f-string 803곳(고유 표현 272개)을 AST로 전수 분석해 인젝션 경로가 없음을 확인했습니다. 사용자 값은 예외 없이 `?`로 바인딩되고, SQL 조각은 모두 코드 내 고정 문자열·상수·정수 인덱스이며, 테이블·컬럼명을 인자로 받는 4개 함수도 호출부가 전부 리터럴입니다. 공개 도구 7개에 임의 SQL 파라미터는 없고 서빙 DB는 `mode=ro`와 `PRAGMA query_only=ON`으로 엽니다. 와일드카드 입력의 성능 영향은 실측 결과 미미했습니다(정식 DB 11,819개 과정 기준 `%` 13.6ms, `_` 5.6ms). 회귀 테스트를 추가하고 Vercel 미러를 동기화했습니다.
- **2026-09-18 CI 복구와 병렬 분할**: 2026-09-12 이후 31회 연속 실패하던 CI를 복구했습니다. 원인은 모두 환경 차이였습니다. ① GitHub Windows 러너가 임시 경로를 8.3 단축 이름(`C:\Users\RUNNER~1\...`)으로 넘겨 Builder 경로 검사가 실패했고, ② `Path.is_junction()`이 Python 3.12부터 존재해 CI의 3.11에서는 junction 탐지가 동작하지 않았으며, ③ 12GB 정식 DB가 필요한 테스트에 skip 조건이 없었고, ④ 체크아웃(D:)과 임시 폴더(C:)가 다른 드라이브여서 상대 경로 계산이 실패했습니다. 경로 검사는 루트를 가리키는 가장 얕은 상위 경로만 해석으로 맞추고 그 아래 모든 구성요소의 reparse point 검사는 그대로 유지해 보안 강도를 낮추지 않았습니다. 단위 테스트 실패가 lint·smoke 단계를 가리지 않도록 워크플로를 고치고, 테스트를 모듈명 CRC32 기준 3개 shard로 병렬 분할해 CI 소요 시간을 41분에서 19.7분으로 줄였습니다.
- **2026-09-17 검색 평가의 정직한 기준선 확보**: 기존 40문항 세트는 alias를 설계할 때 사용한 in-sample이고, 51문항 holdout은 하루에 세 단계에 걸쳐 재측정되어 일반화 지표로서 오염됐습니다. 이를 대체할 40문항 holdout v2(`tests/fixtures/ncs_search_eval_nl_holdout_v2.json`)를 만들고, 측정 **전에** 커밋해 고정한 뒤 한 번만 측정했습니다. 질의 문구는 기존 두 세트와 전혀 겹치지 않고 14건은 어느 세트에도 없던 능력단위를 기대값으로 씁니다. 결과는 Hit@1 `0.450`, Hit@3 `0.500`, MRR `0.483`입니다. 특히 기존 평가에 한 번도 등장하지 않은 능력단위는 14건 중 4건(29%)만 적중한 반면 이미 쓰인 능력단위는 26건 중 16건(62%)이 적중해, 기존 지표가 일반화 성능을 과대평가했음을 확인했습니다. 이 세트는 튜닝 대상이 아니며 릴리스 판단 시에만 확인합니다.

- **2026-09-14 안전한 모호성 결과의 회귀 계약 보강**: 비HR 전환·교육계획 smoke가 source-backed 범위를 해석한 뒤 `needs_clarification`으로 중단하는 경우를 실패로 세지 않고 `safe_clarification`으로 명시하도록 하네스를 보강했습니다. 모호한 `기본구상` 사례는 임의 추천을 만들지 않고 추가 범위 선택을 요구하며, 추천·교육계획 필드가 비어 있어도 이 명시적 fail-closed 결과에서만 검증 예외가 적용됩니다. 전환·교육계획 smoke는 각각 8/8 통과(안전한 명확화 1건), 종합 quality gate는 31 pass·5 warn·0 fail이며 DB·alias·사람 승인 상태를 변경하지 않았습니다.
- **2026-09-14 직접 검색 컨텍스트 provenance 보강**: `ncs_search`가 `인사 직무에 필요한 역량`처럼 질의 문형에서 안전하게 추론한 직무 범위를 lower-level 검색 호출에도 전달하도록 수정했습니다. 결과의 `search_context`가 `job_scope=인사`, `source_backed_exact_job_scope`, `resolved`를 그대로 노출해 hard filter의 출처와 선택된 전체 분류 경로를 감사할 수 있으며, bare `인사하기`와 명시적 caller filter 경로는 변경하지 않았습니다.
- **2026-09-14 범위 복합어 재랭킹 일관성 보강**: 명시적 분류 범위에서 복원한 인접 2토큰 공식 복합어 점수를 과업·KSA 후속 재랭킹에도 동일하게 전달해, `경영 정보 대시보드 시각화`처럼 공백이 삽입된 공식 능력단위가 후보에만 머물지 않고 해당 범위 안에서 우선 노출되도록 했습니다. 점수는 공식 unit명·검증 alias에만 적용하고 중복 복합어는 한 번만 계산하며, 무범위 검색·정의/분류명·원천 DB·holdout에는 영향을 주지 않습니다. 회귀 212건과 lint를 통과했습니다.
- **2026-09-14 명시적 범위 안 띄어쓰기 복합어 보강**: 사용자가 공식 능력단위명을 중간 공백과 조사로 입력한 경우(예: `경영 정보 대시보드 시각화`)에 한해, 호출자가 제공한 source-backed 분류 hard filter 안에서만 인접 2토큰 복합어 후보를 unit명·기존 검증 alias에 제한해 검색하도록 보강했습니다. `출입 계약`이 `수출입계약` 내부 문자열로 승격되는 경로는 경계 검사를 유지해 차단하며, 대분류·세분류 범위가 없는 검색에는 이 보강을 적용하지 않습니다. 신규 alias·DB 쓰기·holdout 튜닝은 없고, 명시적 범위 synthetic 회귀 3건과 전체 테스트 2,587건(환경 의존 6건 skip)을 통과했습니다. 무범위 holdout은 기존 Hit@1 `0.5490`, Hit@3 `0.6471`, MRR `0.6060`에서 변하지 않았습니다.
- **2026-09-14 범위 안전성·운영 재검증**: 명시적인 직무/업무 질의를 NCS 원천 분류체계의 대분류→중분류→소분류→세분류 순서로 해석하고, 정확하고 유일한 경로만 hard filter로 승격하도록 보강했습니다. 미해결·모호·교차 대분류·교차 유형·유니코드 동등 충돌은 `route_context_required`로 중단하며, 과업 전환 추천에서도 더 이상 부분 문자열 `LIKE ... LIMIT 1`로 첫 행을 선택하지 않습니다. 전체 24개 대분류·96개 계층 표본·288회 실행 게이트에서 예상 밖 결과 0건, holdout 미열람, DB/alias/status 변경 0건을 확인했습니다. 충돌 감사는 원천 62,543건에서 교차 경로 2,829개 라벨과 off-path 위험 10,378건을 식별했으며 모두 검토 가능한 증거로만 보존합니다. `인사 직무에 필요한 역량`은 `02 > 02 > 02 > 01 인사`로 제한되고 `인사하기`는 제외되며, `경영관리`·`안전관리`·`관리`처럼 범위가 불명확한 입력은 안전하게 추가 맥락을 요구합니다. 전체 테스트 2,579개 통과(환경 의존 6개 skip), lint·smoke 통과 후 Builder 버전 `20260913_230853_59362889`을 `ncs-mcp-bridge-mini2` production에 재배포했고, build ID `173fa70c69ab455db6fd0be19d5a7727`, deployment `dpl_7rfof6Y7b6keyWj7WZrtzvXJvXLh` 및 공개 MCP 7/7개·실제 호출 12건을 확인했습니다. 원본 12.68GB DB는 보존하고 Vercel에는 456,929,280 bytes compact snapshot만 사용합니다.
- **2026-09-13 최종 운영 반영**: NCS MCP와 외부 대화형 클라이언트의 책임을 분리했습니다. commit `8e16bb0c87c48c7083ab02de8b2ce193cd0b1e7e`에서 특정 챗봇 호출자 이름을 해석하는 전용 분기와 특정 평가 산출물에 종속된 문구·회귀 사례를 제거하고, MCP가 `NCS 분류 범위 해석 → 능력단위·요소·수행준거·KSA 검색 → 출처 경로 반환 → 범위 밖·근거 없음 차단`만 담당하도록 정리했습니다. 검색·라우팅 회귀 테스트 178개와 lint·MCP 계약 검증을 통과했으며, 소스·테스트·배포 미러·계약에서 외부 챗봇 전용 문자열이 남지 않았음을 확인했습니다. 동일한 검증 DB 해시 `282eb9d8f9e963e8bd8e56ba2259e97eb9f32a5fa0aa22b0777622ab54f681f2`를 사용하는 Builder 버전 `20260913_101312_1ac66951`로 `ncs-mcp-bridge-mini2` production에 배포했습니다. 현재 Vercel deployment는 `dpl_yosyfnocZZ2p15UbCi6FZSAN4ag9`, 서버 build ID는 `5bbffade5ee4466c8b5f65abfd0d2b36`입니다. 배포 후 canonical endpoint에서 공개 도구 7/7개와 실제 MCP 호출 12건을 다시 검증해 실패 0건을 확인했습니다. compact DB는 `456,929,280` bytes, ZIP은 `120,158,362` bytes이며 480 MB hard cap까지 `23,070,720` bytes의 여유를 유지합니다.
- **2026-09-13**: 명시적인 `X 직무/업무 필요역량` 질의를 NCS 분류체계의 정확하고 유일한 세분류 경로에 먼저 결합한 뒤, 해당 가지의 능력단위→능력단위요소→수행준거·KSA만 조회하도록 commit `ccd59040cba791496bf34a15c127f7b5d78d49d6`에서 검색 경계를 강화했습니다. `직무/업무` 뒤의 `에`·`의`·`에서`·무조사 문형을 처리하며, `접객`처럼 exact·unique 분류로 승격할 수 없는 범위는 무범위 검색을 실행하지 않고 `route_context_required`로 중단합니다. HR `인사` 범위의 `인사하기`는 filtered `NOT_FOUND`로 처리해 근거 없는 NCS 주장에 사용하지 않습니다. 특정 분야 denylist나 신규 alias 없이 6개 대분류 운영 표본에서 `offscope=0`을 확인했고, 전체 테스트는 2,515개 통과·4개 환경 의존 skip입니다. Builder 단일 경로의 데이터 버전은 `20260913_085611_e74ff3ae`, Vercel deployment는 `dpl_Aic2S4fkXSBDoWN4eQ3WEMLrbkBH`, 서버 build ID는 `6e35bdc0361d45a3a8ebdddf3d17bafb`이며, production health·ready·MCP build identity와 공개 도구 7/7개를 재검증했습니다.
- **2026-09-13**: 검색 일반화와 Vercel snapshot 용량 개선 commit `d225a254672b867d686ce87c127e85bde7141c61`을 반영했습니다. 새 alias나 DB 쓰기 없이 공식 능력단위명의 보수적 복합어 결합만 허용해, 공식명에서 자동 생성한 비-holdout 띄어쓰기 변형 60건의 Hit@1/Hit@3/MRR@3를 `0.6333/0.7333/0.6750`에서 `0.9167/0.9833/0.9472`로 개선했습니다. 배포용 compact DB는 정수 ID 6개를 rowid 기반 `INTEGER PRIMARY KEY`로 보존하고 중복 ID 인덱스 5개를 제거해 `478,756,864` bytes에서 `456,929,280` bytes로 `21,827,584` bytes 절감했습니다. 43개 물리 테이블 행 해시·view 정의와 6개 대분류의 실제 추천·그래프 결과가 기존 snapshot과 동일함을 확인했고, 전체 테스트는 2,497개 통과·4개 환경 의존 skip입니다. 과업·KSA shadow 재랭킹은 NDCG/MRR이 각각 `+0.007366/+0.008565`였지만 구조 회귀 1건과 독립 의미 라벨 부재로 공개 적용을 **HOLD**했습니다. 기존 holdout은 개별 사례를 열거나 alias 튜닝에 사용하지 않았습니다.
- **2026-09-13**: 최종 검색 개선 commit `70c995ce61a62caffd82c3f2094ee5d92e75001f`를 Builder 단일 경로로 `ncs-mcp-bridge-mini2` production에 배포했습니다. Builder 데이터 버전은 `20260913_020821_38fadb3a`, Vercel deployment는 `dpl_BHEgRsnnLMg5exQWzurFm1yqqyi3`, 서버 build ID는 `7c31b93c0d4e46d59881a9824729fc26`입니다. 배포 후 공개 도구 7/7개와 실제 호출 12건을 다시 통과했고, 운영 의미 프로브에서 `채용관리를 → 인력채용`, `적격증빙 수취와 전표 처리 → 적격증빙관리·전표관리`, `출입 통제와 보안 점검`의 수출입 오탐 제거, `classification_filter.major_code=02`의 차량·행사 HR 범위 제한을 확인했습니다. compact DB는 `478,756,864` bytes로 480 MB 하드 캡까지 `1,243,136` bytes만 남아 있으므로 다음 데이터 갱신 전 용량 절감이 필수입니다.
- **2026-09-13**: 조사 제거 후에도 `채용관리`, `인사기획업무`처럼 저정보 접미사가 남는 복합어를 unit명과 해당 unit의 기존 정확 alias에만 제한해 보강했습니다. 새 alias나 DB 쓰기 없이 `채용관리를`의 1위를 `전작 경영관리`에서 `인력채용`으로 바로잡았고 기존 정의 후보는 뒤에 보존했습니다. 23개 대분류의 비-holdout 공식명 변형 162건에서 Hit@1/Hit@3/MRR@20이 `0→1.0`이었으며, dev 40건과 기존 synthetic 48건은 회귀가 없었습니다.
- **2026-09-13**: 과업·KSA 근거 재랭킹은 반환 후보마다 unit·element·criteria·KSA를 추가 SQL 1회로 수집하는 shadow profiler까지 구현했습니다. 후보가 있는 24회에서 추가 p50/p95는 `9.712/14.159ms`, 근거 샘플 coverage는 124/124였지만 독립 relevance 검증이 없으므로 공개 랭킹 승격은 **HOLD**입니다.
- **2026-09-13**: 다음 데이터 갱신이 Vercel 크기 제한에서 갑자기 실패하지 않도록 Builder에 버전별 `snapshot_capacity` 계약을 추가했습니다. 선택한 버전의 실제 staged ZIP member와 manifest를 다시 측정해 DB 크기, 460 MB 소프트 캡·480 MB 하드 캡, 남은 여유를 보고서와 화면에 표시합니다. 460 MB 초과는 명시적 경고로 남기되 다른 검증이 통과하면 배포할 수 있고, 480 MB 이상은 원본·기존 운영 배포·로컬 포인터를 보존한 채 원격 호출 전에 차단합니다.
- **2026-09-13**: 검색 개선 코드 commit `3983983a34863e59d432626700883786bc5546d3`를 Builder 단일 경로로 `ncs-mcp-bridge-mini2` production에 배포했습니다. Builder 데이터 버전은 `20260912_231258_f2fee72e`, Vercel deployment는 `dpl_EjBHSH4T7yND4MQVKp3TQX5gbazp`, 서버 build ID는 `0813c0c8014f4c62870ead373597d8b1`입니다. canonical endpoint의 health·ready·MCP 초기화, 공개 도구 7/7개와 실제 호출 12건을 배포 후 재검증했습니다.
- **2026-09-13**: 한국어 조사 때문에 기존 검색이 비어 버리는 경우를 보완하는 `morphology_fill` 단계를 추가했습니다. 기존 intent·문구·AND·확장 AND 결과와 토큰 OR 순서는 그대로 유지하고, OR 결과가 요청 한도보다 적을 때만 조사 제거 후보를 뒤에 추가합니다. 조사 제거는 최대 4토큰, 토큰당 1회, 최소 2음절, 받침 조건과 전체 검색어 일치를 요구하며 원 질의·DB·alias는 변경하지 않습니다.
- **2026-09-13**: 24개 NCS 대분류에서 만든 비-holdout 조사 변형 48건으로 검증한 결과 Hit@1은 `0.5000→0.5417`, Hit@3는 `0.5833→0.6250`, MRR@20은 `0.5525→0.5941`, 빈 응답은 `10→8`로 개선됐습니다. 기존 결과 prefix와 행 메타데이터는 48/48건 모두 보존됐습니다. 고정 40개 개발셋은 Hit@1 `0.775`, Hit@3 `0.875`, MRR@20 `0.8244`로 회귀가 없었고, 51개 독립 holdout은 `0.549 / 0.6078 / 0.5903`으로 변동 없이 남았습니다. holdout 사례를 보고 alias를 추가하지 않았습니다.
- **2026-09-13**: `context_text`·`job_scope` shadow 경로를 보조 SQL까지 포함해 계측했습니다. 공개 결과 순서와 분류 hard filter는 각각 100% 보존됐지만, compact DB 4개 비-holdout synthetic 질의×7회에서 p50/p95가 `450.491/797.842ms→561.086/895.279ms`로 늘고 보조 SQL이 `2→3회`가 되어 랭킹 승격은 계속 **HOLD**입니다.
- **2026-09-11**: NCS DB 업데이트 빌더에 **Gold LPG 생성·Neo4j 적재·시맨틱 임베딩·MCP 상태 확인** 경로를 연결했습니다. 검증된 Builder 기준 `882,394`개 노드와 `4,465,577`개 관계를 적재했고, 수행준거·능력단위요소·KSA 개념 `778,187`건의 1,024차원 임베딩을 79개 재개 가능 shard로 반영했습니다. SQLite는 계속 원본 권위 데이터이며 Neo4j는 1~2홉 탐색과 벡터 검색을 위한 선택형 read model입니다.
- **2026-09-11**: MCP에 Gold 스키마·상태 Resource와 `ncs_analysis`의 `internal_role`·`semantic` 모드를 추가했습니다. Neo4j가 비활성·장애 상태이면 승인되지 않은 결과를 만들지 않고 bounded unavailable 응답을 반환하며, 기존 SQLite MCP 검색·추천 경로는 그대로 유지됩니다. 사내 직무 매핑은 개인정보를 받지 않는 tenant-scoped 후보 overlay이고 사람의 승인 상태를 자동으로 부여하지 않습니다.
- **2026-09-11**: merge commit `6ecc45d49d3a8b945d506078ec0788c139573681`을 Vercel production에 반영했습니다. 기준 URL은 변경 없이 `https://ncs-mcp-bridge-mini2.vercel.app/api/mcp`이며, 배포 후 `initialize`, 공개 도구 7/7개, 실제 도구 호출 12건, 분석 모드 4종, `/api/health`, `/api/ready`를 다시 검증했습니다.
- **2026-09-09**: [Windows NCS DB 업데이트 빌더](docs/NCS_DATA_BUILDER.md)를 추가했습니다. `run_ncs_builder.bat`를 실행하면 **① 원본 변경분·온톨로지 → ② API 갱신 → ③ 경량 DB 생성 → ④ Vercel 반영**을 각각 별도 버튼으로 실행할 수 있습니다. 현재 작업은 처리 바이트·DB 페이지·API 페이지·능력단위 건수로 진행률을 표시하고, 하단 전체 진행률은 완료한 단계 수(`25% = 1/4단계`)를 표시합니다. 전체량이 알려지지 않은 계산은 임의 퍼센트 대신 작업명·경과시간을 표시합니다.
- **2026-09-09**: Builder의 원본 DB 검사에서 선택적 SQLite `dbstat` 진단 모듈이 없는 환경을 처리하고, API 갱신 오류에 실패 단계·원인을 표시하도록 수정했습니다. 원본과 이전 DB를 보존하고 검증된 별도 후보만 패키징·배포합니다.
- **2026-08-30**: 공개 MCP 기준 URL을 `https://ncs-mcp-bridge-mini2.vercel.app/api/mcp`로 일원화했습니다. 이전 구버전 엔드포인트 `https://ncs-mcp-bridge.vercel.app/api/mcp`는 현재 `404`로 종료되며 신규 연결에 사용하지 않습니다.
- **2026-08-30**: 서버가 사용하지 않는 독립 `GET /api/mcp` SSE 연결을 열어 둔 채 30초 뒤 종료되던 문제를 수정했습니다. 지원하지 않는 GET은 즉시 `405 Method Not Allowed`로 끝내고, `POST` 기반 `initialize`·`tools/list`·`tools/call` 계약은 유지합니다.
- **2026-08-30**: `ncs_search`·`ncs_unit_detail`·`ncs_training`·`ncs_analysis`의 도구 응답을 원시 JSON 문자열 대신 간결한 마크다운으로 제공합니다. 후속 호출에 필요한 `unit_code`·`element_id`·`criteria_id`·`training_course_id`·`concept_id`는 독립 식별자로 유지하고, 중복 `structuredContent`는 제거했습니다.
- **2026-08-30**: 전체 canonical `ncs.db`를 Vercel에 직접 싣지 않고, 온톨로지·KSA·수행준거·교육추천 근거를 포함한 compact SQLite(425,758,720 bytes)와 배포 ZIP(120,785,873 bytes)으로 만드는 결정론적 Builder·Refresh Builder를 정리했습니다.
- **2026-08-30**: Vercel 함수 검증기가 빌드 폴더의 물리 파일뿐 아니라 `.vc-config.json`의 `filePathMap`까지 확인하도록 강화했습니다. 원본 `.db`·SQLite sidecar·금지 디렉터리 참조가 하나라도 있거나 실제 매핑 총량이 상한을 넘으면 배포를 중단합니다.
- **2026-08-30**: Builder 릴리스 단계에 배포 후 원격 스모크 게이트를 추가했습니다. `GET 405 종료`, `initialize`, `tools/list`, 공개 7개 도구 호출, `ncs_analysis`의 `career_path`·`qualification`·`job_base`·`ontology` 4개 모드를 실제 URL에 대해 검증합니다.
- **2026-08-30**: qualification 스모크를 `광역 자격 조회 → 반환된 능력단위코드 정확 검색 → 해당 능력단위의 자격 조회` 체인으로 확장했습니다. 광역 결과만 존재하고 실제 단위별 조회가 깨진 배포는 승격하지 않으며, 검증 보고서에는 조회 코드와 응답 본문을 기록하지 않습니다.
- **2026-08-30**: 운영 스모크는 `scripts/verify_remote_mcp_transport.py`가 담당합니다. 스냅샷 테이블 누락, raw exception 노출, 공개 도구 응답 회귀가 발생하면 production 승격 전에 릴리스를 중단합니다. 데이터 갱신·배포 실행은 현재 Windows NCS Data Builder로 일원화되어 있습니다.
- **2026-08-30**: `initialize`의 `serverInfo.version`에 Git 커밋 SHA, Vercel 배포 ID 또는 스냅샷 해시를 포함해 신·구 배포를 식별할 수 있게 했습니다.
- **2026-08-30**: `ncs_analysis(mode="job_base")` 응답을 필드 화이트리스트와 링크 상한으로 제한하고, 원격 스모크에서 2,000자·1초 계약을 검사하도록 했습니다.
- **2026-08-30**: Vercel compact snapshot의 qualification 계약을 강화했습니다. `ncs_qualification_items`와 `ncs_unit_qualification_links`가 없거나 비어 있으면 패키지 검증과 production 승격이 실패합니다.
- **2026-08-30**: 저장소가 연결하는 NCS API·파일 원천을 전수 구분하고, 공식 레코드·엔드포인트·저장 테이블·이용조건을 [데이터 출처·이용조건 고지](DATA_SOURCE_NOTICE.md)에 기록했습니다.
- **2026-08-30**: 저장소 작성 코드·문서에는 [MIT License](LICENSE)를 적용하고, NCS 원천 데이터·배포 snapshot·OCR·vendor·제3자 자산의 별도 권리와 출처는 [NOTICE](NOTICE)와 데이터 출처 고지로 분리했습니다.

위 mini2 URL은 현재 공개 MCP의 canonical endpoint입니다. 원격 transport와 핵심 도구 계약은 자동으로
검증하지만, 전체 AI-HR 제품의 안정 릴리스 판정과 사람 검토가 필요한 데이터 판단은 별도 승인 절차로
남아 있습니다.

---

## 📊 검색 성능·배포 상태 (2026-09-13)

공개 서비스는 DB 용량을 늘리는 FTS 인덱스 대신, 질의를 토큰으로 분해해 단계적으로 완화하는 검색 경로를
사용합니다. 검색 순서는 `고특이도 실무어 intent alias → 문구 일치 → 토큰 AND → 확장 AND → 토큰 OR`이며,
intent alias가 없는 일반 질의는 기존 문구·토큰 순서를 그대로 사용합니다. 결과 유형별로 필요한 단계만 실행합니다.
따라서 `신입사원 채용 면접`, `데이터 분석가`, `품질관리 담당자 교육`처럼 띄어쓰기가 포함된 자연어
질의도 단일 부분문자열 일치에 의존하지 않습니다. 토큰 OR 결과가 요청 한도에 못 미치고 조사 제거가
안전한 경우에만 `morphology_fill` 후보를 기존 결과 뒤에 추가합니다. 조사 제거 뒤 `관리`·`업무`·`운영`·
`직무`·`실무`가 남는 경우에는 그 앞의 비범용 base를 능력단위명 또는 같은 능력단위의 기존 정확 alias에만
연결하며, 짧은 base를 정의·분류 전체로 확산하지 않습니다.
명시적인 `X 직무/업무 필요역량` 질의는 먼저 `대분류 → 중분류 → 소분류 → 세분류`의 정확하고 유일한
원천 분류 경로를 확인합니다. 경로가 확인되면 그 가지를 hard filter로 고정하고 능력단위·요소·수행준거·KSA로
내려가며, 정확한 범위를 확정할 수 없으면 다른 분야 결과를 섞지 않고 범위 확인이 필요하다고 응답합니다.

| 검증 항목 | 현재 결과 |
| --- | --- |
| 40개 고정 개발·회귀 질의 Hit@1 | `0.775` (`31/40`) |
| 40개 고정 개발·회귀 질의 Hit@3 | `0.875` (`35/40`, 0.7 게이트 통과) |
| 40개 고정 개발·회귀 질의 MRR@20 | `0.8244` |
| 51개 독립 holdout Hit@1 / Hit@3 / MRR@20 | `0.549 / 0.6078 / 0.5903` (목표 미달, 개선 계속) |
| 48개 비-holdout 조사 변형 Hit@3 | `0.5833 → 0.6250` (`+4.17%p`) |
| 48개 비-holdout 조사 변형 빈 응답 | `10 → 8` (`2건` 복구) |
| 162개 비-holdout 공식명 복합어 변형 Hit@1 / Hit@3 / MRR@20 | `0 / 0 / 0 → 1.0 / 1.0 / 1.0` |
| 2026-09-12 대표 12질의 검색 p50 | `853.123 ms → 323.885 ms` (`62.0%` 단축) |
| Top 10 결과 ID overlap | `1.0` |
| 대표 질의 zero-hit | `0건` |
| 2026-09-12 원격 readiness warm p50 | `1,434.019 ms → 235.134 ms` (`83.6%` 단축) |
| 새 preview 3회 첫 요청 p50 | `5,186.377 ms` |
| 첫 요청 중 snapshot bootstrap p50 | `4,554.211 ms` |
| 전체 단위 테스트(로컬) | 총 `2,515개` 통과, `4개 skip` |

`ncs_search`는 `offset`과 `next_offset`을 제공해 5건 이후 결과에도 접근할 수 있습니다.
`scope="all"`은 능력단위, 능력단위요소, 수행준거, KSA가 한 유형에 선점되지 않도록 유형별 결과를
균형 있게 구성합니다. 위 품질 수치는 실제 DB 코드로 실행하는 고정 40개 HR 실무 질의
개발·회귀(in-sample) 세트의 Hit@1·Hit@3·MRR@20입니다. 독립 holdout 수치는 별도 행에 분리했습니다.
holdout은 전후 비교에만 사용하며 결과를 보고 alias를 추가하지 않습니다. 이번 조사 보강은 holdout 집계에는
변화를 만들지 않았으므로 일반화 성능 목표는 아직 달성되지 않았습니다. 기존 50개 `candidate_eval`도 사람
정답 라벨이 없으므로 그 자료만으로 Recall, MRR, nDCG가 개선됐다고 주장하지 않습니다.

`context_text`와 `job_scope` 입력은 현재 공개 스키마에 있지만 랭킹에는 반영하지 않는 shadow 단계입니다.
공개 결과 순서와 명시적 분류 필터는 보존됐지만, resolver를 포함한 p50 오버헤드가 `110.595ms`이고
보조 SQL이 1회 늘어 승격 기준을 넘었으므로 public reranking은 HOLD 상태입니다.

Vercel에는 전체 원본 DB가 아니라 검증된 compact SQLite snapshot을 배포합니다. 현재 snapshot은
`456,929,280 bytes`, 압축 ZIP은 `120,158,362 bytes`입니다. 빌드 소프트 캡 `460 MB`까지
`3,070,720 bytes`, 하드 캡 `480 MB`까지 `23,070,720 bytes`의 여유가 있으며 현재 용량 게이트는
`within_budget`입니다.
FTS5 인덱스는 배포 시 `/tmp` 여유와 콜드스타트 안정성을 해칠 수 있어 현재 릴리스에는 포함하지 않았습니다.
무결성 확인을 위한 SHA-256과 `fsync`는 유지합니다.

readiness는 검증된 manifest의 물리·서비스 가능 행 수를 빠른 경로로 사용하고, override DB, manifest 불일치,
최소 행 수 미달 시 실제 SQL `COUNT`로 되돌아갑니다. production alias는 preview 성능 게이트를 통과한
배포에만 승격합니다.

### 공개 MCP 도구

| 도구 | 용도 |
| --- | --- |
| `ncs_search` | 분류·능력단위·요소·수행준거·KSA 통합 검색 |
| `ncs_unit_detail` | 능력단위 상세와 하위 근거 조회 |
| `ncs_training` | NCS 훈련과정과 연결 근거 조회 |
| `ncs_analysis` | 경력경로·자격·직업기초능력·온톨로지 분석 |
| `ncs_discover_tools` | 자연어 의도에 맞는 도구와 실행 경로 탐색 |
| `ncs_execute_tool` | 허용된 읽기 전용 도구의 메타 실행 |
| `recommend_training_for_task` | 과업·KSA 근거 기반 교육훈련 추천 |

공개 실행 경로는 읽기 전용입니다. `ksa_items.ksa_text_raw`를 수정하지 않으며, 사람의 명시적 결정 없이
`human_reviewed`, `accepted`, `reviewed` 상태를 기록하지 않습니다. 코드와 데이터의 권리 경계는
[코드 라이선스·데이터 출처·면책](#-코드-라이선스데이터-출처면책), [NOTICE](NOTICE),
[데이터 출처·이용조건 고지](DATA_SOURCE_NOTICE.md)를 따릅니다.

---

## 🌱 학습 지향 역량 시스템으로의 제품 방향

현재 구현된 제품은 조직의 직무를 NCS 과업·수행준거·KSA와 연결하고, 현재·목표 직무 사이에서 확인할
학습 항목과 교육과정을 추천하는 기반 엔진입니다. 이때 직무 간 KSA 차이는 구성원의 실제 약점 판정이
아니라 학습 설계를 위한 후보입니다. 구성원이 자신의 보유 역량과 지원이 필요한 영역을 안전하게 공개하고
학습 결과까지 관리하는 개인화 사내 시스템은 아직 구현 완료 상태가 아닙니다.

향후 제품은 `직무·과업 정의 → 본인만 보는 역량·학습 프로필 → 항목별 공유 동의와 수신자 미리보기 →
동의된 근거에 한한 갭 분석·교육 추천 → 구성원의 수정·이의제기·철회 → 코치·교육담당자의 사람 검토 →
소집단을 보호한 집계와 학습 효과 측정`을 하나의 흐름으로 개발합니다.

개인 프로필은 기본 비공개이며 공용 NCS 데이터와 Vercel snapshot에서 분리합니다. 미응답·정보 없음은
부족 역량으로 판정하지 않고, 현재 직무를 맡았다는 사실만으로 개인이 모든 관련 KSA를 보유했다고 자동
간주하지 않습니다. 공유를 거절해도 일반 NCS 탐색과 교육 검색을 사용할 수 있어야 합니다. 성과평가·징계·
보상·승진을 위한 약점 조회나 대량 추출은 제품 경계에서 제한하고, 자동 결과는 사람의 승인 상태로 승격하지
않습니다. 학습 지향 문화가 실제로 확보됐다는 판단은 코드 존재가 아니라 비보복 운영, 구성원의 통제권과
피드백, 사람 검토 증거로 확인합니다. 상세한 데이터 경계·위협모델·단계별 구현·수용 기준은
[학습 지향 개인 역량 프로필 개발 계약](docs/LEARNING_ORIENTED_SKILL_PROFILE.md)에 명시했습니다.

---

## 💡 이렇게 요청해 보세요

| 목적 | 예시 요청 |
| --- | --- |
| 직무기술서 | “인사 채용관리 담당자의 NCS 능력단위와 수행준거를 찾아 직무기술서 초안을 작성해줘.” |
| 구조화 면접 | “인력채용 능력단위의 수행준거와 KSA로 행동면접 질문과 평가요소를 만들어줘.” |
| 교육훈련 | “직원 교육훈련 계획 수립 업무에 필요한 부족 KSA와 적합한 훈련과정을 추천해줘.” |
| 경력개발·배치 | “노무관리에서 인사기획으로 이동할 때 공통역량과 추가 개발역량을 비교해줘.” |
| 조직 역량관리 | “우리 HR팀 직무군에 공통으로 필요한 역량과 직무별 차이를 NCS 근거로 정리해줘.” |

> ℹ️ HRMCP의 결과물은 **교육·업무 설계를 돕는 참고 자료**이며, 공식 자격·채용·법적·규정 판단을
> 대체하지 않습니다.

---

## 🔌 HRMCP 연결 방법

ChatGPT와 Claude는 HRMCP를 연결하는 메뉴와 명칭이 다릅니다. 사용하는 플랫폼의 연결 절차를
선택해 진행하세요. 연결을 마친 뒤의 실제 요청 방법은 [HRMCP 사용 방법](#-hrmcp-사용-방법)에서
확인할 수 있습니다.

### ChatGPT 연결

> 아래 이미지는 ChatGPT Pro 화면 기준입니다. 예시 화면에서는 플러그인 이름을 `rmcp`로
> 만들었지만, 이름은 **HRMCP** 또는 본인이 사용하기 편한 이름으로 지정하면 됩니다.

#### 1️⃣ 왼쪽 아래 프로필을 클릭합니다

![프로필 클릭](docs/images/setup/0_1.jpg)

#### 2️⃣ 메뉴에서 설정으로 이동합니다

![설정 클릭](docs/images/setup/0_2.jpg)

#### 3️⃣ 설정 → 플러그인으로 이동한 뒤, 목록을 아래로 내립니다

![플러그인 이동](docs/images/setup/0_3.jpg)

#### 4️⃣ 목록 맨 아래의 개발자 모드를 클릭합니다

![개발자 모드 진입](docs/images/setup/0_4.jpg)

#### 5️⃣ 개발자 모드를 ON으로 변경합니다

![개발자 모드 ON](docs/images/setup/0_5.jpg)

#### 6️⃣ 왼쪽 메뉴에서 플러그인을 선택합니다

![플러그인 메뉴](docs/images/setup/1.jpg)

#### 7️⃣ 오른쪽 위의 `+` 버튼을 클릭합니다

![플러그인 추가 버튼](docs/images/setup/1_1.jpg)

#### 8️⃣ 새 플러그인 정보를 입력합니다

- **이름:** `HRMCP` 또는 본인이 사용하기 편한 이름
- **연결 방식:** `서버 URL`
  - **서버 URL:** 아래 HTTPS MCP 주소를 그대로 복사해 붙여넣기

    ```text
    https://ncs-mcp-bridge-mini2.vercel.app/api/mcp
    ```

- **인증 방식:** `인증 없음` 선택 (드롭다운의 `∨`를 클릭해 선택)

![새 플러그인 정보 입력](docs/images/setup/1_2.jpg)

#### 9️⃣ 안내사항 확인란에 체크한 뒤 만들기를 클릭합니다

![안내 체크 후 만들기](docs/images/setup/1_3.jpg)

#### 🔟 연결하기를 누르면 설정이 완료됩니다

![연결하기](docs/images/setup/1_4.jpg)

### Claude 연결

Claude의 원격 MCP 커스텀 커넥터는 Free·Pro·Max·Team·Enterprise 플랜에서 사용할 수
있습니다. Free 플랜은 커스텀 커넥터를 1개까지 등록할 수 있습니다.
자세한 최신 정책은 [Anthropic 공식 안내](https://support.claude.com/ko/articles/11175166-%EC%9B%90%EA%B2%A9-mcp%EB%A5%BC-%EC%82%AC%EC%9A%A9%ED%95%98%EC%97%AC-%EC%82%AC%EC%9A%A9%EC%9E%90-%EC%A0%95%EC%9D%98-%EC%BB%A4%EB%84%A5%ED%84%B0-%EC%8B%9C%EC%9E%91%ED%95%98%EA%B8%B0)와
[Claude Academy의 현재 화면 안내](https://academy.claude.com/tutorials/connect-your-tools-to-unlock-a-smarter-more-capable-ai-companion)를 참고하세요.

#### 개인 플랜 (Free·Pro·Max)

Claude 웹 화면에서 다음 순서에 따라 커넥터를 직접 추가합니다.

1. Claude 홈 화면 왼쪽 아래의 **프로필**을 클릭합니다.

![Claude 홈 화면에서 왼쪽 아래 프로필 메뉴를 여는 위치](docs/images/setup/claude_hrmcp_setup_01_profile.png)

2. 열린 프로필 메뉴에서 **설정**을 선택합니다.

![Claude 프로필 메뉴에서 설정을 선택하는 화면](docs/images/setup/claude_hrmcp_setup_02_settings.png)

3. 설정 창 왼쪽 아래의 **사용자 지정**을 선택합니다.

![Claude 설정 창에서 사용자 지정을 선택하는 화면](docs/images/setup/claude_hrmcp_setup_03_customize.png)

4. 사용자 지정 화면 왼쪽에서 **커넥터**를 선택합니다.

5. 오른쪽 위의 **추가**를 클릭합니다.

![Claude 사용자 지정의 커넥터 화면에서 추가 버튼을 선택하는 화면](docs/images/setup/claude_hrmcp_setup_04_connectors.png)

6. **추가**를 누르면 **커스텀 커넥터 추가** 창이 열립니다. **이름**에 `HRMCP`를 입력합니다.

7. **원격 MCP 서버 URL**에 `https://ncs-mcp-bridge-mini2.vercel.app/api/mcp`를 입력합니다.

8. 두 값을 확인한 뒤 **계속**을 클릭해 등록을 진행합니다.

![Claude 커스텀 커넥터 추가 창에서 이름과 원격 MCP 서버 URL을 입력하는 화면](docs/images/setup/claude_hrmcp_setup_05_add_custom_connector.png)

**계속**을 누르면 연결 설정이 진행됩니다. 확인 단계가 표시되면 내용을 확인해 등록을
마칩니다. HRMCP는 인증이 필요하지 않으므로 OAuth Client ID·Secret은 입력하지 않습니다.

> Claude 업데이트나 계정 유형에 따라 메뉴 배치나 버튼 이름이 조금 달라질 수 있습니다.

#### Team·Enterprise 플랜

1. 조직의 **Owner 또는 Primary Owner**가 **Organization settings → Connectors** 로
   이동합니다.
2. **Add → Custom → Web** 을 선택하고 위의 원격 MCP 서버 URL을 입력합니다.
3. OAuth 고급 설정은 비워 둔 채 **Add** 를 클릭해 조직에 등록합니다.
4. 각 구성원은 **설정 → 사용자 지정 → 커넥터**에서 `HRMCP`를 찾아 **연결**을 클릭합니다.

---

## 💬 HRMCP 사용 방법

위의 ChatGPT 또는 Claude 연결 절차를 마친 뒤 HRMCP를 사용할 수 있습니다. 연결 방식은
플랫폼마다 다릅니다. 아래는 ChatGPT에서 구조화된 행동면접 질문을 만들고, Claude에서 NCS
직무기술서를 DOCX 문서로 만드는 사용 예시입니다.

### ChatGPT에서 사용하기 — 구조화된 행동면접 질문

채팅창에서 등록한 이름 앞에 `@`를 붙여 HRMCP를 선택한 뒤 요청합니다. 연결 이름을
`HRMCP`로 만들었다면 다음과 같이 입력합니다.

![@HRMCP를 선택하는 방법](docs/images/setup/1_5.jpg)

```text
@HRMCP 첨부한 채용공고와 직무기술서를 참고해 구조화된 행동면접 질문 10개를 작성해줘.
각 질문별 평가요소, 추가 질문, 긍정적·부정적 행동지표도 함께 제시해줘.
```

![ChatGPT에서 HRMCP를 활용한 면접 질문 생성 결과](docs/images/setup/1_6.jpg)

### Claude에서 사용하기 — NCS 직무기술서 작성

새 대화에서 다음과 같이 요청합니다. Claude에서는 `@HRMCP`를 붙일 필요가 없습니다.
도구 사용 권한 확인 창이 표시되면 내용을 확인한 뒤 허용합니다.

```text
HRMCP 인사기획 직무기술서를 워드로 만들어줘
```

아래 화면은 Claude가 HRMCP에서 인사기획 능력단위와 NCS 근거를 조회한 뒤 직무기술서를
DOCX 문서로 생성하고, 결과를 미리 보거나 다운로드하는 활용 예시입니다.

![Claude에서 HRMCP를 활용해 NCS 직무기술서를 생성하고 다운로드하는 화면](docs/images/setup/claude_hrmcp_use_02_job_description.png)

> HRMCP가 호출되지 않으면 **설정 → 사용자 지정 → 커넥터**에서 `HRMCP` 행의 체크 표시를
> 확인하세요. 대화 입력창에 도구 선택 메뉴가 표시되는 계정에서는 해당 메뉴에서도 HRMCP가
> 허용되어 있는지 확인합니다.

---

## ⚠️ 이용 시 주의사항 (공개 테스트 단계)

현재 HRMCP는 **공개 테스트 단계**입니다.

- **개인정보, 지원자 정보, 기관 내부자료, 비공개 문서** 등 민감한 정보는 제외하고 사용해 주세요.
- 결과물은 참고용이며, 공식 자격·채용·법적·규정 판단의 근거로 사용하지 마세요.
- 서비스 안정성 및 데이터는 예고 없이 변경될 수 있습니다.

---

## 🧩 연결 정보 요약

| 항목 | 값 |
| --- | --- |
| MCP 서버 URL | `https://ncs-mcp-bridge-mini2.vercel.app/api/mcp` |
| 인증 | 없음 (Auth: None) |
| 상태 확인(health) | `https://ncs-mcp-bridge-mini2.vercel.app/api/health` |
| 준비 확인(ready) | `https://ncs-mcp-bridge-mini2.vercel.app/api/ready` |

ChatGPT Custom GPT(Agent/Tools) 설정에서는 아래 JSON의 `url`만 넣으면 됩니다.

```json
{
  "mcpServers": {
    "hrmcp": {
      "url": "https://ncs-mcp-bridge-mini2.vercel.app/api/mcp"
    }
  }
}
```

---

## 🗂️ HRMCP가 하는 일 (기술 개요)

- NCS 계층 구조(분류·능력단위·능력단위요소·수행준거)와 원천 KSA 행을 원본 그대로 보존합니다.
- 원천 KSA 텍스트를 덮어쓰지 않고 KSA/과업 온톨로지 테이블을 구축합니다.
- 교육과정의 목표·시간·방법·시설과 NCS 단위 근거를 과업/KSA 추천 근거에 연결합니다.
- 경력 전환 및 과업 기반 교육훈련 추천을 간결한 근거 요약과 함께 제공합니다.
- NCS 경력경로, 자격항목 API, 직무기초능력 API에서 보조 근거를 추가합니다.
- NCS 구조 검색, 온톨로지 조회, 교육과정 검색, AI-HR 교육경로 설계를 위한 MCP 도구를 노출합니다.
- 자연어 요청을 공개 도구로 라우팅하고 운영자 워크플로를 차단하는 읽기 전용 기관 챗봇
  참고 UI/API를 별도로 포함합니다.

> 현재 제품 범위는 NCS 중심입니다. SQF 및 NCS 학습모듈 플로우는 과거 테이블이나 호환성
> 코드에 남아 있을 수 있으나, 운영자가 명시적으로 다시 활성화하지 않는 한 레거시/참고
> 영역입니다.

### 데이터 흐름

```text
NCS Excel/원천 데이터
  → 분류(classifications)
  → 능력단위(competency_units)
  → 능력단위요소(competency_elements)
  → 수행준거(performance_criteria)
  → 원천 KSA 행(raw KSA)
  → KSA/과업 온톨로지 → 추천 근거(recommendation evidence)
```

---

## 🖥️ 셀프 호스팅 / 배포 (관리자용)

### 로컬 실행 (읽기 전용 기본값)

로컬 런처는 기본적으로 읽기 전용 SQLite 서빙으로 동작하며 운영자 MCP 도구를 감춥니다.

```powershell
.\run_ncs_mcp_http.cmd     # 로컬 HTTP MCP 서버
.\run_ncs_institutional_chat.cmd   # 기관 챗봇 참고 UI/API
```

- 로컬 HTTP: `http://127.0.0.1:8766/mcp` / health `http://127.0.0.1:8766/health`
- 참고 챗 UI: `http://127.0.0.1:8780/`

기본적으로 루프백 외 바인딩은 거부됩니다. 강화된 컨테이너 예시는
`deploy/compose.internal.yml`에 있으며, 신원·TLS·사용자 권한은 기관 게이트웨이
(`docs/INSTITUTIONAL_CHATBOT_SELF_HOST_GUIDE.md`)에서 처리해야 합니다.

### Vercel 배포 (Streamable HTTP)

ChatGPT 연결은 주소 한 줄(`/api/mcp`)만 넣으면 됩니다. 전체 배포 가이드는
`docs/README_VERCEL_HTTPS.md`를 참고하세요.

- 기준 입력은 운영자가 준비한 단일 canonical DB `data/processed/ncs.db`
  (12,680,593,408 bytes)입니다. Publisher가 이를 stage·verify한 뒤 compact SQLite
  (478,756,864 bytes)와 `api/ncs_ontology_compact.zip`(128,894,654 bytes), manifest
  쌍을 원자적으로 publish합니다. 실패하면 기존 쌍을 rollback합니다.
- `deploy/vercel_mcp_app/vercel.json`은 함수 진입점(`api/index.py`)과 ZIP/manifest
  포함 규칙을 정의합니다. 측정된 production function file mapping은 178,567,716 bytes,
  1,848개 파일이며 500,000,000 bytes 상한 검사를 통과했습니다.
- `api/mcp.py`는 시작 시 ZIP과 manifest를 검증한 뒤 `/tmp/ncs_ontology_compact.db`에
  DB를 materialize하여 read-only로 엽니다. 요청 시 NCS API를 수집하거나 AI 모델을
  호출하지 않습니다. `NCS_DB_URL`은 표준 배포 의존성이 아닙니다.
- 현재 production MCP URL은 `https://ncs-mcp-bridge-mini2.vercel.app/api/mcp`입니다. 배포별
  식별자와 서버 빌드 식별자는 릴리스마다 달라지므로 원격 `initialize` 및 배포 검증 보고서에서 확인합니다.

Vercel 런타임 설정은 `deploy/vercel_mcp_app/vercel.json`에 포함되어 있습니다.

```text
NCS_MCP_READ_ONLY=1
NCS_MCP_ENABLE_OPERATOR_TOOLS=0
NCS_MCP_DISABLE_DNS_REBINDING_PROTECTION=1
NCS_MCP_STREAMABLE_HTTP_PATH=/mcp
NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS=2
NCS_MCP_READINESS_EXTRA_TABLES=ontology_concepts,...,ncs_unit_standard_training
```

production 반영은 아래 단일 진입점에서 선택 버전의 ① 원본·온톨로지 → ② API
갱신 → ③ 경량 DB 생성·검증 → ④ Vercel 반영을 순서대로 수행합니다.

```powershell
.\run_ncs_builder.bat
```

새 원천 DB도 Builder에서 전체 Excel과 비교 기준 DB를 선택해 별도 후보로 만듭니다.
검증된 후보의 source identity, compact ZIP/manifest, preview MCP, production MCP가 모두
일치할 때만 기준본을 승격합니다. Publisher와 low-level snapshot 스크립트는 이 단계의
내부 구현이며 별도 운영 명령으로 실행하지 않습니다. 자격/NCS006 API는 Builder 자동
갱신에 포함하지 않고 기존 retry-hygiene, coverage-plan, checkpoint, operator-ready
조건을 만족한 경우에만 별도 운영자 절차로 다룹니다.

### API 키 발급

NCS API 키는 이 저장소 외부에서 발급합니다. 공공데이터포털(HRDK/NCS API 호스팅)에서 필요한
서비스에 접근을 신청하고, 발급된 서비스 키를 로컬 `.env`에 넣습니다.

- `NCS_SERVICE_KEY` — NCS 참조 API 키
- `NCS_TRAINING_COURSE_SERVICE_KEY` — NCS 교육과정 API 키
- `NCS_QUALIFICATION_SERVICE_KEY` — NCS 단위 자격항목 API 키
- `NCS_JOB_BASE_SERVICE_KEY` — NCS 직무기초능력 API 키

> `.env`는 커밋하지 마세요. 실제 키를 보고서·로그·이슈·스크린샷에 붙여넣지 마세요.

---

## 📄 코드 라이선스·데이터 출처·면책

저장소의 코드 라이선스와 외부 데이터 이용조건은 서로 다른 권리 경계입니다.

| 대상 | 적용 라이선스·이용조건 |
| --- | --- |
| HRMCP가 작성한 소스 코드와 문서 | [MIT License](LICENSE). 복제·수정·배포 시 저작권 고지와 MIT 허가문을 포함해야 합니다. |
| NCS 원천 데이터와 공공데이터 API 응답 | 저장소 MIT 적용 대상이 아닙니다. 원 제공기관의 최신 이용조건, 출처 표시, API 승인·트래픽 조건과 제3자 권리가 별도로 적용됩니다. |
| 가공 DB, ontology index, compact SQLite와 Vercel snapshot | 원천을 가공했다는 이유로 원천의 이용조건이 사라지거나 더 넓은 재배포 권리가 생기지 않습니다. |
| OCR 모델, vendor 코드, 다운로드 문서·이미지·영상 | 파일별 라이선스와 원 권리자의 조건이 적용됩니다. `3d-force-graph` 고지는 [`scripts/vendor/3d-force-graph-LICENSE.txt`](scripts/vendor/3d-force-graph-LICENSE.txt)에 보존합니다. |

재배포하거나 서비스에 포함하기 전에는 [NOTICE](NOTICE)와
[데이터 출처·이용조건 고지](DATA_SOURCE_NOTICE.md)를 함께 확인해야 합니다. 저장소 코드·문서를
배포할 때는 `LICENSE`와 `NOTICE`를 포함하고, DB·snapshot·외부 자료는 해당 원천의 최신 공식 조건과
파일별 권리를 다시 확인해야 합니다. API 레코드의 이용조건이 NCS 누리집에서 직접 받은 별도 파일이나
문서까지 자동으로 포괄한다고 간주하지 않습니다.

생성 운영 DB와 배포 snapshot ZIP은 Git 소스 파일로 추적하지 않습니다. 다만 Vercel 릴리스는 검증된
compact SQLite snapshot을 별도 배포 산출물로 스테이징하고, 런타임에서 읽기 전용으로 materialize할 수
있습니다. 따라서 **Git 미포함**과 **배포 산출물 사용**은 서로 다른 경계입니다.

### 연결된 외부 데이터 원천

| 구분 | 공식 출처·연결 | 현재 역할 |
| --- | --- | --- |
| NCS 정보망 Excel DB | NCS 누리집에서 취득한 `ncs_info_network_db_2026_02.xlsx` | 분류·능력단위·요소·수행준거·원천 KSA의 canonical 기반 |
| NCS 기준정보 API | [공공데이터포털 15128213](https://www.data.go.kr/data/15128213/openapi.do), `hrdkapi/NCS004·005·006` | 직무·능력단위 정의 보강과 요소 검증 |
| NCS 훈련과정 API | [공공데이터포털 15086447](https://www.data.go.kr/data/15086447/openapi.do), `ncsTrainingCource/openapi18` | 훈련목표·시간·시설·방법을 교육추천 근거로 연결 |
| 능력단위별 자격 API | [공공데이터포털 15074404](https://www.data.go.kr/data/15074404/openapi.do), `ncsClCdJm/getNcsClCdJmList` | 능력단위와 자격 종목의 보조 근거 |
| NCS 직업기초능력 API | [공공데이터포털 15086440](https://www.data.go.kr/data/15086440/openapi.do), `ncsJobBase/openapi19` | 공통·부족 기초역량의 보조 근거 |
| NCS 경력개발경로 CSV | NCS 누리집 파일을 `ncs_career_paths`로 import | 직무 전환·성장 단계의 보조 근거 |

CQ-Net NCS 관련 정보, 학습모듈, SQF 관련 API·자료실 코드는 레거시·참조용이며 현재 공개 HRMCP의
기본 추천 경로에 사용하지 않습니다. API별 공식 이용허락 표시, 파일별 공공누리 확인 상태, 코드
연결 지점과 저장 테이블은 [데이터 출처·이용조건 고지](DATA_SOURCE_NOTICE.md)에 하나씩 정리했습니다.
공공데이터포털 API 레코드의 `이용허락범위 제한 없음` 표시는 해당 API 레코드에 대한 확인이며,
NCS 누리집에서 직접 받은 Excel·CSV·PDF·이미지·자료실 첨부파일에 자동으로 확대 적용하지 않습니다.

HRMCP의 추천·생성 결과는 교육·업무 설계를 돕는 참고 자료이며, 공식 NCS 정의·자격 인정·채용·법적·규정
판단이 아닙니다. NCS 원천 데이터의 권리는 한국산업인력공단 등 각 원 권리자에게 있습니다.

---

## 🧠 온톨로지로 HRMCP가 달라지는 점

![NCS DB에 온톨로지를 연결해 직무 중심 HR에 스킬 관점을 더하고 채용·배치·경력개발·교육·조직 역량관리·신직무 설계를 지원하는 변화](docs/images/hrmcp_ontology_hr_value.jpg)

### 핵심 변화: 검색 DB에서 관계 기반 HR 지식 구조로

기존 NCS DB는 직무, 능력단위, KSA, 수행준거처럼 **무엇이 있는지 찾고 개별 정보를 조회하는
데 강점**이 있습니다. HRMCP의 온톨로지 DB는 이 원천 정보를 바꾸지 않고 별도의 개념·링크·관계
테이블을 더해 **직무 ↔ 능력단위 ↔ 수행준거 ↔ KSA ↔ 교육과정**을 연결해서 탐색할 수 있게
합니다. 즉, 직무 분류를 없애는 것이 아니라 직무 중심 NCS 위에 스킬 관점을 추가하는 구조입니다.

| 기존 NCS DB 활용 | 온톨로지 확장 후 활용 |
| --- | --- |
| 직무명과 정보 단위별 개별 조회 | 직무·역량·KSA·수행준거 사이의 관계 탐색 |
| 검색어와 일치하는 항목 확인 | 입력한 역량에서 관련 NCS 직무와 능력단위로 탐색 확장 |
| 각 직무의 요구사항을 따로 비교 | 직무 간 공통역량·부족역량과 전이 가능한 KSA 분석 |
| 과정명이나 분류 중심 교육 검색 | 부족 KSA를 수행준거·훈련목표·수준·시간·방법과 연결 |
| NCS에 존재하는 분류 범위 중심 활용 | 여러 NCS 근거를 조합한 탐색적 신직무 프로파일 설계 지원 |

대표적인 활용 흐름은 **역량 입력 → 관계 탐색 → 관련 직무·역량 도출 → KSA Gap 확인 →
교육·경력개발 지원**입니다. 이 흐름은 문자열이 비슷하다는 이유만으로 결론을 내리는 방식이 아니라,
실제 NCS 능력단위·수행준거·KSA와 저장된 온톨로지 관계를 근거로 결과를 추적할 수 있게 합니다.

### 핵심 데이터 규모

| 데이터 | 현재 규모 | 활용 |
| --- | ---: | --- |
| NCS 능력단위 | 13,435건 | 직무와 가장 가까운 능력단위를 찾는 기본 축 |
| 수행준거 | 196,658건 | 면접 질문·직무기술서·교육 추천의 세부 근거 |
| 원천 KSA | 574,279건 | 직무 수행에 필요한 지식·기술·태도 원문 근거 |
| 온톨로지 개념 노드 | 533,909건 | 표현을 대표 개념으로 연결하고 인접 역량을 탐색하는 축 |
| 수행준거–개념 연결 | 3,025,498건 | 수행준거와 관련 KSA 개념을 잇는 중복 제거 논리 관계 |
| 개념 간 온톨로지 관계 | 3,235,434건 | 지식·기술·태도 개념 사이를 연결하는 논리 관계 |
| 교육과정 | 11,819건 | 부족 역량과 연결해 검토하는 교육훈련 과정 |

두 핵심 관계 계층은 합계 **6,260,932건**입니다. 경량판에서는 이 관계를 삭제하지 않고 여러 관계
ID를 한 행의 압축 목록(posting)에 묶어 저장하므로, SQLite의 물리 행 수와 위의 논리 관계 건수는
다릅니다.
건수는 현재 검증된 배포 데이터 기준이며, 원 NCS 데이터와 API 자료가 갱신되면 달라질 수 있습니다.

### Gold LPG·Neo4j 운영 경계

Gold LPG는 SQLite의 NCS·온톨로지 근거를 LLM/MCP가 짧은 탐색으로 회수할 수 있게 만든 **선택형
read model**입니다. 현재 Builder에서 검증한 `serving_core`는 `882,394`개 노드와 `4,465,577`개
관계이며, 수행준거·능력단위요소·KSA 개념 `778,187`건에 동일한 1,024차원 임베딩을 적용했습니다.
79개 shard는 모두 재개 가능한 방식으로 적재됐고 cosine vector index 3개를 생성했습니다.

| 계층 | 역할과 현재 운영 상태 |
| --- | --- |
| **SQLite canonical DB** | Bronze/Silver 전처리와 전체 온톨로지·추천 근거의 권위 데이터. 원천 KSA와 사람 검토 상태를 보존합니다. |
| **Neo4j Gold LPG** | 직무→KSA, 사내 직무→NCS 직무→KSA 같은 1~2홉 탐색과 시맨틱 벡터 검색을 위한 로컬/엔터프라이즈 선택 경로입니다. 실패하면 폐기·재생성할 수 있습니다. |
| **공개 Vercel MCP** | 현재는 Neo4j 접속정보 없이 compact SQLite를 읽기 전용으로 서비스합니다. 따라서 Gold가 꺼져 있어도 기존 URL과 검색·추천 도구가 중단되지 않습니다. |
| **InternalJobRole overlay** | 조직이 제공한 비개인 역할만 후보로 매핑합니다. 현재 검증 실행에는 승인된 사내 역할 데이터가 없어 overlay가 비활성이고, 매핑 결과를 자동 승인하지 않습니다. |

Gold의 고정 스키마, 안전한 dry-run/apply, 임베딩·vector index, Builder와 MCP 연결 방법은
[Neo4j Gold LPG 운영 가이드](docs/NEO4J_GOLD_LPG.md)에 정리했습니다.

### HR에서 달라지는 6가지 활용

| HR 업무 | 온톨로지가 지원하는 작업 |
| --- | --- |
| **채용 고도화** | 직무기술서, 구조화 행동면접 질문, 평가요소와 행동지표를 수행준거·KSA 근거에 연결 |
| **배치·이동 지원** | 현재 역량과 인접 직무의 공통 KSA를 비교하고 이동 후보와 추가 확인 항목을 탐색 |
| **경력개발 지원** | 목표 직무 대비 보유·부족 역량을 구분해 업스킬링·리스킬링 경로 초안 작성 |
| **교육 추천** | 부족 KSA를 교육과정의 훈련목표·수준·시간·방법·시설 근거와 연결해 과정 묶음 검토 |
| **조직 역량관리** | 팀·조직에 필요한 공통역량 후보와 직무군별 역량 구조를 파악하는 초안 제공 |
| **신직무 설계 지원** | HR Analytics, AI HR처럼 여러 NCS 영역에 걸친 역할의 근거를 조합해 탐색적 프로파일 설계 |

온톨로지는 채용·배치·승진을 자동 판정하지 않습니다. 관계 탐색 결과와 추천은 HR 담당자가 검토할
수 있는 **근거와 초안**이며, 조직별 중요도·직급 수준·보유역량·운영 여건은 별도로 확인해야 합니다.

### NCS에 없는 신직무의 탐색적 설계

HRMCP는 `HR Analytics`처럼 NCS에 동일 명칭의 분류·능력단위가 없는 역할에 대해서도 `인사기획`,
`인사평가`, `통계조사`, `빅데이터분석`, `빅데이터 분석 결과 시각화` 등 실제 NCS 범위의 근거를
각각 조회할 수 있습니다. HR 담당자나 외부 에이전트가 이 조회 결과의 능력단위·수행준거·KSA를
조합하면 **탐색적 신직무 프로파일 초안**으로 활용할 수 있습니다.

![NCS에 없는 HR Analytics 신직무를 관련 NCS 근거와 온톨로지 관계로 탐색적으로 설계하는 흐름](docs/images/hrmcp_new_job_ontology.png)

- **NCS 직접 근거**: 실제 능력단위, 능력단위요소, 수행준거, KSA, 훈련과정
- **온톨로지 기반 연결**: 저장된 개념·별칭·관계를 따라 찾은 인접 근거
- **모델·조직 제안**: 역할 구조, 중요도, NCS 밖의 추가 역량

현재 공개 MCP가 신직무를 한 번에 자동 분해하거나 조직별 프로파일을 DB에 저장하는 것은 아닙니다.
여러 조회 결과는 ChatGPT·Claude 같은 외부 에이전트와 HR 담당자가 조합하며, 포함 범위·중요도·
역할 수준·조직 고유 역량은 사람이 확정해야 합니다. 결과는 공식 NCS 정의나 채용 판정이 아니라
직무기술서·역량모델·교육체계 설계를 위한 검토용 초안입니다.

### 경량 배포와 데이터 갱신

Vercel에는 전체 운영 DB 대신 온톨로지와 교육 추천에 필요한 **500MB 이하의 읽기 전용 경량
스냅샷**을 배포합니다. Builder와 Vercel 런타임은 AI 모델을 실행하거나 요청 시점에 NCS API를
수집하지 않으며, 원 데이터 갱신·변경 감지·검증·배포는 별도의 재현 가능한 파이프라인에서
처리합니다.

릴리스는 추적된 파일만 복사한 clean staging에서 조립하며, 실제 Vercel `filePathMap`에서 원본 DB가
0건인지 확인합니다. 현재 검증된 함수 번들은 169,901,404 bytes이고, 런타임에 펼쳐지는
SQLite는 456,929,280 bytes입니다. Builder의 480 MB 하드 캡까지 23,070,720 bytes가 남아 있으며,
향후 데이터 증가 시에도 용량 게이트와 `/tmp` 사용량을 계속 확인합니다.

- [경량 DB Builder·Refresh Builder·Vercel 배포 절차](docs/VERCEL_SNAPSHOT_BUILDER.md)
- [Vercel 배포 구조·전체 포함 데이터·운영 범위](docs/README_VERCEL_HTTPS.md)
- [원천 데이터·온톨로지 계층·불변조건](ARCHITECTURE.md)

경량판도 원천 KSA를 덮어쓰지 않고, 검토되지 않은 정의나 후보를 자동 승인하지 않습니다.
