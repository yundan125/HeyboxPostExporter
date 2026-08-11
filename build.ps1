$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot
$IconFileName = "app-icon.png"
$IconSource = Join-Path $ProjectRoot $IconFileName

if (-not (Test-Path -LiteralPath $IconSource -PathType Leaf)) {
    throw "Application icon not found: $IconSource"
}

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

& ".venv\Scripts\python.exe" -m pip install -i "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple" -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed with exit code $LASTEXITCODE."
}
$Architecture = & ".venv\Scripts\python.exe" -c "import platform; print(platform.architecture()[0])"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to determine Python architecture (exit code $LASTEXITCODE)."
}
if ($Architecture -ne "64bit") {
    throw "A 64-bit Python runtime is required."
}
$PythonBasePrefix = & ".venv\Scripts\python.exe" -c "import sys; print(sys.base_prefix)"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to determine the Python base prefix (exit code $LASTEXITCODE)."
}
$RuntimeLibraryBin = Join-Path $PythonBasePrefix "Library\bin"
if (Test-Path -LiteralPath $RuntimeLibraryBin -PathType Container) {
    $env:PATH = "$RuntimeLibraryBin;$env:PATH"
}

$StagingDist = Join-Path $ProjectRoot "build\release-staging"
$StagedApp = Join-Path $StagingDist "HeyboxPostExporter"
$SingleFileStaging = Join-Path $ProjectRoot "build\singlefile-staging"
$SingleFileExe = Join-Path $ProjectRoot "dist\HeyboxPostExporter.exe"
$ReleaseApp = Join-Path $ProjectRoot "dist\HeyboxPostExporter"
$ReleaseArchive = Join-Path $ProjectRoot "dist\HeyboxPostExporter_Windows_x64.zip"

& ".venv\Scripts\pyinstaller.exe" `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name "HeyboxPostExporter" `
    --icon $IconFileName `
    --add-data "$IconFileName;." `
    --distpath $StagingDist `
    --paths "src" `
    --exclude-module "PIL" `
    --exclude-module "playwright" `
    --exclude-module "heybox_exporter.browser" `
    --exclude-module "heybox_exporter.browser_connection" `
    "src\heybox_exporter\gui_entry.py"
if ($LASTEXITCODE -ne 0) {
    throw "The onedir build failed with exit code $LASTEXITCODE."
}

& ".venv\Scripts\pyinstaller.exe" `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "HeyboxPostExporter" `
    --icon $IconSource `
    --add-data "$IconSource;." `
    --distpath $SingleFileStaging `
    --workpath "build\HeyboxPostExporter-onefile" `
    --specpath "build" `
    --paths "src" `
    --exclude-module "PIL" `
    --exclude-module "playwright" `
    --exclude-module "heybox_exporter.browser" `
    --exclude-module "heybox_exporter.browser_connection" `
    "src\heybox_exporter\gui_entry.py"
if ($LASTEXITCODE -ne 0) {
    throw "The single-file build failed with exit code $LASTEXITCODE."
}

try {
    if (Test-Path -LiteralPath $ReleaseApp) {
        $ResolvedRelease = [IO.Path]::GetFullPath($ReleaseApp)
        $ExpectedRelease = [IO.Path]::GetFullPath((Join-Path $ProjectRoot "dist\HeyboxPostExporter"))
        if ($ResolvedRelease -ne $ExpectedRelease -or (Split-Path -Leaf $ResolvedRelease) -ne "HeyboxPostExporter") {
            throw "Refusing to clean unexpected release path: $ResolvedRelease"
        }
        Remove-Item -LiteralPath $ResolvedRelease -Recurse -Force
    }
    New-Item -ItemType Directory -Path $ReleaseApp -Force | Out-Null
    Copy-Item -Path (Join-Path $StagedApp "*") -Destination $ReleaseApp -Recurse -Force
    $PublishedApp = $ReleaseApp
} catch {
    $Fallback = Join-Path $ProjectRoot ("dist\HeyboxPostExporter_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
    New-Item -ItemType Directory -Path $Fallback -Force | Out-Null
    Copy-Item -Path (Join-Path $StagedApp "*") -Destination $Fallback -Recurse -Force
    $PublishedApp = $Fallback
    Write-Warning "Stable release directory is in use; published to $Fallback"
}

Copy-Item -LiteralPath (Join-Path $SingleFileStaging "HeyboxPostExporter.exe") -Destination $SingleFileExe -Force

if (Test-Path -LiteralPath $ReleaseArchive) {
    Remove-Item -LiteralPath $ReleaseArchive -Force
}
Compress-Archive -LiteralPath $StagedApp -DestinationPath $ReleaseArchive -CompressionLevel Optimal

Write-Host "Build complete: $PublishedApp\HeyboxPostExporter.exe"
Write-Host "Single-file build: $SingleFileExe"
Write-Host "Release archive: $ReleaseArchive"
Write-Host "Browser sidecar uses system npx + chrome-devtools-mcp@1.6.0 --autoConnect with the normal Edge User Data."
