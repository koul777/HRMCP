@echo off
setlocal

if "%NCS_MCP_READ_ONLY%"=="" set "NCS_MCP_READ_ONLY=1"
if "%NCS_MCP_ENABLE_OPERATOR_TOOLS%"=="" set "NCS_MCP_ENABLE_OPERATOR_TOOLS=0"
if "%NCS_CHAT_HOST%"=="" set "NCS_CHAT_HOST=127.0.0.1"
if "%NCS_CHAT_PORT%"=="" set "NCS_CHAT_PORT=8780"
if "%NCS_CHAT_AUTH_MODE%"=="" set "NCS_CHAT_AUTH_MODE=local"

set "REMOTE_ARG="
if /I "%NCS_CHAT_ALLOW_REMOTE_BIND%"=="1" set "REMOTE_ARG=--allow-remote-bind"

python -m ncs_mcp.institutional_chat --host "%NCS_CHAT_HOST%" --port "%NCS_CHAT_PORT%" --auth-mode "%NCS_CHAT_AUTH_MODE%" %REMOTE_ARG%
