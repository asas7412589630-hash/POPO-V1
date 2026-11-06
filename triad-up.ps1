<#
.SYNOPSIS
  triad-up.ps1 — Windows PowerShell launcher for Triad / POPO-V1 armor proxy.
.DESCRIPTION
  Manages starting, restarting, stopping, and status check of the local proxy on Windows.
.EXAMPLE
  .\triad-up.ps1
  .\triad-up.ps1 -Status
  .\triad-up.ps1 -Restart
  .\triad-up.ps1 -Stop
#>
param(
    [switch]$Status,
    [switch]$Restart,
    [switch]$Stop,
    [switch]$Foreground
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$triadPy = Join-Path $scriptDir "triad.py"

if ($Status) {
    python $triadPy --status
} elseif ($Restart) {
    python $triadPy --restart
} elseif ($Stop) {
    python $triadPy --stop
} elseif ($Foreground) {
    python $triadPy --foreground
} else {
    python $triadPy
}
