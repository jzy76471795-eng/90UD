param(
    [switch]$Verify
)

$ErrorActionPreference = "Stop"
$nativeRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$startRoot = (Resolve-Path (Join-Path $nativeRoot "..")).Path
$serverRoot = (Resolve-Path (Join-Path $startRoot "..")).Path
$repoRoot = (Resolve-Path (Join-Path $serverRoot "..")).Path
$toolchainCandidates = @(
    (Join-Path $repoRoot "analysis_output\toolchains\llvm-mingw-20260616-msvcrt-x86_64"),
    (Join-Path $repoRoot "analysis_output\history\core_cleanup_20260720\analysis_output_root_remaining\toolchains\llvm-mingw-20260616-msvcrt-x86_64")
)
$toolchain = $toolchainCandidates | Where-Object {
    Test-Path (Join-Path $_ "bin\x86_64-w64-mingw32-g++.exe")
} | Select-Object -First 1
if (-not $toolchain) {
    throw "Missing x64 llvm-mingw compiler. Tried: $($toolchainCandidates -join ' | ')"
}

$compiler = Join-Path $toolchain "bin\x86_64-w64-mingw32-g++.exe"
$source = Join-Path $nativeRoot "usdof_session_launcher.cpp"
$output = Join-Path $startRoot "usdof_session_launcher.exe"

foreach ($required in @($compiler, $source)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Missing native launcher build input: $required"
    }
}

Write-Host "[native-launcher] compiler=$compiler"
Write-Host "[native-launcher] source=$source"
Write-Host "[native-launcher] output=$output"

& $compiler `
    -std=c++17 `
    -O2 `
    -DNDEBUG `
    -DUNICODE `
    -D_UNICODE `
    -D_WIN32_WINNT=0x0601 `
    -Wall `
    -Wextra `
    -Werror `
    -municode `
    -static `
    -s `
    $source `
    -lws2_32 `
    -o $output
if ($LASTEXITCODE -ne 0) {
    throw "Native session launcher build failed with exit code $LASTEXITCODE"
}

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $output).Hash
Write-Host "[native-launcher] sha256=$hash"

if ($Verify) {
    & $output --check
    if ($LASTEXITCODE -ne 0) {
        throw "Native session launcher verification failed with exit code $LASTEXITCODE"
    }
}
