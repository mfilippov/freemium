# Runs the build for everyday use with a NetLog, for audit/netlog.py.
#   powershell -File audit\netlog-browse.ps1 [-Chrome <chrome.exe>] [-UserDataDir <dir>]
# Use the browser as usual, close it, then:
#   uv run --no-project python audit\netlog.py build\netlog\browse-<time>.json
# The log holds the URLs you visited (no cookies or bodies); it stays in build\ (not committed).
param(
  [string]$Chrome = "$PSScriptRoot\..\build\chromium\src\out\Release\chrome.exe",
  [string]$UserDataDir = ""
)
$Chrome = (Resolve-Path $Chrome).Path
$running = Get-Process chrome -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $Chrome }
if ($running) {
  "This chrome.exe is already running: close it first, or the NetLog flag is ignored."
  exit 1
}
$dir = "$PSScriptRoot\..\build\netlog"
New-Item -ItemType Directory -Force $dir | Out-Null
$log = Join-Path (Resolve-Path $dir).Path ("browse-" + (Get-Date -Format "yyyyMMdd-HHmm") + ".json")
$chromeArgs = @("--log-net-log=$log", "--net-log-capture-mode=Default")
if ($UserDataDir) { $chromeArgs += "--user-data-dir=$UserDataDir" }
Start-Process $Chrome -ArgumentList $chromeArgs
"NetLog: $log"
