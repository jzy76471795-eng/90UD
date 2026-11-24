param(
 [Parameter(Mandatory=$true)][string]$InputAssembly,
 [Parameter(Mandatory=$true)][string]$OutputDirectory,
 [Parameter(Mandatory=$true)][string]$DotnetExe,
 [Parameter(Mandatory=$true)][string]$ObfuscarDll,
 [Parameter(Mandatory=$true)][string]$RuntimeRoot,
 [Parameter(Mandatory=$true)][string]$DependencyList
)
$ErrorActionPreference='Stop'
function Digest([string]$path){
 $algorithm=[Security.Cryptography.SHA256]::Create();$stream=[IO.File]::OpenRead($path)
 try{return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}
 finally{$stream.Dispose();$algorithm.Dispose()}
}
$outputFull=[IO.Path]::GetFullPath($OutputDirectory)
$workRoot=[IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($DependencyList))
$workPrefix=$workRoot.TrimEnd([IO.Path]::DirectorySeparatorChar)+[IO.Path]::DirectorySeparatorChar
if($outputFull -ine $workRoot -and !$outputFull.StartsWith($workPrefix,[StringComparison]::OrdinalIgnoreCase)){throw 'Protection output must be inside the build workspace'}
for($p=$outputFull;$p;$p=[IO.Path]::GetDirectoryName($p)){
 if((Test-Path -LiteralPath $p) -and ((Get-Item -LiteralPath $p).Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'Protection output includes a reparse point'}
}
foreach($file in @($InputAssembly,$DotnetExe,$ObfuscarDll)){if(!(Test-Path -LiteralPath $file -PathType Leaf)){throw "Missing protection input: $file"}}
New-Item -ItemType Directory -Path $outputFull -Force | Out-Null
[xml]$config=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'obfuscar.xml') -Raw
$config.Obfuscator.Var | ForEach-Object {
 if($_.name -eq 'InPath'){$_.value=[IO.Path]::GetDirectoryName($InputAssembly)}
 if($_.name -eq 'OutPath'){$_.value=$outputFull}
}
$config.Obfuscator.Module.file=$InputAssembly
foreach($folder in (Get-Content -LiteralPath $DependencyList | Where-Object {$_ -and (Test-Path -LiteralPath $_ -PathType Leaf)} | ForEach-Object {[IO.Path]::GetDirectoryName($_)} | Sort-Object -Unique)){
 $element=$config.CreateElement('AssemblySearchPath');$element.SetAttribute('path',$folder);[void]$config.Obfuscator.AppendChild($element)
}
foreach($framework in @('Microsoft.NETCore.App','Microsoft.WindowsDesktop.App')){
 $version=Get-ChildItem -LiteralPath (Join-Path $RuntimeRoot ('shared\'+$framework)) -Directory | Where-Object {$_.Name -match '^[68]\.0\.\d+$'} | Sort-Object {[version]$_.Name} -Descending | Select-Object -First 1
 if(!$version){throw "Missing framework: $framework"}
 $element=$config.CreateElement('AssemblySearchPath');$element.SetAttribute('path',$version.FullName);[void]$config.Obfuscator.AppendChild($element)
}
$configPath=Join-Path $outputFull 'obfuscar.resolved.xml'
$config.Save($configPath)
$protected=Join-Path $outputFull ([IO.Path]::GetFileName($InputAssembly))
# An unsuccessful run must never reuse an earlier protected file.
if(Test-Path -LiteralPath $protected){Remove-Item -LiteralPath $protected -Force}
& $DotnetExe $ObfuscarDll $configPath *> (Join-Path $outputFull 'obfuscar.log')
if($LASTEXITCODE -ne 0){Get-Content -LiteralPath (Join-Path $outputFull 'obfuscar.log') -Tail 25;throw 'Obfuscar failed'}
if(!(Test-Path -LiteralPath $protected -PathType Leaf)){throw 'Protected assembly was not produced'}
$before=Digest $InputAssembly
$after=Digest $protected
if($before -eq $after){throw 'Obfuscation did not change the assembly'}
$mapping=Join-Path $outputFull 'Mapping.txt'
if(!(Test-Path -LiteralPath $mapping -PathType Leaf)){throw 'Obfuscation mapping was not produced'}
$changed=@(Get-Content -LiteralPath $mapping | Where-Object {$_ -match ' -> '}).Count
if($changed -lt 1){throw 'Obfuscation did not rename any eligible members'}
$receipt=@{schema_version=1;tool='Obfuscar';version='2.2.50';original_sha256=$before;protected_sha256=$after;original_path=$InputAssembly;protected_path=$protected;policy_sha256=(Digest (Join-Path $PSScriptRoot 'obfuscar.xml'));mapping_changed_entries=$changed}
[IO.File]::WriteAllText((Join-Path $outputFull 'protection.json'),($receipt|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
Write-Output "Protected managed assembly: $after"
