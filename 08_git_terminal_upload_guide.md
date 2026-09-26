# Uploading to GitHub via terminal (public repo)

Run these in PowerShell, in the folder containing all the pipeline files
(`mega_to_youtube.py`, `drive_to_mega_youtube.py`, `verify_*.py`,
`match_local_files.py`, `requirements.txt`, `.gitignore`, both
`*-workflow.yml` files, both `*.yaml.template` files).

## 1. Create the repo on GitHub (via browser, one-time)

Go to https://github.com/new — name it (e.g. `mega2yt-pipeline`), set
visibility to **Public** (as decided), **don't** check "Add a README" (you
already have files to push), click **Create repository**. Leave the page
open — it shows you the exact remote URL you'll need next.

## 2. Push your local files (PowerShell, from your files' folder)

```powershell
cd C:\path\to\your\pipeline\files

git init
git add .
git status
```

**Check the `git status` output carefully before committing** — confirm
you do NOT see `config.yaml`, `config_drive_source.yaml`, or any
`credentials/` or `*.json` files listed. The `.gitignore` you have should
already exclude these, but this is your last checkpoint before anything
becomes public.

```powershell
git commit -m "Initial pipeline setup"
git branch -M main
git remote add origin https://github.com/<your-username>/mega2yt-pipeline.git
git push -u origin main
```

You'll be prompted to authenticate — either a browser popup (if you have
GitHub CLI/Git Credential Manager set up, common default on Windows) or a
Personal Access Token if prompted for a password (GitHub no longer accepts
your account password directly for git operations — if asked, generate one
at https://github.com/settings/tokens with `repo` scope, use it as the
password).

## 3. Verify what's actually public

Refresh the repo page in your browser. Click through the file list —
confirm only the intended files are there (scripts, workflow files,
templates, `.gitignore`) and nothing under `credentials/` or any filled-in
config file.

## 4. Workflow folder structure — already done for you

The two workflow YAML files must live in `.github/workflows/` for GitHub to
pick them up. **This repo already has them in the right place** — the initial
commit was made with them there, so there is nothing to move.

If you ever need to recreate it from scratch:

```powershell
mkdir .github\workflows
move mega-to-youtube-workflow.yml .github\workflows\
move drive-to-mega-youtube-workflow.yml .github\workflows\
```

After any push, confirm both workflows appear under the repo's **Actions**
tab. If they don't, the files are in the wrong path.

## 5. Add your Secrets (browser, one-time each)

Repo page → **Settings → Secrets and variables → Actions → New repository
secret**. Add each one from your earlier setup: `CONFIG_YAML`,
`CLIENT_SECRET_CHANNEL1-5`, `TOKEN_CHANNEL1-5`, `CONFIG_DRIVE_SOURCE_YAML`,
`CLIENT_SECRET_DRIVE_SOURCE`, `TOKEN_DRIVE_SOURCE`, and
`CLIENT_SECRET_DRIVE`/`TOKEN_DRIVE` if using Pipeline A's optional Drive
backup. These stay encrypted and hidden regardless of the repo being
public — this is the one part of the setup that's safe either way.

## 6. Future updates (whenever you edit a script locally)

```powershell
git add .
git status   # check again — same habit every time
git commit -m "describe what changed"
git push
```
