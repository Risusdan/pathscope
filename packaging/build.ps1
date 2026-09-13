# Build the pathscope onedir bundle with PyInstaller and zip it for
# release (Windows). Run from anywhere; this script resolves the repo
# root itself.
#
# Output: dist\pathscope\ (the onedir bundle, executable at
# dist\pathscope\pathscope.exe) with targets\ copied in beside it -
# never inside the PyInstaller-collected tree, per spec section 3 -
# plus dist\pathscope-<version>-windows.zip containing both.
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$VenvPip = Join-Path $RepoRoot ".venv\Scripts\pip.exe"
$VenvPyInstaller = Join-Path $RepoRoot ".venv\Scripts\pyinstaller.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Error "error: $VenvPython not found - create .venv first"
    exit 1
}

Write-Host "== installing build extra =="
& $VenvPip install -e ".[ui,build]"

Write-Host "== cleaning previous build output =="
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist

Write-Host "== running pyinstaller =="
& $VenvPyInstaller --noconfirm --clean packaging\pathscope.spec

Write-Host "== copying targets\ next to the executable =="
Copy-Item -Recurse targets dist\pathscope\targets

Write-Host "== zipping release archive =="
$VersionScript = @'
import re
text = open("pyproject.toml").read()
print(re.search(r"version = \"([^\"]+)\"", text).group(1))
'@
$Version = & $VenvPython -c $VersionScript
$ZipName = "pathscope-$Version-windows.zip"
$ZipPath = Join-Path "dist" $ZipName

if (Test-Path $ZipPath) {
    Remove-Item -Force $ZipPath
}
Compress-Archive -Path dist\pathscope -DestinationPath $ZipPath

Write-Host "== built $ZipPath =="
