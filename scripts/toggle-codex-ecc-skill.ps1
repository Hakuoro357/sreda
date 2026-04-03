[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("enable", "disable", "status")]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [ValidateSet("ecc-security-review", "ecc-verification-loop", "ecc-deep-research", "ecc-tdd-workflow")]
    [string]$Skill
)

$ErrorActionPreference = "Stop"

$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$skillsDir = Join-Path $codexHome "skills"
$disabledDir = Join-Path $codexHome "skills-disabled"
$enabledPath = Join-Path $skillsDir $Skill
$disabledPath = Join-Path $disabledDir $Skill

switch ($Action) {
    "status" {
        if (Test-Path -LiteralPath $enabledPath) {
            Write-Output "enabled"
        } elseif (Test-Path -LiteralPath $disabledPath) {
            Write-Output "disabled"
        } else {
            Write-Output "missing"
        }
    }
    "disable" {
        if (-not (Test-Path -LiteralPath $enabledPath)) {
            Write-Output "already-disabled-or-missing"
            return
        }
        New-Item -ItemType Directory -Force -Path $disabledDir | Out-Null
        Move-Item -LiteralPath $enabledPath -Destination $disabledPath -Force
        Write-Output "disabled"
    }
    "enable" {
        if (-not (Test-Path -LiteralPath $disabledPath)) {
            Write-Output "already-enabled-or-missing"
            return
        }
        New-Item -ItemType Directory -Force -Path $skillsDir | Out-Null
        Move-Item -LiteralPath $disabledPath -Destination $enabledPath -Force
        Write-Output "enabled"
    }
}
