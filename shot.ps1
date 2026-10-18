# ============================================================================
# shot.ps1 -- render a page and save a PNG so Claude Code can "see" it.
#
# Claude Code cannot see your browser window or the VS Code preview pane.
# It CAN read image files. So: render -> PNG -> Claude reads the PNG.
#
# Usage (from the project root):
#     .\shot.ps1                                   # homepage -> outputs\shots\
#     .\shot.ps1 -Url http://127.0.0.1:8000/farm.html
#     .\shot.ps1 -Url http://127.0.0.1:8000/library.html -Name library -Height 3000
#     .\shot.ps1 -Url http://127.0.0.1:8000/ -Dom  # also dump rendered DOM
#
# Requires: the backend running (.\start.ps1). Assets use absolute /assets/...
# paths, so file:// will render unstyled -- always point -Url at http://127.0.0.1.
# ============================================================================

param(
    [string]$Url = "http://127.0.0.1:8000/",
    [string]$Name,
    [string]$OutDir = "outputs\shots",
    [int]$Width = 1440,
    [int]$Height = 2000,
    [int]$WaitMs = 6000,
    [switch]$Dom,
    [string]$Browser
)

$root = $PSScriptRoot
if (-not [System.IO.Path]::IsPathRooted($OutDir)) { $OutDir = Join-Path $root $OutDir }
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

function Find-Browser($hint) {
    if ($hint) {
        if (Test-Path $hint) { return $hint }
        throw "Browser not found at: $hint"
    }
    $cands = @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    )
    foreach ($c in $cands) { if (Test-Path $c) { return $c } }
    throw "No Chrome or Edge found. Pass -Browser <path-to-exe>."
}
$exe = Find-Browser $Browser

if (-not $Name) {
    $slug = ($Url -replace '^https?://', '') -replace '[^A-Za-z0-9]+', '_'
    $slug = $slug.Trim('_')
    if (-not $slug) { $slug = "page" }
    $Name = $slug
}

$png  = Join-Path $OutDir "$Name.png"
$prof = Join-Path $env:TEMP "ccshot_$Name"
if (Test-Path $png) { Remove-Item $png }

$chromeArgs = @(
    "--headless", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
    "--user-data-dir=$prof",
    "--window-size=$Width,$Height",
    "--virtual-time-budget=$WaitMs",
    "--screenshot=$png",
    $Url
)
try { & $exe @chromeArgs 2>$null | Out-Null } catch { }
if (-not (Test-Path $png)) { throw "Screenshot failed for $Url (no PNG produced)." }
Write-Host "PNG -> $png"

if ($Dom) {
    $domPath = Join-Path $OutDir "$Name.dom.html"
    try {
        & $exe --headless --disable-gpu --no-first-run "--user-data-dir=$prof" `
            "--virtual-time-budget=$WaitMs" --dump-dom $Url 2>$null |
            Out-File -FilePath $domPath -Encoding utf8
    } catch { }
    if (Test-Path $domPath) { Write-Host "DOM -> $domPath" }
}
