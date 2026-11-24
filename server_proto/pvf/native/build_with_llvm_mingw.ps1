param(
    [switch]$Verify
)

$ErrorActionPreference = "Stop"
$nativeRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$serverRoot = (Resolve-Path (Join-Path $nativeRoot "..\..")).Path
$repoRoot = (Resolve-Path (Join-Path $serverRoot "..")).Path
$pythonRoot = Join-Path $repoRoot "server-pack-win10\cache\python-3.12.10-full"
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
$python = Join-Path $pythonRoot "python.exe"
$include = Join-Path $pythonRoot "Include"
$importLibrary = Join-Path $pythonRoot "libs\python312.lib"
$source = Join-Path $nativeRoot "usdof_pvf_native.cpp"
$output = Join-Path $serverRoot "usdof_pvf_native.cp312-win_amd64.pyd"

foreach ($required in @($python, $include, $importLibrary, $source)) {
    if (-not (Test-Path $required)) {
        throw "Missing native PVF build input: $required"
    }
}

Write-Host "[native-pvf] compiler=$compiler"
& $compiler --version | Select-Object -First 1 | Write-Host
Write-Host "[native-pvf] target=x86_64-w64-windows-gnu"
Write-Host "[native-pvf] python=$python"
Write-Host "[native-pvf] include=$include"
Write-Host "[native-pvf] import_lib=$importLibrary"
Write-Host "[native-pvf] output=$output"

& $compiler `
    -std=c++17 `
    -O3 `
    -DNDEBUG `
    -Wall `
    -Wextra `
    -Werror `
    -Wno-missing-field-initializers `
    -shared `
    -I $include `
    $source `
    $importLibrary `
    -static-libgcc `
    -static-libstdc++ `
    -o $output
if ($LASTEXITCODE -ne 0) {
    throw "Native PVF module build failed with exit code $LASTEXITCODE"
}

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $output).Hash
Write-Host "[native-pvf] sha256=$hash"

if ($Verify) {
    & $python (Join-Path $nativeRoot "verify_native_module.py")
    if ($LASTEXITCODE -ne 0) {
        throw "Native PVF verification failed with exit code $LASTEXITCODE"
    }
}
