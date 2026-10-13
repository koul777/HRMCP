# 테스트 출력 격리·추천 요약 수정 — 2026-10-13

이번 수정의 전체 테스트·집중 검증·lint·smoke와 운영 산출물 보존 검사를 완료했다. GitHub 반영·Vercel 배포 상태는 아래에 별도로 기록한다.

## 변경 내용

- **운영 리포트 보호:** 체크포인트·수집기·watchdog 테스트가 입력 fixture만 임시 경로로 바꾸고, 날짜별 출력 별칭은 실제 `reports/`에 쓰거나 삭제하던 문제를 수정했다. [공통 테스트 도우미](../tests/script_output_sandbox.py)가 `PROJECT_ROOT`와 미리 계산된 경로 상수를 함께 임시 폴더로 옮기고 예외 발생 시에도 복원한다. 기본 CLI 출력, 별칭 복사·삭제, 실패 후 정리 경로를 외부 파일 보존 검사로 검증한다. 운영 수집 코드의 동작은 변경하지 않았다.
- **정의 검토 경고 보존:** [추천 요약 코드](../src/ncs_mcp/training_recommendation.py)의 네 소비 지점에 공통 선택 규칙을 적용했다. 정의 경고가 첫 세 항목에서 잘릴 때 첫 과업 근거, 훈련목표 또는 연결 방식 근거, 정의 검토 문장을 함께 남긴다. 경고가 이미 보이거나 없는 경우 기존 요약을 유지한다. 추천 점수·confidence·순위·tier·사람 검토 상태는 변경하지 않는다. 배포 런타임 미러에도 동일하게 적용했다.

## 체크포인트 복구 범위

운영 DB를 `mode=ro`, `PRAGMA query_only=ON`으로 읽어 다음 NCS006 현황을 확인했다. 최종 산출물 보존 검사 후 `20261013`·`20261013_current` 체크포인트를 생성하고, 오염된 `20260624_current` 별칭을 복구했다. 각 JSON·Markdown은 검토한 복구본과 바이트가 일치하며, 기존 오염 파일은 별도 백업으로 보존했다.

| 2026-10-13 읽기 전용 집계 | 건수 |
| --- | ---: |
| 능력단위요소 전체 | 47,620 |
| API 자료 연결됨 | 47,132 |
| API 응답에 자료 없음 | 488 |
| 미수집 / API 실패 | 0 / 0 |

집계 시점과 수집 이력의 시점을 구분한다. 보존된 runner `20260620` 로그와 watchdog `20260624` 로그의 마지막 사건은 **2026-06-26 17:02:52 UTC**다. `20261010` watchdog 파일의 10건짜리 테스트 fixture는 복구 근거에서 제외했다. 이 작업은 새 API 수집이나 손실된 이후 이력의 재구성이 아니며, 오염 파일은 별도로 보존한다. 원천 KSA와 사람 승인 상태를 수정하지 않는다.

기본 대시보드는 최신 readiness가 지정한 릴리스 묶음의 체크포인트를 우선하며, 이번 확인에서는 `20260712_10h`를 선택했다. 에이전트 큐는 새 `20261013_current`를 선택한다. 운영 현황을 확인할 대시보드 세션에서는 JSON·Markdown을 함께 지정한다. 아래 명시적 경로가 선택되는 것도 확인했다.

```powershell
$env:NCS_NCS006_ELEMENT_API_CHECKPOINT_JSON_PATH="reports/checkpoint_ncs006_element_api_status_20261013_current.json"
$env:NCS_NCS006_ELEMENT_API_CHECKPOINT_MD_PATH="reports/checkpoint_ncs006_element_api_status_20261013_current.md"
python scripts/ncs_dashboard.py --host 127.0.0.1 --port 8765
```

릴리스 증거의 날짜 묶음과 명시적 세션 경로 선택 규칙은 유지한다. 큐 상태를 재검증할 때도 최신 readiness의 `agent_work_queue_path`를 사용하며, 오래된 `_current` 파일의 다른 큐를 혼합하지 않는다.

## 검증 상태

