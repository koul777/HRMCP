# 3단계 — 소규모 결함 정리

작성일: 2026-09-11

## 원인 분석과 변경 계획

### 1. 능력단위요소 수준 `0`

- `preprocess_excel.py`는 Excel의 `능력단위요소수준`을 `element_level_raw`에 그대로 저장하고, `collect_api.py`는 NCS006의 `COMPE_UNIT_FACTR_LEVEL`을 `api_element_level`에 저장한다. 컬럼 매핑은 정상이다.
- 실 DB 47,620개 요소 중 원천 수준이 `0`인 행은 17,364개이며, API와 연결된 행 가운데 원천/API가 모두 `0`인 행은 17,115개이다. 원천과 API의 수준 불일치는 0건이다.
- 인사·조직(`020202`) 능력단위의 단위 수준은 3~6으로 존재하지만 요소 수준은 원천과 API 모두 `0`이다. 따라서 `0`은 요소별 수준이 제공되지 않은 원천 placeholder로 판단한다.
- 원천 컬럼과 DB는 변경하지 않는다. `get_unit_structure`/`ncs_unit_detail` 공개 응답에서 `None`, 빈 문자열, `0`만 `-`로 표시하고 유효한 1~8 값은 유지한다. 로컬·Vercel 서버 미러를 함께 수정하고 회귀 테스트를 추가한다.

### 2. 개발·수집 의존성 분리

- `PyMuPDF(fitz)`, `pypdf`, `pytesseract`, `olefile`, `Pillow(PIL)`은 `preprocess_sqf_documents.py`의 레거시 문서 수집 경로에서 지연 import된다. `pypdf`는 별도로 `scripts/ncs_learning_module_file_probe.py`에서도 사용한다.
- 공개 서버 런타임(`server.py`, `api/`)은 이 패키지를 직접 import하지 않는다. Vercel 미러의 `pyproject.toml`/`requirements.txt`도 이미 이 패키지를 포함하지 않는다.
- 다섯 패키지를 루트 `pyproject.toml`의 `ingest` 선택 그룹으로 이동한다. `pytest`는 `requirements.txt`에서 제거하고 `dev = ["pytest>=7,<9"]`로 이동한다.
- CI는 `pip install -e ".[dev]"`로 바꾼다. 루트 `requirements.txt`는 전체 수집 작업의 호환 설치 목록으로 유지하되 `pytest`만 제거한다. Docker는 기존 COPY 계약을 유지하면서 `pyproject.toml`의 기본 런타임 의존성만 설치한다.

### 3. Vercel 배포 크기·콜드 스타트 기준선

- 현재 Vercel compact archive: 120,785,873 bytes, 압축 해제 DB: 425,758,720 bytes.
- 변경 전 로컬 콜드 경로 3회: p50 2,082.380 ms, 평균 2,123.868 ms.
- Vercel 전용 패키지는 이미 문서 수집 의존성을 제외하므로 이번 의존성 재분류에 따른 Vercel archive 크기 변화는 없어야 한다. 변경 후 동일 명령으로 재측정한다.
- 원격 fresh-instance 수치는 배포를 새로 만들지 않는 한 확보할 수 없으므로, 로컬 재현 수치와 실제 배포 후 원격 연결 검증을 구분해 기록한다.

### 4. DNS rebinding 보호

- 현재 `api/mcp.py`는 `NCS_MCP_DISABLE_DNS_REBINDING_PROTECTION` 기본값이 `1`이어서 보호가 꺼져 있다.
- 실제 production 도메인은 `ncs-mcp-bridge-mini2.vercel.app`이다. Production 환경에 `NCS_MCP_ALLOWED_HOSTS=ncs-mcp-bridge-mini2.vercel.app`와 `NCS_MCP_DISABLE_DNS_REBINDING_PROTECTION=0`을 설정하는 방안을 검증한다.
- 설정 적용 후 실제 MCP initialize/tools 요청을 점검한다. 원격 Claude/Codex 호환 경로가 실패하면 즉시 이전 설정으로 원복하고 원인을 기록한다.

