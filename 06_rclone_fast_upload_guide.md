# rclone — fastest PC → Google Drive uploads

`rclone` beats the Drive website/desktop app by uploading multiple files
in parallel, using more of your actual upload bandwidth than a single
browser connection can.

## 1. Install rclone on Windows

```powershell
winget install Rclone.Rclone
```
Or download from https://rclone.org/downloads/ and add to PATH. Verify:
```powershell
rclone version
```

## 2. Set up the Drive remote

```powershell
rclone config
```
- `n` for new remote, name it `gdrive`
- Storage type: `24` (Google Drive)
- client_id / client_secret: leave blank, or paste from
  `client_secret_drive_source.json` to avoid rclone's shared-app limits
- Scope: `2` for `drive.file` (narrower, safer — matches the OAuth scope
  used elsewhere in this setup)
- root_folder_id: leave blank
- Edit advanced config? `n`
- Use auto config? `y` — opens your browser for the Google login step
- `q` to quit the wizard

Confirm:
```powershell
rclone lsd gdrive:
```
Empty result is expected with `drive.file` scope — it only sees folders
rclone itself created. Create your landing folder through rclone so it's
visible going forward:
```powershell
rclone mkdir gdrive:VideoLanding
```

## 3. Tuned settings for this specific machine

(AMD Athlon 200GE, 2 core/4 thread, 8GB RAM, SATA SSD boot + spinning HDD
storage, 40Mbps upload — confirmed via live testing in this conversation
that `--transfers 3` gives the best sustained throughput, ~3 MiB/s /
~25Mbps, with no meaningful gain from going higher.)

```powershell
rclone copy "C:\path\to\your\videos" gdrive:VideoLanding `
  --transfers 3 `
  --checkers 4 `
  --drive-chunk-size 32M `
  --buffer-size 16M `
  --progress
```

## 4. Stopping and resuming — safe, no duplicates

`Ctrl+C` (or Stop, if using a GUI) finishes in-flight chunks then exits
cleanly. Re-running the exact same command later only uploads what's
missing — `rclone copy` always checks the destination first and skips
anything already there. At most the files mid-transfer at the moment you
stopped need re-uploading from scratch; everything already complete stays
complete.

## 5. Testing PC → MEGA speed for comparison (no mega-cmd needed)

Install **MEGAsync** (official GUI, https://mega.nz/desktop) instead —
also does parallel chunked uploads, shows live Mbps directly in its
transfer panel. Drag in the same test video used for the rclone test and
compare the live speed readings directly.

## 6. GUI option (same speed, easier to use — not faster)

**A GUI doesn't transfer any faster** — same rclone engine underneath
either way. If you want one anyway:

- **rclone's own Web GUI**: `rclone rcd --rc-web-gui` — opens a browser
  tab with drag-and-drop and live progress graphs, using your same `gdrive`
  remote and settings.
- **RcloneBrowser** (third-party, traditional two-pane file manager):
  https://github.com/kapitainsky/RcloneBrowser — auto-detects your
  existing `rclone.conf`, no re-authorization needed. Put your tuned flags
  (`--transfers 3 --checkers 4 --drive-chunk-size 32M --buffer-size 16M`)
  in its extra-arguments settings field.

## 7. A resumable pattern for feeding files in over time

```powershell
rclone copy "C:\path\to\your\videos" gdrive:VideoLanding --transfers 3 --checkers 4 --progress
```
Safe to re-run anytime you add new local videos — only uploads what's new.
