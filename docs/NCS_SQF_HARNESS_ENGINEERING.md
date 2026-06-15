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

## 교육 추천 데이터 보강 순서

SQF `dutyEduTrain`, `dutyQualf`, `dutyCarr`가 비어 있는 분야가 많기 때문에 교육 추천은 SQF API 필드만으로 만들지 않는다. 하네스는 아래 순서로 근거를 채운다.

1. 학습모듈 API에서 `major_code = 02` 전체와 주요 키워드별 모듈을 수집한다.
2. API가 특정 능력단위의 학습모듈을 반환하지 않으면 공식 NCS 학습모듈 PDF를 `role = ncs_learning_module`로 등록한다.
3. NCS 활용패키지 PDF는 `role = ncs_learning_package`로 등록하고 수행준거, KSA, 자가진단, 직무기술서 근거를 추출한다.
4. SQF 보고서와 개발 매뉴얼은 `role = sqf_report` 또는 `framework_reference`로 등록하고 SQF 직무수준과 NCS 능력단위의 필수/선택 관계를 추출한다.
5. 직접 학습모듈이 없으면 NCS 능력단위, 수행준거, KSA를 학습목표로 변환해 fallback 추천을 만든다.

자료가 방대하면 대상 범위, API 공백, 추천 실패 케이스를 기준으로 우선순위를 정한다. 무차별 전체 투입보다 `02 경영·회계·사무`, 경영지원, 인사기획처럼 현재 추천 품질에 영향을 주는 범위부터 자산 단위로 등록한다. 운영형 v1에서는 대상 범위의 공식 SQF/NCS 보고서와 학습모듈 PDF가 모두 `extracted` 상태여야 한다.

학습모듈 API의 기준 항목은 `결과코드`, `결과메시지`, `대분류코드`, `대분류코드명`, `중분류코드`, `중분류코드명`, `소분류코드`, `소분류코드명`, `세분류코드`, `세분류코드명`, `학습모듈번호`, `학습모듈명`, `학습모듈내용`이다. 특정 학습모듈명 질의가 `002 empty data`이면 API에 데이터가 없는 것으로 보고, 공식 PDF를 보강 근거로 사용한다.

```powershell
python scripts\ncs_harness.py query-study-modules --major-code 02 --module-name "인사기획"
python scripts\ncs_harness.py collect-study-modules --major-code 02 --num-of-rows 200
python scripts\ncs_harness.py import-ontology-source --input "<LM...pdf>" --title "NCS 학습모듈 - <능력단위명>" --role ncs_learning_module
python scripts\ncs_harness.py import-ontology-source --input "<NCS-package.pdf>" --title "NCS 활용패키지 - <직무명>" --role ncs_learning_package
python scripts\ncs_harness.py import-ontology-source --input "<SQF-report.pdf>" --title "SQF 보고서 - <분야명>" --role sqf_report
python scripts\ncs_harness.py preprocess-sqf-documents --only-unprocessed --chunk-chars 2400 --overlap-chars 250 --ocr-empty --ocr-lang kor+eng --ocr-dpi 160
```

공식 NCS 학습모듈 PDF는 `학습모듈의 개요`를 먼저 추출한다. 최소 추출 항목은 목표, 선수학습, 내용체계, 핵심 용어다. 파일명 또는 본문에서 `LM0202020101_19v2` 같은 학습모듈번호를 확보하고, 현재 DB 능력단위 코드가 `0202020101_23v3`처럼 최신 버전이면 기본 코드(`0202020101`)로 연결한다. 원래 PDF 버전과 추출 위치는 `source_payload`와 `learning_module_unit_links.evidence_text`에 보존한다.

SQF 보고서에서 NCS 능력단위가 필수로 제시되면 `relation = requires`, 선택 또는 부분 대응이면 `relation = partiallyCovers`로 저장한다. 이 관계는 `sameAs`가 아니며, 근거 문서, 페이지, chunk, confidence, 추출 버전을 함께 남긴다.

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
