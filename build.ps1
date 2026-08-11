$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

& ".venv\Scripts\python.exe" -m pip install -i "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple" -r requirements-dev.txt
$Architecture = & ".venv\Scripts\python.exe" -c "import platform; print(platform.architecture()[0])"
if ($Architecture -ne "64bit") {
    throw "A 64-bit Python runtime is required."
}

$StagingDist = Join-Path $ProjectRoot "build\release-staging"
$StagedApp = Join-Path $StagingDist "HeyboxPostExporter"
$SingleFileStaging = Join-Path $ProjectRoot "build\singlefile-staging"
$SingleFileExe = Join-Path $ProjectRoot "dist\HeyboxPostExporter.exe"
$ReleaseApp = Join-Path $ProjectRoot "dist\HeyboxPostExporter"

& ".venv\Scripts\pyinstaller.exe" `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name "HeyboxPostExporter" `
    --distpath $StagingDist `
    --paths "src" `
    --exclude-module "playwright" `
    --exclude-module "heybox_exporter.browser" `
    --exclude-module "heybox_exporter.browser_connection" `
    "src\heybox_exporter\gui_entry.py"

& ".venv\Scripts\pyinstaller.exe" `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "HeyboxPostExporter" `
    --distpath $SingleFileStaging `
    --workpath "build\HeyboxPostExporter-onefile" `
    --specpath "build" `
    --paths "src" `
    --exclude-module "playwright" `
    --exclude-module "heybox_exporter.browser" `
    --exclude-module "heybox_exporter.browser_connection" `
    "src\heybox_exporter\gui_entry.py"

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

Write-Host "Build complete: $PublishedApp\HeyboxPostExporter.exe"
Write-Host "Single-file build: $SingleFileExe"
Write-Host "Browser sidecar uses system npx + chrome-devtools-mcp@1.6.0 --autoConnect with the normal Edge User Data."
