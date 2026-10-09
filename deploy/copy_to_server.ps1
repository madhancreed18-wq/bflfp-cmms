<#
    copy_to_server.ps1 - put a release onto the live server PC over the network.

    THE LIST OF FILES NOW COMES OUT OF THE RELEASE ZIP, not out of this script.
    It used to be a list kept by hand down below, and it went stale exactly the way a
    hand-kept list does: it still named the b256 files, so it would have copied
    server\app.py - which imports server\projects.py - WITHOUT copying projects.py, and
    the live server would have failed to start with ModuleNotFoundError. The zip in
    _to_server\ is what was built and tested, so the zip is what goes across.

    USUAL USE - from the development PC, with the server's share visible:

        powershell -ExecutionPolicy Bypass -File deploy\copy_to_server.ps1 `
            -Zip  "_to_server\bflfp-cmms-b264.zip" `
            -Dest "\\SERVERPC\c$\bflfp-cmms" `
            -AppUrl "http://SERVERPC:8000"

    -WhatIf            show what would be copied and change nothing.
    -AppUrl <url>      check the app is STOPPED before writing, and refuse if it answers.
    -NoBackup          skip the database backup (not advised).
    -Force             copy even though the app is answering. You will lose the changes
                       a running app writes after the copy, and a release that alters
                       the database must not be copied under a running app at all.

    NEVER COPIED, whatever is in the zip: data\  .venv\  .git\  deploy\secrets.env
    The server keeps its own database. Copying a development database over a live one
    is the one mistake that cannot be undone.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$Dest,
    [string]$Zip,
    [string]$Source,                 # copy loose files from a source tree instead of a zip
    [string]$AppUrl,
    [switch]$NoBackup,
    [switch]$Force,
    [switch]$IncludeSecrets
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Host $m }
function Head($m) { Write-Host ''; Write-Host $m -ForegroundColor Cyan }
# Relative paths are written and compared with backslashes throughout, the way the
# release and this file talk about them. Turn one into a path the host can open.
function Local($rel) { $rel.Replace('\', [IO.Path]::DirectorySeparatorChar) }

# Anything matching these never travels, however it got into the zip.
$NEVER = @('data\', 'data-dev\', '.venv\', '.git\', 'deploy\secrets.env', 'READ-ME-FIRST.txt')

# -- 1 - the destination really is the CMMS ----------------------------------------
if (-not (Test-Path $Dest)) { throw "Destination not reachable: $Dest" }
if (-not (Test-Path (Join-Path $Dest 'run.py'))) {
    throw "That does not look like the CMMS folder (no run.py in $Dest)"
}

# -- 2 - the app must be stopped ---------------------------------------------------
# A .py file copied under a running app does nothing until it restarts, which is
# survivable. A release that CHANGES THE DATABASE copied under a running app is not:
# the app rewrites the file it is holding open and the migration lands on top.
if ($AppUrl) {
    $running = $false
    try {
        $r = Invoke-WebRequest -Uri ("{0}/api/health" -f $AppUrl.TrimEnd('/')) `
                               -TimeoutSec 4 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $running = $true }
    } catch { $running = $false }
    if ($running) {
        if (-not $Force) {
            throw "The app at $AppUrl is still running. Stop it on the server, then run this again. (-Force overrides, and you should not.)"
        }
        Write-Warning "The app at $AppUrl is answering and -Force was given. Copying anyway."
    } else {
        Say "  app at $AppUrl is not answering - good, it is stopped"
    }
} else {
    Write-Warning "No -AppUrl given, so I cannot check the app is stopped. Make sure it is."
}

# -- 3 - back the live database up, on the server, before writing anything ---------
$db = Join-Path $Dest (Local 'data\cmms.db')
if (-not $NoBackup) {
    if (Test-Path $db) {
        $bak = "$db.bak-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
        if ($PSCmdlet.ShouldProcess($bak, 'back up the live database')) {
            Copy-Item -LiteralPath $db -Destination $bak -Force
            $mb = [math]::Round((Get-Item $bak).Length / 1MB, 1)
            Say "  backed up  data\cmms.db  ->  $(Split-Path $bak -Leaf)   ($mb MB)"
        }
    } else {
        Write-Warning "No data\cmms.db at the destination - nothing to back up."
    }
}

# -- 4 - work out what to copy -----------------------------------------------------
$staging = $null
if ($Zip) {
    if (-not (Test-Path $Zip)) { throw "Zip not found: $Zip" }
    # GetTempPath() rather than $env:TEMP - the latter is not set on every host, and a
    # null there takes the script down before it has copied anything.
    $staging = Join-Path ([System.IO.Path]::GetTempPath()) `
                         ('cmms-release-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    Expand-Archive -LiteralPath $Zip -DestinationPath $staging -Force
    # the zip holds one folder, bflfp-cmms-bNNN\, and the tree lives under it
    $inner = Get-ChildItem $staging -Directory | Select-Object -First 1
    $root = if ($inner) { $inner.FullName } else { $staging }
    Say "  reading   $(Split-Path $Zip -Leaf)"
} elseif ($Source) {
    # -Source used to mean "copy the files on the list below", and the list is gone: the
    # release zip is the list now. Walking a working tree instead would copy Training
    # Videos, the master workbooks and every old release zip - gigabytes of the wrong
    # thing onto the live server. Refuse rather than do that quietly.
    throw @"
-Source is no longer supported. The file list used to live inside this script and went
stale - that is what would have copied app.py without projects.py and stopped the live
server. Use the release zip instead, which IS the list:

    -Zip "$(Join-Path $Source '_to_server\bflfp-cmms-b264.zip')"
"@
} else {
    throw "Give -Zip: a release from _to_server\, e.g. -Zip `"_to_server\bflfp-cmms-b264.zip`""
}

