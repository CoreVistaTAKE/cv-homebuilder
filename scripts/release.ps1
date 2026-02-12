param(
  [Parameter(Mandatory=$true)][string]$Version,
  [Parameter(Mandatory=$true)][string]$Summary,
  [Parameter(Mandatory=$true)][string]$NextTarget
)

$today = Get-Date -Format "yyyy-MM-dd"

# --- 1) VERSION を更新 ---
Set-Content -Path "VERSION" -Value $Version -NoNewline

# --- 2) CHANGELOG.md に追記（先頭に入れる） ---
$changelogPath = "CHANGELOG.md"
if (-not (Test-Path $changelogPath)) {
  Set-Content $changelogPath "# Changelog`n"
}

$old = Get-Content $changelogPath -Raw
if ($old -notmatch "^# Changelog") {
  # もし形式が違うなら先頭にヘッダを付ける（壊れにくくする）
  $old = "# Changelog`n`n" + $old
}

$newEntry = "## [$Version] - $today`n- $Summary`n`n"
# "# Changelog" の直後に挿入
$updated = $old -replace "(?s)^# Changelog\s*\n", "# Changelog`n`n$newEntry"
Set-Content $changelogPath $updated

# --- 3) docs/STATUS.md を更新（Current Version / Next Target を置換） ---
$statusPath = "docs/STATUS.md"
if (-not (Test-Path $statusPath)) {
  New-Item -ItemType Directory -Force -Path "docs" | Out-Null
  Set-Content $statusPath "# Project Status`n`n- Current Version: $Version`n- Next Target: $NextTarget`n"
} else {
  $status = Get-Content $statusPath -Raw
  if ($status -match "- Current Version:") {
    $status = $status -replace "- Current Version:.*", "- Current Version: $Version"
  } else {
    $status += "`n- Current Version: $Version"
  }

  if ($status -match "- Next Target:") {
    $status = $status -replace "- Next Target:.*", "- Next Target: $NextTarget"
  } else {
    $status += "`n- Next Target: $NextTarget"
  }

  Set-Content $statusPath $status
}

# --- 4) commit ---
git add -A
git commit -m "Release v$Version"

# --- 5) tag（annotated） ---
git tag -a "v$Version" -m "v$Version: $Summary"

# --- 6) push（commit + tag） ---
git push
git push origin "v$Version"

Write-Host "Released v$Version and pushed tag."