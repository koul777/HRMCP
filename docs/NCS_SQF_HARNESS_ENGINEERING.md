# NCS-SQF Harness Engineering

## 목적

이 문서는 NCS-SQF 온톨로지 MVP를 반복 가능하게 만들기 위한 실행 하네스 기준이다. 목표는 경영지원 분야부터 시작해, 사용자가 원하는 업무를 물었을 때 관련 SQF 직무수준, NCS 능력단위, KSA, 교육훈련·자격·경력 근거를 추적 가능한 형태로 반환하는 것이다.

## MVP 범위

1차 MVP는 경영지원 분야로 제한한다.

```text
SQF
  ncs_lclas_cd = 02
  ncs_lclas_name = 경영·회계·사무
  sqf_field_name = 경영관리
  job_name = 경영지원

NCS
  major_code = 02
  major_name = 경영·회계·사무
```

현재 SQF `경영지원` 직무는 7건이다. 이 범위를 우선 그래프화하고, 이후 인사, 재무, 회계, 경영전략기획으로 확장한다.

## 기본 명령

저장소 루트에서 실행한다.

```powershell
$env:PYTHONPATH="C:\workspace\NCS_MCP\src"
python scripts\ncs_harness.py inspect
python scripts\ncs_harness.py pipeline --api-sqf --sqf-major-code 02
python scripts\ncs_harness.py build-sqf-mappings
python scripts\ncs_harness.py export-package
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
```

전체 SQF 수집:

```powershell
python scripts\ncs_harness.py pipeline --api-sqf
```

대시보드:

```powershell
python scripts\ncs_dashboard.py --host 127.0.0.1 --port 8765
```

대시보드에서 `경영지원 MVP` 버튼을 눌러 SQF 경영지원 범위를 확인한다.

## 데이터 파악 체크

`inspect`에서 확인한다.

- `sqf_service_key_present = true`
- `counts.sqf_duties > 0`
- `sqf_duties`에 `02 > 경영관리 > 경영지원` 데이터 존재
- `api_sqf_report.md` 최신 생성

SQL 점검:

```sql
SELECT sqf_field_name, job_name, COUNT(*) AS count
FROM sqf_duties
WHERE ncs_lclas_cd = '02'
GROUP BY sqf_field_name, job_name
ORDER BY sqf_field_name, job_name;
```

## 온톨로지 생성 순서

1. `sqf_duties`에서 경영지원 SQF 직무수준 노드를 만든다.
2. NCS `02`의 분류, 능력단위, 요소, 수행준거, KSA 노드를 만든다.
3. `ncs_lclas_cd = major_code`는 확정 연결로 둔다.
4. SQF 직무정의와 NCS 세분류/능력단위/API 정의/KSA 텍스트를 비교해 매핑 후보를 만든다.
5. 매핑 후보는 별도 객체에 저장한다.
6. 사람이 대시보드에서 후보를 검토하고 `review_status`를 갱신한다.
7. 추천 도구는 검토된 매핑을 우선 사용하고, 없으면 후보 매핑을 낮은 confidence로 사용한다.

## 매핑 객체 필수 필드

```text
source_type
source_id
target_type
target_id
relation
score
confidence
match_method
evidence_text
evidence_source
source_version
review_status
created_at
updated_at
```

관계는 다음만 우선 사용한다.

- `requiresNCSUnit`
- `partiallyCovers`
- `closeMatch`
- `related`

`sameAs`는 기본 사용하지 않는다.

## 추천 응답 기준

경영지원 MVP 추천은 다음을 반드시 포함한다.

- 매칭된 SQF 직무수준
- SQF 직무정의와 직무수준 설명
- SQF 교육훈련, 자격, 경력 조건
- 연결된 NCS 능력단위
- 연결 근거와 confidence
- 부족 능력단위요소, 수행준거, KSA

SQF 교육훈련 필드가 비어 있으면 NCS 능력단위와 KSA를 학습 목표로 변환한다.

## 검증 루프

변경 후 최소 실행:

```powershell
python -m unittest discover -s tests -v
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
```

대시보드 변경 시:

```powershell
python -m unittest tests.test_dashboard -v
```

성공 기준:

- 대시보드 상단에 SQF 직무수준, 경영지원 MVP, NCS-SQF 매핑 상태가 보인다.
- `경영지원 MVP SQF 직무` 카드에서 7건 내외의 직무수준을 볼 수 있다.
- 매핑 테이블이 없으면 대시보드가 실패하지 않고 `테이블 생성 필요`를 표시한다.
- 추천은 공식 인정 판정이 아니라 근거 기반 교육 추천/갭분석으로 표현된다.
