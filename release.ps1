# Tag a release: bump the version, commit, tag, and optionally push.
#
#   ./release.ps1 0.2.0          # bump + commit + tag locally
#   ./release.ps1 0.2.0 --push   # ... and push the commit and tag
#
# Pushing the v* tag makes GitHub Actions run the tests, build and publish the
# multi-arch image to GHCR (0.2.0, 0.2, latest) and open a release.
[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)][string]$Version,
    [Alias('p')][switch]$Push
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$Version = $Version.TrimStart('v')
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    Write-Error "version must look like 1.2.3 (got '$Version')"
    exit 1
}

if (git status --porcelain) {
    Write-Error 'working tree is dirty; commit or stash first'
    exit 1
}
git rev-parse -q --verify "refs/tags/v$Version" > $null
if ($LASTEXITCODE -eq 0) {
    Write-Error "tag v$Version already exists"
    exit 1
}

$targets = @{
    'pyproject.toml'                         = 'version = '
    'src/trilium_calendar_mcp/__init__.py'   = '__version__ = '
}
foreach ($file in $targets.Keys) {
    $pattern = if ($file -like '*.toml') { '^version = "[^"]+"' } else { '^__version__ = "[^"]+"' }
    $replacement = if ($file -like '*.toml') { "version = `"$Version`"" } else { "__version__ = `"$Version`"" }
    $content = Get-Content -Raw -Path $file
    $updated = [regex]::Replace($content, $pattern, $replacement, 1, [TimeSpan]::Zero)
    if ($updated -eq $content) { Write-Error "could not update the version in $file"; exit 1 }
    Set-Content -Path $file -Value $updated -NoNewline
    Write-Host "  ~ $file -> $Version"
}

git add pyproject.toml src/trilium_calendar_mcp/__init__.py
if (git status --porcelain) {
    git commit -m "chore: release v$Version"
}
else {
    Write-Host "  = version is already $Version; tagging the current commit"
}
git tag -a "v$Version" -m "v$Version"
Write-Host "tagged v$Version"

if ($Push) {
    git push
    git push --tags
    Write-Host 'pushed; GitHub Actions will build the image and open the release'
}
else {
    Write-Host 'not pushed (re-run with --push, or: git push && git push --tags)'
}
