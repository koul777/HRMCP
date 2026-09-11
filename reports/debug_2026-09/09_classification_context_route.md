# 9단계 — 분류 맥락 전달 경로 보강

작성일: 2026-09-11

## 목적

자연어 질의의 직무 맥락을 caller가 선택한 NCS 분류 범위와 함께 검색까지
전달한다. holdout 문장에 맞춘 alias 추가나 자동 major 추론은 하지 않는다.

## 구현

- `route_ncs_query(..., classification_filter=...)`가 허용된 분류 필드만
  정규화해 구조 검색 route의 `params`에 보존한다.
- `classification_context`에 `provided`와 실제 filter를 함께 남긴다.
- `ncs_execute_tool`이 `_route_query`를 재계산할 때 동일한
  `classification_filter`를 포함하므로 route fingerprint가 유지된다.
- 검색 계층은 기존의 parameter-bound hard filter를 그대로 적용한다.
- Vercel mirror와 canonical source의 query router/server 파일 parity를 유지한다.

## 실제 DB 확인

canonical `data/processed/ncs.db`에서 동일 질의를 분류 필터 없이/`major_code=02`
로 각각 실행했다.

| 질의 | 필터 없음 상위 결과 | `major_code=02` 적용 결과 |
|---|---|---|
| 업무용 차량 배차 관리 | 업무용 동산관리, 차량손해사정 기획 관리, 차량손해액 산정 | 업무용 동산관리, 차량운영관리, 업무지원 |
| 워크숍 행사 준비 | 웨딩 행사 준비, 연회 행사 준비, 국내여행안내 행사준비 | 행사지원관리, 세무조정 준비, 공공조달 입찰 참가 준비 |

분류 필터 적용 결과에는 `classification_filter_applied=true`가 포함되며,
다른 대분류의 동명·유사명 능력단위가 후보에서 제외된다.

## 검증

- query router 테스트: 통과
- search recall 테스트: 통과
- meta-tool route fingerprint/filter 전달 테스트: 통과
- local/Vercel runtime parity: 통과
- `ncs_harness.py lint`: 오류 0, 경고 0
- `ncs_harness.py smoke`: 통과

## 해석 및 한계

이 변경은 caller가 명시한 분류 맥락을 검색 경로에 연결하는 것이다. 일반
단어만 보고 시스템이 임의로 major를 추론하지 않는다. 따라서 UI나 agent는
분류를 먼저 확인한 뒤 `classification_filter`를 전달해야 한다.

holdout 성능 개선 여부는 기존 holdout을 변경하지 않은 동일 조건으로 별도
측정해야 하며, 이번 변경 자체의 효과를 alias 성능으로 해석하지 않는다.