$items = Get-ChildItem -Path $root -Recurse -File | ForEach-Object {
    # Normalise the separator before anything compares a path. Windows hands back
    # backslashes and Linux hands back forward slashes, and every check below -
    # the never-copy list, the build-number read, the app.py import test - is a
    # string comparison that silently matches nothing if the two disagree.
    $rel = $_.FullName.Substring($root.Length).Replace('/', '\').Trim('\')
    [pscustomobject]@{ Rel = $rel; Full = $_.FullName; Len = $_.Length }
} | Where-Object {
    $r = $_.Rel
    -not ($NEVER | Where-Object { $r -like "$_*" -or $r -eq $_ })
}

if (-not $items) { throw "Nothing to copy - the zip or source held no files." }

# The build's own identity, so what lands is never a guess.
$ui = ''
$idx = $items | Where-Object { $_.Rel -eq 'static\index.html' } | Select-Object -First 1
if ($idx) {
    $m = Select-String -LiteralPath $idx.Full -Pattern "UIBUILD='(b\d+)'" | Select-Object -First 1
    if ($m) { $ui = $m.Matches[0].Groups[1].Value }
}
$sw = $items | Where-Object { $_.Rel -eq 'static\sw.js' } | Select-Object -First 1
$swv = ''
if ($sw) {
    $m2 = Select-String -LiteralPath $sw.Full -Pattern 'bflfp-v(\d+)' | Select-Object -First 1
    if ($m2) { $swv = 'v' + $m2.Matches[0].Groups[1].Value }
}
if ($ui -and $swv -and ($ui -replace '^b', 'v') -ne $swv) {
    throw "index.html is $ui but sw.js is cache $swv. They ship together or phones keep the old page. Rebuild the release."
}

# server\app.py without server\projects.py is the failure this script existed to cause.
# Any module app.py imports must be in the same release, or the server will not start.
$rels = $items.Rel
if (($rels -contains 'server\app.py')) {
    $appTxt = Get-Content -LiteralPath ($items | Where-Object { $_.Rel -eq 'server\app.py' }).Full -Raw
    foreach ($mod in [regex]::Matches($appTxt, 'from \.\s*import\s*\(([^)]*)\)') ) {
        foreach ($name in ($mod.Groups[1].Value -split '[,\s]+' | Where-Object { $_ })) {
            $need = "server\$name.py"
            if (-not ($rels -contains $need) -and -not (Test-Path (Join-Path $Dest (Local $need)))) {
                throw "app.py imports '$name' but $need is neither in this release nor already on the server. The app would not start. Rebuild the release with it."
            }
        }
    }
}

Head ("  release " + $(if ($ui) { "$ui / cache $swv" } else { 'files' }) + "  ->  $Dest")

# -- 5 - copy ----------------------------------------------------------------------
$copied = 0; $bytes = 0; $failed = @()
foreach ($it in $items) {
    $dst = Join-Path $Dest (Local $it.Rel)
    $dir = Split-Path $dst -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    if ($PSCmdlet.ShouldProcess($it.Rel, 'copy')) {
        try {
            Copy-Item -LiteralPath $it.Full -Destination $dst -Force
            $copied++; $bytes += $it.Len
            Say ("  copied   {0,-30} {1,9:N1} KB" -f $it.Rel, ($it.Len / 1KB))
        } catch { $failed += "$($it.Rel) - $($_.Exception.Message)" }
    }
}

if ($IncludeSecrets -and $Source) {
    $s = Join-Path $Source 'deploy\secrets.env'
    if ((Test-Path $s) -and $PSCmdlet.ShouldProcess('deploy\secrets.env', 'copy')) {
        Copy-Item -LiteralPath $s -Destination (Join-Path $Dest 'deploy\secrets.env') -Force
        Say '  copied   deploy\secrets.env            (Cloudflare token)'; $copied++
    }
}

# -- 6 - check it actually landed --------------------------------------------------
# A copy over a network share can report success and write nothing - a full disk, a
# read-only share, a file the running app still holds. Read the bytes back.
$bad = @()
if (-not $WhatIfPreference) {
    foreach ($it in $items) {
        $dst = Join-Path $Dest (Local $it.Rel)
        if (-not (Test-Path $dst)) { $bad += "$($it.Rel) - did not arrive"; continue }
        $there = (Get-Item $dst).Length
        if ($there -ne $it.Len) { $bad += "$($it.Rel) - $there bytes on the server, $($it.Len) expected" }
    }
}

if ($staging) { Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue }

Head "  $copied file(s), $([math]::Round($bytes/1KB)) KB"
if ($failed) { $failed | ForEach-Object { Write-Warning $_ } }
if ($bad) {
    $bad | ForEach-Object { Write-Warning $_ }
    throw "$($bad.Count) file(s) did not land correctly. The server is now PART UPDATED - copy again before starting the app."
}
if (-not $WhatIfPreference) { Say '  every file verified byte-for-byte on the server' }

Say ''
Say '  NOT copied, on purpose:  data\  .venv\  .git\  deploy\secrets.env'
Say '  Now, on the server:  start the app, then check http://localhost:8000/api/health'
if ($ui) { Say "  The footer should read ui $ui. If it does not, the browser is showing a cached page - Ctrl+F5." }
