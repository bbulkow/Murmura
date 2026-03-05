# esp-build.ps1 - Build ESP-IDF project with proper environment
# Usage: powershell -ExecutionPolicy Bypass -File esp-build.ps1 build
# Output goes to build_output.txt

param([Parameter(ValueFromRemainingArguments)][string[]]$IdfArgs)

# Purge inherited MSYS/MINGW env vars from Git Bash parent
Remove-Item Env:MSYSTEM -ErrorAction SilentlyContinue
Remove-Item Env:MSYSTEM_PREFIX -ErrorAction SilentlyContinue
Remove-Item Env:MINGW_PREFIX -ErrorAction SilentlyContinue
Remove-Item Env:MSYSTEM_CHOST -ErrorAction SilentlyContinue
Remove-Item Env:MSYSTEM_CARCH -ErrorAction SilentlyContinue

# Source the ESP-IDF environment
. C:\Users\bbulk\dev\esp\esp-adf\esp-idf\export.ps1 *> $null

$outputFile = Join-Path $PSScriptRoot "build_output.txt"
& idf.py @IdfArgs *> $outputFile

$exitCode = $LASTEXITCODE
"EXIT_CODE=$exitCode" | Out-File -FilePath $outputFile -Append -Encoding utf8

# Only emit a summary line for the agent
if ($exitCode -eq 0) { Write-Host "BUILD OK" } else { Write-Host "BUILD FAILED - see build_output.txt" }

exit $exitCode