## 구현·검증 결과

### 변경 파일

- `src/ncs_mcp/server.py`
- `deploy/vercel_mcp_app/src/ncs_mcp/server.py`
- `tests/test_public_mcp_payload_contracts.py`
- `pyproject.toml`, `requirements.txt`, `uv.lock`
- `.github/workflows/ci.yml`
- `CHANGELOG.md`
- `reports/debug_2026-09/03_*`

### 구현 결과

- `get_unit_structure`가 원천/API 요소 수준의 `None`, 빈 문자열, `0`을 공개 표시값 `-`로 변환한다. 유효한 수준 값과 DB 원문은 변경하지 않는다.
- PDF/OCR 관련 다섯 패키지를 `project.optional-dependencies.ingest`로, pytest를 `dev`로 분리했다. CI는 `.[dev]`를 설치한다.
- 기본 패키지 설치 dry-run 결과에 `Pillow`, `PyMuPDF`, `pypdf`, `pytesseract`, `olefile`, `pytest`가 포함되지 않음을 확인했다.
- Docker는 계속 `pip install .`을 사용하므로 기본 런타임 그룹만 설치한다. 기존 배포 계약에 따라 `requirements.txt` 파일 복사는 유지하지만 설치에는 사용하지 않는다.
- `CHANGELOG.md`에 1~3단계 변경을 기록했다.

### 크기·성능 비교

| 항목 | 변경 전 | 변경 후 | 변화 |
|---|---:|---:|---:|
| Vercel compact archive | 120,785,873 bytes | 120,785,873 bytes | 0 bytes |
| 압축 해제 DB | 425,758,720 bytes | 425,758,720 bytes | 0 bytes |
| 로컬 cold path p50 (3회) | 2,082.380 ms | 2,044.885 ms | -1.80% |
| 로컬 cold path 평균 (3회) | 2,123.868 ms | 2,091.083 ms | -1.54% |

Vercel 전용 패키지는 변경 전부터 수집 의존성을 포함하지 않았으므로 archive 크기 변화가 없다. 콜드 스타트 차이는 동일 archive 반복 측정의 실행 환경 변동 범위이며, 이번 변경에 의한 성능 향상으로 해석하지 않는다. 원격 fresh-instance 수치는 별도 preview 배포를 생성하지 않아 측정하지 않았다.

### 검증 결과

- `python -m unittest discover -s tests`: 2,120 tests 통과, 1 skipped, 709.452초.
- 최초 전체 실행에서는 Dockerfile COPY 문구를 고정한 기존 계약 테스트 1건이 실패했다. 기대값은 수정하지 않았고, 설치 동작과 무관한 COPY 문구를 복원한 뒤 전체 테스트를 처음부터 재실행해 통과했다.
- `python scripts/ncs_harness.py lint`: 오류 0, 경고 0.
- `python scripts/ncs_harness.py smoke`: 통과.
- 로컬/Vercel `server.py` byte parity: 통과.
- `python scripts/export_mcp_tool_contract.py --out mcp/ncs-tool-contract.json --check`: 통과. 공개 MCP 계약 변경 없음.

### 배포 보안

- Vercel `ncs-mcp-bridge-mini2` Production 환경에 다음 config를 등록했다.
  - `NCS_MCP_ALLOWED_HOSTS=ncs-mcp-bridge-mini2.vercel.app`
  - `NCS_MCP_DISABLE_DNS_REBINDING_PROTECTION=0`
- Production 재배포와 실제 MCP transport 검증 결과는 배포 후 아래에 추가한다.

### 미해결 이슈

- 요소 수준 `0`은 원천 데이터의 미지정 placeholder이므로 실제 요소별 수준을 복원할 수 없다. 공개 화면에서만 `-`로 명확히 표시한다.
- `requirements.txt`는 기존 호환성 때문에 ingest 패키지를 포함하는 전체 설치 목록으로 남아 있다. 경량 서버 설치는 `pip install .`, 수집 설치는 `pip install ".[ingest]"`, 개발 설치는 `pip install ".[dev]"`를 사용한다.
