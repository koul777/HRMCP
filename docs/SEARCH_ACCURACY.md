# NCS 검색 정확도 개선과 회귀 확인

목표는 공식 능력단위명과 정확히 같은 표현을 입력하지 않아도 관련 NCS 코드를
찾게 하면서, 정확한 이름·코드 조회와 분야 필터를 보존하는 것이다. 자연어 후보
확장, 오탈자 복구, 후보 순위 개선은 검색 보조 기능이며 원천 NCS 데이터나
사람의 검토 상태를 수정하지 않는다.

공식 능력단위명 어휘에서 유일한 한 글자 오탈자 후보를 찾아 같은 능력단위의
다른 질의 단어와 문맥을 검증한다. 보정 정보는 `unit_query_terms.resolved_from`에
표시하고 원래 질의는 유지한다. 이미 유효한 단어·접두어, 모호한 후보, 짧은
단어와 NCS 코드는 임의로 보정하지 않는다.

업무 설명과 KSA의 공동 근거가 충분하면 기존 50개 후보 중 하나를 3위로
승격할 수 있다. 상위 두 결과와 강한 정확 명칭 검색 단계는 유지한다. 기존
3위가 동의어 확장을 포함해 이미 같은 근거를 갖고 있으면 중복 승격하지
않는다. 이 기능의 효과는 아래의 동일 DB 비교와 분야별 지표로 확인한다.

## 변경마다 실행하는 CI

`.github/workflows/search-accuracy.yml`은 원격 저장소에 반영된 뒤 검색·라우터·평가·
배포 런타임과 관련된 push 및 pull request, 수동 실행에서 동작하도록 설정했다.
Windows의 Python 3.11과 3.12에서
검색 회귀 테스트를 실행하고 JUnit 결과를 14일간 보관한다. GitHub 토큰 권한은
`contents: read`이며 원천 DB 다운로드, API 수집, 운영 갱신, 배포 및 일정 실행을
포함하지 않는다. 기존 전체 CI를 함께 유지한다.

검사 범위는 다음과 같다.

- 공식 능력단위명 어휘에서 유일한 한 글자 오탈자 후보를 찾는 테스트. 이미
  유효한 단어·접두어, 모호한 후보, 짧은 단어, 코드, 여러 글자 오류는 임의로
  고치지 않는지도 검사한다.
- 검색 회귀, 긴 문장의 후보 단어 선택, 표준 요청 문구, 유니코드 정규화,
  prefix 인덱스, 분야 필터, 페이지네이션, 라우터 및 선택적 semantic rescue.
- `test_ncs_query_candidate_recall.py`의 후보 복구 통합 테스트. 파일이 누락되면
  검사가 실패하며 조건부 생략하지 않는다.
- 자연어 평가 지표와 회귀 비교, 검색 진단·벤치마크 도구의 계약.
- 공식 명칭으로부터 한 번의 인접 글자 전치 오타를 만드는 자기검색 감사 도구의
  계약. 원본 기대 코드 유지, 표본 순서 독립성 및 코드 오타 생성 금지를 검사한다.
- canonical 소스와 `deploy/vercel_mcp_app` 런타임의 일치 여부. CI에서 소스를
  자동 복사해 차이를 숨기지 않으므로 수정자가 두 사본을 함께 갱신해야 한다.

CI는 격리된 합성 smoke DB와 테스트별 임시 DB를 사용한다. 테스트 통과는
검사한 동작의 회귀가 없다는 증거이며 운영 DB의 자연어 정확도 수치가 아니다.
대용량 DB가 없는 환경에서 합성 smoke 결과를 실제 NCS 평가 통과로 해석하지
않는다.

핵심 검사를 로컬에서 다시 실행할 수 있다.

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q tests/test_ncs_search_typo.py tests/test_ncs_search_recall.py tests/test_ncs_search_unit_terms.py tests/test_ncs_search_eval_nl.py tests/test_audit_ncs_search_precision.py
python -m pytest -q tests/test_deploy_runtime_parity.py tests/test_vercel_deploy_source_sync.py
```

## 준비된 실제 DB에서 비교하기

운영 갱신이나 원천 수집을 시작하는 대신, Builder가 준비한 SQLite 스냅샷을
읽기 전용으로 사용한다. 기준 코드와 후보 코드는 독립된 체크아웃과 프로세스로
실행하고 같은 DB·fixture·결과 수를 사용한다. DB의 SHA-256, 크기, 코드 커밋과
fixture SHA-256을 평가 기록에 남긴다. 비교 도중 DB를 교체하지 않는다.

평가 세트는 역할을 분리한다.

| 입력 | 역할 | 해석 |
| --- | --- | --- |
| `tests/fixtures/ncs_search_eval_nl.json` | 기존 40건 회귀 | 알려진 질의의 코드 복구가 유지되는지 확인 |
| `tests/fixtures/ncs_search_eval_nl_dev.json` | 개발 및 비HR 대조군 | 후보 확장·순위 변경의 개발 지표 |
| `tests/fixtures/ncs_search_eval_nl_dev_long.json` | 긴 문장 개발 | 긴 업무 설명의 후보 추출을 점검 |
| `tests/fixtures/ncs_search_eval_nl_holdout_v2.json` | 이전 동결 세트 | 이미 노출된 실패 사례를 튜닝 정답으로 재사용하지 않음 |
| `tests/fixtures/ncs_search_eval_nl_holdout_v3.README.md` | 새 holdout의 동결 계약 | JSON이 없으면 평가 준비 완료로 표시하지 않음 |

아래는 동일한 개발 fixture를 기준 코드와 후보 코드로 비교하는 예다. 첫 명령은
기준 체크아웃에서, 두 번째는 후보 체크아웃에서 실행한다. `<snapshot.db>`와
리포트 경로는 실제 절대 경로로 바꾼다. 기준 리포트는 첫 명령이 만든 전체
JSON을 사용한다.

```powershell
# 기준 체크아웃에서 실행
python scripts\audit_ncs_search_precision.py --nl-eval --input tests\fixtures\ncs_search_eval_nl_dev.json --db <snapshot.db> --limit 10 --enforce-hit3 --hit3-threshold 0.7 --out <baseline-dev.json> --markdown-out <baseline-dev.md>

