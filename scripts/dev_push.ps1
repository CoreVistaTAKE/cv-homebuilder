param(
  [Parameter(Mandatory=$true)][string]$Msg
)

# 変更があるか確認
$changes = git status --porcelain
if (-not $changes) {
  Write-Host "No changes. Nothing to commit."
  exit 0
}

git add -A
git commit -m $Msg
git push
Write-Host "Pushed."