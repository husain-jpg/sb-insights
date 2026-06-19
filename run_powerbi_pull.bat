@echo off
REM Daily OCS Power BI market-intelligence pull — all stores x all windows, headless.
REM Registered in Windows Task Scheduler. Appends output to logs\powerbi_pull.log
REM and preserves the Python exit code (0 ok / 1 some failed / 2 session expired)
REM so Task Scheduler's "Last Run Result" reflects what happened.
cd /d C:\terroir-ops
if not exist logs mkdir logs
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
echo ===== run started %date% %time% ===== >> logs\powerbi_pull.log
python jobs\powerbi_pull.py pull >> logs\powerbi_pull.log 2>&1
set RC=%ERRORLEVEL%
echo ===== run finished %date% %time% exit=%RC% ===== >> logs\powerbi_pull.log
exit /b %RC%