# 후보 체크아웃에서 같은 DB와 fixture로 실행
python scripts\audit_ncs_search_precision.py --nl-eval --input tests\fixtures\ncs_search_eval_nl_dev.json --db <snapshot.db> --limit 10 --enforce-hit3 --hit3-threshold 0.7 --baseline <baseline-dev.json> --fail-on-regression --regression-tolerance 0 --out <candidate-dev.json> --markdown-out <candidate-dev.md>
```

40건 회귀 세트와 긴 문장 개발 세트도 각각 독립된 기준 리포트를 만들어 같은
절차로 실행한다. 개발·회귀·holdout 수치를 합쳐 하나의 평균으로 표시하지
않는다. 저장소의 `tests/fixtures/search_baselines/*.json`은 과거 수치 참고용이며,
다른 DB에서 얻은 수치를 현재 동일 스냅샷 비교의 기준으로 대신 쓰지 않는다.
`--baseline`은 지표의 하락을 비교하며 DB·fixture 해시와 소스 동일성을 자동
검증하지 않는다. 해당 식별값과 limit은 독립 비교 리포트에서 직접 확인한다.
`--compare-stage1-baseline`은 과거 fallback 동작의 진단용 재현 옵션이다. 실제
직전 릴리스 코드와의 비교는 별도 체크아웃에서 생성한 기준 리포트로 수행한다.

동결 holdout은 개발 튜닝을 마친 릴리스 후보에서 계약에 따라 측정한다. v3
준비 시에는 기대 코드의 DB 존재, 질의 중복, 분야 균형을 확인하고 사람이
동결한 뒤 사용한다. 질의별 실패를 확인해 다시 튜닝하면 그 세트는 독립된
일반화 추정 근거가 아니므로 다음 동결 세트를 준비한다. 공개 holdout 보고는
집계 Hit@1·Hit@3·MRR과 분야별 표를 사용한다.

## 받아들일 변경의 증거

전체 및 분야별 Hit@1·Hit@3·MRR, 검색 오류 수를 함께 확인한다. 높은 Hit@3
하나만으로 상위 1위 악화나 비HR 분야의 회귀를 감추지 않는다. 결과 limit을
바꾸면 MRR의 측정 범위도 달라지므로 기존 리포트와 바로 비교하지 않는다.
DB 부재, 기대 코드 부재, 검색 오류, 비교 입력 불일치를 정상 평가 통과로
표시하지 않는다.

정확 이름·코드 조회도 별도로 확인한다. 이 검사는 전체 NCS 대분류를 포함하는
원천 자기검색 검사이며 자연어 의미 정답 평가와 구분한다.

```powershell
python scripts\audit_ncs_exact_lookup.py --db <snapshot.db> --per-major-limit 10 --out reports\search-exact-names.json --fail-on-miss
python scripts\audit_ncs_exact_lookup.py --db <snapshot.db> --kind code --per-major-limit 10 --out reports\search-exact-codes.json --fail-on-miss
python scripts\audit_ncs_exact_lookup.py --db <snapshot.db> --kind name --variant single_typo --per-major-limit 10 --out reports\search-source-typos.json
python scripts\audit_ncs_exact_lookup.py --db <snapshot.db> --surface public --per-major-limit 10 --prompt-template all --out reports\search-request-frames.json --fail-on-miss
```

`single_typo`는 공식 명칭에서 검색 알고리즘의 성공 여부와 무관하게 인접한
서로 다른 두 글자의 순서를 한 번 바꾸고 원본 기대 코드를 유지한다. 적용할 수
없는 짧은 명칭 등은 제외되므로 분모·대분류별 표본 수를 함께 보고한다. 오타
회수율이 낮은 결과도 그대로 기록하며, 생성한 합성 오타가 실제 사용자 오타
분포나 의미상 표현 변화를 대표한다고 주장하지 않는다. 이 옵션은 명칭에만
적용하며 NCS 코드에 대해 유사 코드를 임의로 생성하거나 보정하지 않는다.
같은 DB와 독립 생성 표본을 기준·후보 코드에서 각각 평가하고 미복구 수와
대분류별 결과를 함께 보고한다. 보수적 복구는 모호한 후보를 남길 수 있으므로
이 감사에 100% 회수 조건을 무조건 적용하지 않는다.

개선 적용 전에는 같은 DB에서 속도도 확인한다. 새로운 정확도 기능이 모든
질의에서 큰 지연을 만드는지 확인하며, CI 빌드와 실제 DB 벤치마크를 동시에
실행하지 않는다. 읽기 전용 코드 A/B 명령과 측정 한계는
`docs/HARNESS_ENGINEERING.md`의 Query Performance Comparisons를 따른다.

리포트가 보여주는 것은 해당 스냅샷과 기대 NCS 코드에 대한 검색 성능이다.
코드로 검토한 기대 목록은 사람이 확인한 모든 업무 의미의 정답 집합이
아니며, 실행 환경·DB·배포 커밋이 다른 운영 MCP의 정확도 보장은 별도로 실제
배포를 확인해야 한다. 지속 개선은 관련 변경마다 CI를 실행하고, 실제 DB 비교
근거가 있는 변경을 다음 Builder 릴리스 후보로 검토하는 흐름으로 진행한다.
