@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo HR Telegram Bot Restart
echo ==========================================
echo(

echo [1/2] Stopping running app instance if found...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$runtimeFile = Join-Path (Get-Location) 'runtime\\admin_url.txt';" ^
  "$ports = New-Object System.Collections.Generic.List[int];" ^
  "if (Test-Path $runtimeFile) {" ^
  "  $url = (Get-Content $runtimeFile -Raw -ErrorAction SilentlyContinue).Trim();" ^
  "  if ($url -match ':(\\d+)') { [void]$ports.Add([int]$matches[1]) }" ^
  "}" ^
  "if (-not $ports.Count) { [void]$ports.Add(8080) }" ^
  "$ports = $ports | Select-Object -Unique;" ^
  "foreach ($port in $ports) {" ^
  "  $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1;" ^
  "  if ($conn) {" ^
  "    try { Stop-Process -Id $conn.OwningProcess -Force -ErrorAction Stop; Write-Host ('Stopped PID ' + $conn.OwningProcess + ' on port ' + $port) }" ^
  "    catch { Write-Host ('Failed to stop PID ' + $conn.OwningProcess + ' on port ' + $port) }" ^
  "  }" ^
  "}"

timeout /t 2 /nobreak >nul

echo [2/2] Starting fresh app instance...
call "%~dp0start_hr_bot.bat"

endlocal
