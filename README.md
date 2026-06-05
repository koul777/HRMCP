# NCS MCP Project

NCS정보망 DB 엑셀 전체를 SQLite로 정규화하고, 공공데이터포털 `한국산업인력공단_NCS 기준정보 조회` API로 능력단위 마스터 정보를 보강한 뒤, Claude Desktop에서 MCP로 조회할 수 있게 만드는 프로젝트입니다.

## 1. 환경 설정

```powershell
cd C:\Workplace\NCS_MCP
python -m pip install -e .
Copy-Item .env.example .env
```

`.env`에서 다음 값을 확인합니다.

```text
NCS_EXCEL_PATH=원본 엑셀 경로
NCS_DB_PATH=C:/Workplace/NCS_MCP/data/processed/ncs.db
NCS_SERVICE_KEY=공공데이터포털 ServiceKey
```

## 2. 전체 엑셀 전처리

```powershell
python -m ncs_mcp.preprocess_excel --reset
```

산출물:

```text
data/processed/ncs.db
reports/preprocess_summary.json
reports/preprocess_summary.md
```

특정 시트나 일부 행만 테스트하려면:

```powershell
python -m ncs_mcp.preprocess_excel --reset --sheets 02 --max-rows 5000
```

## 3. API 수집 및 조인

```powershell
python -m ncs_mcp.collect_api
```

기본값은 승인된 `한국산업인력공단_NCS 기준정보 조회` API의 `/NCS005`를 사용합니다.

산출물:

```text
reports/api_join_report.md
```

## 4. 품질 진단

```powershell
python -m ncs_mcp.quality
```

산출물:

```text
reports/quality_issues.md
```

## 5. MCP Server 실행

```powershell
python -m ncs_mcp.server
```

Claude Desktop 설정 예시:

```json
{
  "mcpServers": {
    "ncs-mcp": {
      "command": "python",
      "args": ["C:/Workplace/NCS_MCP/src/ncs_mcp/server.py"],
      "env": {
        "NCS_DB_PATH": "C:/Workplace/NCS_MCP/data/processed/ncs.db"
      }
    }
  }
}
```

## 6. 주요 MCP Tools

- `list_classifications`
- `get_competency_units`
- `get_unit_structure`
- `get_element_detail`
- `get_performance_criteria`
- `get_ksa`
- `search_ncs`
- `get_quality_issues`
- `compare_raw_refined`
- `get_api_join_status`
