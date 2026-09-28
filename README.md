# Clockify Filler

Automatically sync your **GitHub commit activity**, **Google Meet meetings** (with Gemini notes), and **Slack DMs + huddles** into **Clockify** time entries.

---

## What It Does

1. **Fetches GitHub commits** across all branches of your mapped repositories
2. **Fetches Google Meet events** from your calendar (with Gemini meeting notes from Google Drive)
3. **Fetches Slack DMs and huddles** — groups DMs by counterpart's email domain, logs huddles with their exact duration
4. **Maps** repos, meetings, and Slack counterparts to Clockify projects
5. **Generates descriptions** using OpenAI GPT (what was done + why, or summary of the DM)
6. **Creates time entries** in Clockify with accurate timestamps and durations
7. **Spreads hours** from heavy commit days to preceding empty weekdays for realistic time distribution

---

## Prerequisites

- **Python 3.11+**
- **pip** (Python package manager)
- A **GitHub** account with a Personal Access Token
- A **Clockify** account with an API key
- *(Optional)* An **OpenAI** API key for GPT-powered descriptions
- *(Optional)* A **Google Cloud** project for Google Meet + Gemini notes integration
- *(Optional)* A **Slack** app with a User OAuth Token for DM and huddle logging

---

## Installation

### 1. Install Python Dependencies

```bash
pip install requests google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

If you don't need Google Meet integration, only `requests` is required:

```bash
pip install requests
```

### 2. Configure Environment Variables

```bash
cd ~/scripts/clockify_filler
cp .env.example .env
```

Edit `.env` and fill in your actual values (see sections below for how to get each key).

---

## Getting Your API Keys

### GitHub Personal Access Token

1. Go to https://github.com/settings/tokens
2. Click **"Generate new token (classic)"**
3. Name: `clockify-filler`
4. Expiration: 90 days (or your preference)
5. Select scope: **`repo`** (full control of private repositories)
6. Click **"Generate token"**
7. Copy the token (starts with `ghp_...`) into `.env` as `GITHUB_TOKEN`

### Clockify API Key

1. Log in to https://app.clockify.me
2. Go to **User Settings** (click your profile icon > Profile Settings)
3. Scroll to the bottom — find **"API"** section
4. Click **"Generate"** if no key exists
5. Copy the key into `.env` as `CLOCKIFY_API_KEY`

### OpenAI API Key (Optional)

1. Go to https://platform.openai.com/api-keys
2. Click **"Create new secret key"**
3. Copy the key (starts with `sk-...`) into `.env` as `OPENAI_API_KEY`

> Without this key, the script uses a simple keyword-based description generator instead of GPT. It still works fine, just less detailed.

---

## Google Meet + Gemini Notes Setup (Optional)

This enables the script to:
- Pull your Google Meet meetings from Google Calendar
- Find Gemini-generated meeting notes from Google Drive
- Create Clockify entries with meeting duration and summarized notes

### Step 1: Set Up Google Cloud Project

1. Go to https://console.cloud.google.com
2. Select or create a project (e.g., "Default Gemini Project")

### Step 2: Enable Required APIs

Go to **APIs & Services > Library** and enable all three:

- **Google Calendar API** — https://console.cloud.google.com/apis/library/calendar-json.googleapis.com
- **Google Drive API** — https://console.cloud.google.com/apis/library/drive.googleapis.com
- **Google Docs API** — https://console.cloud.google.com/apis/library/docs.googleapis.com

### Step 3: Configure OAuth Consent Screen

1. Go to **APIs & Services > OAuth consent screen**
2. Choose **External** user type
3. Fill in:
   - App name: `Clockify Filler`
   - User support email: your email
   - Developer contact: your email
4. Click **Save and Continue**
5. On the **Scopes** page, add:
   - `https://www.googleapis.com/auth/calendar.readonly`
   - `https://www.googleapis.com/auth/drive.readonly`
   - `https://www.googleapis.com/auth/documents.readonly`
