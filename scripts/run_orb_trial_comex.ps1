$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$outDir = "run\trials"
$config = "config\paper.tws.orb_a.comex_rth.local.yaml"

& $python -m trader trial -c $config --minutes 360 --outdir $outDir --export-days 14

$retentionDays = -30
$cutoff = (Get-Date).AddDays($retentionDays)

Get-ChildItem -Path (Join-Path $outDir "exports") -File -Recurse -ErrorAction SilentlyContinue |
  Where-Object { $_.LastWriteTime -lt $cutoff } |
  Remove-Item -Force

Get-ChildItem -Path (Join-Path $outDir "reports") -File -Recurse -ErrorAction SilentlyContinue |
  Where-Object { $_.LastWriteTime -lt $cutoff } |
  Remove-Item -Force
