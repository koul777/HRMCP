# NCS-SQF Handoff Package

이 프로젝트의 권장 전달 단위는 실시간 API 호출 코드가 아니라 전처리된 SQLite 지식베이스와 그 설명 파일이다.

```text
NCS Excel/API + SQF API
  -> raw/API response cache
  -> normalized SQLite
  -> SQF-NCS mapping candidates
  -> MCP server reads SQLite
```

## Generate Package

기본 실행은 큰 DB를 복사하지 않고 문서와 SQL만 만든다.

```powershell
python scripts\ncs_harness.py export-package
```

기본 출력 위치:

```text
exports/ncs_sqf_output/
```

생성 내용:

```text
README.md
manifest.json
sql/schema.sql
sql/indexes.sql
sql/sample_queries.sql
docs/schema.md
docs/data_dictionary.md
data/db/README.md
```

## Include SQLite DB

현재 `data/processed/ncs.db`는 큰 파일이다. 전달용 이름인 `ncs_sqf.sqlite`를 만들 때는 먼저 하드링크를 권장한다.

```powershell
python scripts\ncs_harness.py export-package --db-mode hardlink
```

하드링크는 같은 볼륨에서 공간을 거의 쓰지 않지만 원본 DB와 같은 파일 데이터를 공유한다. 완전히 독립된 복사본이 필요하고 디스크 여유가 충분하면 다음을 사용한다.

```powershell
python scripts\ncs_harness.py export-package --db-mode copy
```

압축 파일까지 만들려면:

```powershell
python scripts\ncs_harness.py export-package --db-mode hardlink --zip
```

주의: DB가 포함된 zip은 매우 클 수 있다.

## What To Inspect

핸드오프를 받은 쪽에서는 다음을 먼저 확인한다.

- `manifest.json`: 생성 시점, 원본 DB 경로, 테이블별 행 수.
- `docs/schema.md`: 온톨로지/MCP 관점의 테이블 역할.
- `docs/data_dictionary.md`: 필드 설명과 FK/index.
- `sql/sample_queries.sql`: NCS 구조, SQF 직무수준, 후보 매핑 조회 쿼리.

## Security

API 키는 패키지에 포함하지 않는다. `.env`는 전달하지 않는다. 필요한 환경변수 이름만 `.env.example`에 둔다.