6. On the **Test users** page, add your Google email
7. Save

### Step 4: Create OAuth Credentials

1. Go to **APIs & Services > Credentials**
2. Click **"+ Create Credentials" > OAuth client ID**
3. Application type: **Desktop app**
4. Name: `clockify-filler`
5. Click **Create**
6. Click **"Download JSON"**
7. Move the downloaded `client_secret_*.json` file into the `clockify_filler/` directory

### Step 5: First-Time Authentication

The first time you run the script with Google Meet enabled, it will:
1. Open your browser for Google OAuth
2. Ask you to sign in and approve access
3. Cache the token in `google_token.json` (subsequent runs won't need the browser)

If the token expires, just delete `google_token.json` and run again.

---

## Slack Setup (Optional)

Enables the script to:
- Pull your Slack **DMs, group DMs, and channels** (public + private) in the date range — including **thread replies** — group them by counterpart (by email domain → Clockify project), and use OpenAI to estimate active time spent per conversation per day
- Detect Slack **huddles** and log them with their exact start/end times

### Step 1: Create a Slack App

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. Name it (e.g., `Clockify Filler`) and pick your workspace

### Step 2: Add User Token Scopes

Use **User Token scopes** (not Bot) so the app reads *your* conversations. In the app's sidebar, go to **OAuth & Permissions** → scroll to **User Token Scopes** → **Add an OAuth Scope** and add each of:

- `users:read`
- `users:read.email`
- `im:history`
- `im:read`
- `channels:history`
- `channels:read`
- `groups:history`
- `groups:read`
- `mpim:history`
- `mpim:read`

> `users:read.email` sometimes requires workspace admin approval. If install is blocked, that's usually why.

### Step 3: Install to Workspace

In **OAuth & Permissions**, click **Install to Workspace** (or **Request to Install** if an admin needs to approve). Approve the scopes in the resulting dialog.

### Step 4: Copy the User OAuth Token

After install, **OAuth & Permissions** shows a **User OAuth Token** that starts with `xoxp-...` (not `xoxb-`). Copy it to `.env`:

```
SLACK_USER_TOKEN=xoxp-XXXX-XXXX-XXXX-XXXX
```

### Step 5: (Optional) Test the Fetch

Before running the main sync, you can verify Slack access with the bundled test script, which writes everything it reads to a log file without touching Clockify:

```bash
python3 slack_fetch_test.py --from 2026-04-01 --to 2026-04-20
# Logs to slack_fetch.log in the same directory

python3 slack_fetch_test.py --types im                # DMs only
python3 slack_fetch_test.py --raw-json /tmp/slack.json # also dump raw API payloads
```

The log's final `HUDDLES` section shows per-huddle duration and a per-email total — useful sanity check before running the real sync.

### How Slack DMs Map to Projects

DM time is attributed to the project matching the other person's **email domain**:

| Email Domain | Clockify Project |
|---|---|
| bobbi.com.au | BOBBI |
| firstpage.com.au | First Page AU |
| teamgamdom.com | Gamdom |
| firstpage.nz | First Page NZ |
| outsourcey.com | Outsourcey |
| superistgroup.com | Superist Group |

DMs with counterparts outside those domains fall back to `"Slack - Unassigned"` (create that project in Clockify to capture them, otherwise they're skipped).

For **group DMs and channels** (public + private), time is attributed to a representative participant — the first message sender in the window whose email domain matches the table above, otherwise the first sender with an email (falling back to the member list if nobody else spoke).

**Thread replies** are included: history is scanned up to 180 days before the sync window so replies inside the window are found even when the thread was started earlier (Slack's history API never returns thread replies on its own).

Only conversation days where **you sent at least one message** are logged. Bots (including Slackbot) and users without an email are ignored.

### How Slack Huddles Are Logged

Each Slack huddle becomes its own Clockify entry with the *exact* start/end from Slack's API. Huddles that are still active (no `date_end` yet) are skipped and picked up on the next run.

The counterpart's email domain determines the project using the same table above.

---

## Repository to Clockify Project Mapping

The script maps Git repositories to Clockify projects using this configuration (edit in `sync_clockify.py`):

| Repository | Clockify Project |
|---|---|
| bobbi-web-portal, bobbi-portal-api, bobbi-portal, bobbi-lp, outsourcey-web | BOBBI |
| nexseo, mia, FPAU-STAGING | First Page AU |
| FPNZ-STAGING | First Page NZ |
| gamdom, i18n, gamdon-reporting | Gamdom |
| lisnic, lisnic-frontend | Lisnic |
| Nicks | Nick's Projects |
| Outsourcey-client-portal-v2 | Outsourcey |
| Sentr-CRM | SENTR 2.0 |
| superyoung-web | Super Young |

Unmapped repositories are logged to the console and skipped.

Repos are looked up under each org in `GITHUB_ORGS` and then under your own account (`GITHUB_USERNAME`).

### Commits made under a different git identity

Commits are matched by GitHub login, plus any name or email listed in `GITHUB_AUTHOR_ALIASES`. This catches commits pushed from a machine whose git identity isn't linked to your GitHub account (for example a deploy host committing as "Coolify Host Admin"). Set them in `.env`:

```
GITHUB_AUTHOR_ALIASES=Coolify Host Admin,claude@superistgroup.com
```

---

## Meeting to Clockify Project Mapping

Meetings are mapped by keywords in the meeting title:

| Keyword in Title | Clockify Project |
|---|---|
| bobbi | BOBBI |
| nexseo, first page au, fpau | First Page AU |
| first page nz, fpnz | First Page NZ |
| gamdom, gamedom | Gamdom |
| lisnic | Lisnic |
| nick | Nick's Projects |
| outsourcey | Outsourcey |
| sentr | SENTR 2.0 |
| super young, superyoung | Super Young |

Meetings that don't match any keyword are mapped to **"Google Meet - Unassigned"**.

### Skipped Meetings

The following meetings are automatically excluded (non-work events):

- Hats Off Friday
- Leave: AL, BL, PL, SL, Annual Leave, Birthday Leave, Sick Leave, Personal Leave, Bereavement Leave, Parental Leave
- OOO / Out of Office
- Day Off, Leave Request, On Leave

---

## Usage

### Basic Commands

```bash
# Run for current month (auto-detects first and last day)
python3 sync_clockify.py

# Run for a specific date range
python3 sync_clockify.py --from 2026-03-01 --to 2026-03-31

# Preview what would be created (no changes made)
python3 sync_clockify.py --dry-run

# Delete old script entries and recreate (safe re-run)
python3 sync_clockify.py --from 2026-03-01 --to 2026-03-31 --delete-range

# Skip Google Meet integration
python3 sync_clockify.py --no-meets

# Combine flags
python3 sync_clockify.py --from 2026-03-01 --to 2026-03-31 --delete-range --dry-run
```

### Recommended Workflow

```bash
# 1. Always preview first
python3 sync_clockify.py --from 2026-03-01 --to 2026-03-31 --dry-run

# 2. If it looks good, run for real
python3 sync_clockify.py --from 2026-03-01 --to 2026-03-31

# 3. Need to re-run? Use --delete-range to avoid duplicates
python3 sync_clockify.py --from 2026-03-01 --to 2026-03-31 --delete-range
```

### Command Line Flags

| Flag | Description |
|---|---|
| `--from YYYY-MM-DD` | Start date (default: 1st of current month) |
| `--to YYYY-MM-DD` | End date (default: last day of current month) |
| `--dry-run` | Preview entries without creating them |
| `--delete-range` | Delete existing script-created entries before creating new ones |
| `--no-meets` | Skip Google Meet / Gemini notes integration |
| `--no-slack` | Skip Slack DM + huddle integration |

---

## How It Works

### Time Duration Calculation

- **Single commit day**: 3 hours (default)
- **Multiple commits**: time span between first and last commit + 1.25 hour buffer
- **Minimum**: 0.5 hours per entry
- **Maximum**: 8 hours per entry

### Hour Spreading

When a day has a heavy commit load (> 5 hours) and preceding weekdays have no commits for that repo, the script redistributes hours backward. This accounts for work that was done locally before committing.

Example:
- Monday: 0 commits → gets ~3h (spread from Tuesday)
- Tuesday: 10 commits, 7h span → reduced to ~3.5h

### Description Generation

With OpenAI enabled, the script sends commit messages to GPT-4o-mini to generate:
- **What was done**: plain English summary of the work
- **Why it was done**: inferred purpose (bug fix, feature, maintenance, etc.)

Descriptions are strictly kept under 2500 characters (Clockify limit).

### Safe Re-runs with --delete-range

The `--delete-range` flag identifies entries created by this script by checking:
1. Description starts with `[` (our format: `[repo-name]` or `[Meeting]`)
2. Entry belongs to a mapped Clockify project

Your manually-created Clockify entries are **never touched**.

---

## File Structure

```
clockify_filler/
├── .env                     # Your secrets (git-ignored)
├── .env.example             # Template for .env
├── .gitignore               # Blocks secrets from git
├── client_secret_*.json     # Google OAuth credentials (git-ignored)
├── google_token.json        # Cached Google auth token (git-ignored)
├── sync_clockify.py         # Main script (safe to commit)
├── slack_fetch_test.py      # Read-only Slack inspector (safe to commit)
└── README.md                # This file
```

---

## Troubleshooting

### "CLOCKIFY_API_KEY not set"
Make sure `.env` exists in the same directory as `sync_clockify.py` and contains `CLOCKIFY_API_KEY=...`

### "GITHUB_TOKEN not set — only public repos will be searched"
Add `GITHUB_TOKEN=ghp_...` to your `.env` file. Without it, private/org repos won't be found.

### "Google Meet fetch failed" or OAuth errors
1. Ensure all 3 Google APIs are enabled (Calendar, Drive, Docs)
2. Delete `google_token.json` and re-run to re-authenticate
3. Make sure your email is listed as a test user in the OAuth consent screen

### "Error 400: invalid_scope"
One or more Google APIs aren't enabled. Go to Google Cloud Console > APIs & Services > Library and enable Calendar API, Drive API, and Docs API.

### Meetings missing from results
Only events with Google Meet / video conferencing links are included. Regular calendar events without a Meet link are skipped.

### Commits missing from results
The script scans all branches of mapped repos. If a repo isn't in `REPO_PROJECT_MAP`, its commits are skipped (logged as "unmapped"). Add the repo name to the mapping in `sync_clockify.py`.

If the commit's git author name/email isn't your GitHub login (check the commit on GitHub: no avatar/login next to the author), add that name or email to `GITHUB_AUTHOR_ALIASES`.

### Slack: "SLACK_USER_TOKEN not set" / Slack step is skipped
Add `SLACK_USER_TOKEN=xoxp-...` to your `.env`. See **Slack Setup** above.

### Slack: "missing_scope" or "not_authed" error
You're missing one of the required User Token Scopes (usually `users:read.email`). Go back to **OAuth & Permissions**, add the scope, **Reinstall to Workspace**, and copy the new token into `.env`.

### Slack: entries rejected with `Don't use "<" and ">" characters`
Clockify rejects angle brackets in descriptions. The script auto-sanitises them to parentheses — if you still see this, pull the latest `sync_clockify.py`.

### Slack DM counterpart skipped with "No Clockify project"
The counterpart's email domain isn't in `SLACK_EMAIL_DOMAIN_PROJECT_MAP` *and* there's no `"Slack - Unassigned"` project in your workspace. Either add the domain to the map or create `"Slack - Unassigned"` in Clockify.

### Duplicate entries after re-running
Use `--delete-range` to clean up entries from a previous run before creating new ones:
```bash
python3 sync_clockify.py --from 2026-03-01 --to 2026-03-31 --delete-range
```
