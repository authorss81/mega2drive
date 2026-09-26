# Checking what's uploaded vs. what's remaining

Two separate layers — don't mix them up:
1. **PC → Drive landing folder** (rclone's job) — this file.
2. **Drive → MEGA/YouTube** (the automated pipeline) — use
   `verify_drive_pipeline.py` (or `verify_mega_pipeline.py` for Pipeline A),
   see `MASTER_GUIDE.md`.

## What's left to upload (PC → Drive)

```powershell
rclone copy "C:\path\to\your\videos" gdrive:VideoLanding --dry-run -v
```
`--dry-run` transfers nothing — just prints what *would* upload. Empty
output = fully done.

## What's already uploaded

```powershell
rclone ls gdrive:VideoLanding
```
Add a count:
```powershell
(rclone ls gdrive:VideoLanding | Measure-Object).Count
```

## Combined summary script

Save as `check_progress.ps1`:

```powershell
param(
    [string]$LocalFolder = "C:\path\to\your\videos",
    [string]$RemoteFolder = "gdrive:VideoLanding"
)

Write-Host "`n=== Checking upload progress ===`n" -ForegroundColor Cyan

$localFiles = Get-ChildItem -Path $LocalFolder -Filter *.mkv -Recurse
$localCount = $localFiles.Count
$localSizeGB = [math]::Round(($localFiles | Measure-Object -Property Length -Sum).Sum / 1GB, 2)
Write-Host "Local folder: $localCount file(s), $localSizeGB GB"

$remoteCount = (rclone ls $RemoteFolder | Measure-Object).Count
Write-Host "Already on Drive: $remoteCount file(s)"

Write-Host "`n--- Files still needing upload ---`n" -ForegroundColor Yellow
$remaining = rclone copy $LocalFolder $RemoteFolder --dry-run -v 2>&1 | Select-String "Copied|would copy"
$remaining
$remainingCount = ($remaining | Measure-Object).Count
Write-Host "`nRemaining: $remainingCount file(s) not yet on Drive.`n" -ForegroundColor Cyan

if ($remainingCount -eq 0) {
    Write-Host "Everything local is already uploaded." -ForegroundColor Green
} else {
    Write-Host "Run: rclone copy `"$LocalFolder`" $RemoteFolder --transfers 3 --checkers 4 --progress" -ForegroundColor Yellow
}
```

Run with:
```powershell
.\check_progress.ps1 -LocalFolder "C:\path\to\your\videos" -RemoteFolder "gdrive:VideoLanding"
```

(If PowerShell blocks the script: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then retry.)

## The downstream layer (Drive → MEGA/YouTube)

Once files are confirmed on Drive:
```bash
python3 verify_drive_pipeline.py
```
Reports DONE/PARTIAL/FAILED/NEVER ATTEMPTED for the automated pipeline
stage, independent of what rclone already confirmed above.
