# Run from Windows PowerShell with Go 1.26 installed.
param([string]$Go = "go.exe")
$ErrorActionPreference = "Stop"
$root=$PSScriptRoot
$out=Join-Path $root "构建输出"
[IO.Directory]::CreateDirectory($out) | Out-Null
$env:GOENV="off"
$env:GOTOOLCHAIN="local"
$env:GOTELEMETRY="off"
$env:GOPATH=Join-Path $out "go"
$env:GOMODCACHE=Join-Path $out "gomod"
$env:GOCACHE=Join-Path $out "cache"
$env:TEMP=Join-Path $out "temp"
$env:TMP=$env:TEMP
$env:CGO_ENABLED="0"
[IO.Directory]::CreateDirectory($env:TEMP) | Out-Null
Push-Location $root
try {
    & $Go build -trimpath -mod=readonly -o (Join-Path $out "gmserver.exe") ./cmd/gmserver
    if($LASTEXITCODE){exit $LASTEXITCODE}
    & $Go build -trimpath -mod=readonly -ldflags "-H windowsgui" -o (Join-Path $out "gm-desktop.exe") .
    if($LASTEXITCODE){exit $LASTEXITCODE}
    if(Test-Path -LiteralPath (Join-Path $root "app.manifest")){
        Copy-Item -LiteralPath (Join-Path $root "app.manifest") -Destination (Join-Path $out "gm-desktop.exe.manifest")
    }
} finally { Pop-Location }