| 검사 | 현재 결과 | 범위 |
| --- | --- | --- |
| 체크포인트·수집 안전성 | 56개 통과 | 임시 DB·출력 경로와 예외 정리 검사 |
| 추천 요약·정의 신뢰도·배포 미러 | 49개 통과 | 네 요약 경로, 정의 없음·정형 정의·미확인·경고 없음 구분 |
| 저장 응답 전후 비교 | 4개 요청·15개 과정·56개 요약 확인 | 원본 입력·전체 카드·설명 외 필드 동일 |
| HRD 가이드 계약 | 템플릿 10/10, 교육훈련 예시 17/17 | 추가 사용자 맥락 필요 3건 유지 |
| 집중 검사 후 운영 리포트 보존 | 최상위 파일 11,175개 불변 | 추가·삭제·내용·메타데이터 변경 0; 전체 suite 결과와 별개 |
| lint | 오류 0·경고 0 | 저장소 검사 |
| smoke | 통과 | 운영 DB 읽기 전용 조회: 해당 직무 능력단위 14·표본 요소 3·수행준거 9·KSA 24 |
| 전체 테스트 | 2,784개 통과·28개 건너뜀·실패/오류 0 | 격리된 세 checkout에서 2,812개 전체 발견·중복/누락 없이 실행 |
| 운영 DB 라우팅 회귀 | 별도 7개 통과 | 전체 격리 검사에서 생략된 실제 DB 사례를 읽기 전용으로 실행 |
| 전체 검사 후 운영 리포트·DB 보존 | 통과 | 복구 반영 전 11,175개 파일 불변; 운영 DB 전체 SHA-256·크기·수정 시각 동일 |
| 체크포인트 반영 후 보존 | 통과 | 계획한 파일 4개 추가·2개 교체 외 변경/삭제 0; 큐·대시보드 선택 경로 확인 |
| 이번 수정의 GitHub 반영·배포 | 확인 대기 | 커밋·원격 CI·실제 배포 확인 전 |

전체 테스트의 건너뛰기는 격리 환경에 없는 운영·가공 DB 23건, Windows 심볼릭 링크 권한 5건이다. 저장 응답 비교는 10월 5일에 저장한 응답을 사용했다. 새 운영 추천 실행이나 독립적인 의미 정확도 평가가 아니다. 기준 추천 모듈과 수정 모듈을 같은 현재 의존성으로 비교했고, 변경된 함수도 설명 생성·요약 경로로 한정됐는지 확인했다. 운영 산출물 보존은 격리된 전체 테스트와 수정된 집중 테스트의 실행 범위에 대한 결과이며, 모든 테스트가 파일을 쓰지 않는다는 보장은 아니다.

검증 로그와 오염 파일 백업은 로컬 `reports/urgent_2h_20261013/`에 보관하며 Git에 포함하지 않는다. 대용량 운영 DB도 커밋 대상이 아니다.

저장소 전체의 소스 배포 경계 검사에는 기존 추적 파일 94개와 과거 LFS 경로 5개의 정리 과제가 남아 있다. 이번 변경은 코드·테스트·문서만 커밋하며, 별도 소스 미리보기에서 금지 파일을 제외하고 배포 패키지의 파일·비밀정보 검사를 수행한다. 이 코드 수정이 기존 데이터 품질이나 사람 검토에 필요한 릴리스 조건을 충족했다는 뜻은 아니다.

## 재현

저장소 루트에서 프로젝트 의존성이 설치된 Python으로 실행한다. 전체 검증은 별도 checkout과 임시 DB·출력 폴더를 사용한다.

```powershell
python -m unittest tests.test_checkpoint_reports tests.test_element_api_collection_safety -v
python -m unittest tests.test_training_recommendation.TrainingRecommendationTests.test_compact_definition_review_survives_all_three_reason_summaries tests.test_training_recommendation.TrainingRecommendationTests.test_compact_definition_review_keeps_match_evidence_without_goal_highlights tests.test_training_recommendation.TrainingRecommendationTests.test_compact_reasons_preserve_short_lists_and_existing_review_position tests.test_deploy_runtime_parity -v
python scripts/ncs_harness.py hrd-guide-prompt-coverage --out reports/local_validation/hrd_guide_prompt_coverage.json --markdown-out reports/local_validation/hrd_guide_prompt_coverage.md
```

배포는 기존 [Windows NCS DB 업데이트 빌더](NCS_DATA_BUILDER.md)의 코드 배포 경로를 사용한다. 운영 데이터 재수집이나 정의 승격은 이번 변경에 포함하지 않는다.
