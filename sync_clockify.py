#!/usr/bin/env python3
"""
sync_clockify.py — Fetch GitHub commit activity and log it as Clockify time entries.

USAGE:
    # Install dependencies
    pip install requests google-api-python-client google-auth-httplib2 google-auth-oauthlib

    # Run for current month
    python sync_clockify.py

    # Run for a specific date range
    python sync_clockify.py --from 2026-03-01 --to 2026-03-31

    # Preview what would be created
    python sync_clockify.py --dry-run

    # Re-run (delete old entries first)
    python sync_clockify.py --from 2026-03-01 --to 2026-03-31 --delete-range

    # Skip Google Meet integration
    python sync_clockify.py --no-meets

SETUP:
    Create a .env file in the same directory as this script:

        GITHUB_TOKEN=ghp_xxxx
        CLOCKIFY_API_KEY=xxxx
        OPENAI_API_KEY=sk-xxxx
        GITHUB_USERNAME=your-username

    Place your Google OAuth client_secret_*.json in the same directory.
"""

import argparse
import os
import sys
import time as _time
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:
    sys.exit(
        "[ERROR] 'requests' is not installed.\n"
        "        Run:  pip install requests"
    )

# Load .env file from the same directory as the script
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_FILE = os.path.join(_SCRIPT_DIR, ".env")
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ.setdefault(_key.strip(), _val.strip())

# All date grouping and time entries use Melbourne time
MELB_TZ = ZoneInfo("Australia/Melbourne")

# ============================================================================
# Configuration
# ============================================================================

GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "sy-Kazmi")
# Comma-separated list of GitHub orgs/users to search for known repos in.
# For each repo in REPO_PROJECT_MAP, the walker tries each org until it finds one.
GITHUB_ORGS = [
    o.strip() for o in os.environ.get(
        "GITHUB_ORGS", "superist-group,Lisnic-com,appscore,smein-org"
    ).split(",") if o.strip()
]

GITHUB_API = "https://api.github.com"
CLOCKIFY_API = "https://api.clockify.me/api/v1"
OPENAI_API = "https://api.openai.com/v1/chat/completions"

# Google OAuth — looks for client_secret_*.json in the script directory
GOOGLE_CREDS_FILE = next(
    (os.path.join(_SCRIPT_DIR, f) for f in os.listdir(_SCRIPT_DIR) if f.startswith("client_secret_") and f.endswith(".json")),
    os.path.join(_SCRIPT_DIR, "credentials.json"),
)
GOOGLE_TOKEN_FILE = os.path.join(_SCRIPT_DIR, "google_token.json")
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
]

# Repo name (case-insensitive) -> Clockify project name (must match exactly)
REPO_PROJECT_MAP: dict[str, str] = {
    "bobbi-web-portal":             "BOBBI",
    "bobbi-portal-api":             "BOBBI",
    "bobbi-portal":                 "BOBBI",
    "bobbi-lp":                     "BOBBI",
    "outsourcey-web":               "BOBBI",
    "nexseo":                       "First Page AU",
    "mia":                          "First Page AU",
    "fpau-staging":                 "First Page AU",
    "fpnz-staging":                 "First Page NZ",
    "gamdom":                       "Gamdom",
    "i18n":                         "Gamdom",
    "gamdon-reporting":             "Gamdom",
    "lisnic":                       "Lisnic",
    "lisnic-frontend":              "Lisnic",
    "nicks":                        "Nick's Projects",
    "outsourcey-client-portal-v2":  "Outsourcey",
    "sentr-crm":                    "SENTR 2.0",
    "superyoung-web":               "Super Young",
}

# Meeting title keywords -> Clockify project (checked in order, first match wins)
MEETING_KEYWORD_MAP: list[tuple[list[str], str]] = [
    (["bobbi"],                              "BOBBI"),
    (["nexseo", "first page au", "fpau"],    "First Page AU"),
    (["first page nz", "fpnz"],             "First Page NZ"),
    (["gamdom", "gamedom"],                  "Gamdom"),
    (["lisnic"],                             "Lisnic"),
    (["nick"],                               "Nick's Projects"),
    (["outsourcey"],                          "Outsourcey"),
    (["sentr"],                              "SENTR 2.0"),
    (["super young", "superyoung"],          "Super Young"),
]
MEETING_FALLBACK_PROJECT = "Google Meet - Unassigned"
GIT_FALLBACK_PROJECT = "Git Project - Unassigned"

# Meetings to skip entirely (not work — checked case-insensitive)
MEETING_SKIP_KEYWORDS: list[str] = [
    "hats off friday",
    "birthday leave",
    "ooo",
    "out of office",
    " al ",     # annual leave (with spaces to avoid false matches like "portal")
    " bl ",     # birthday leave
    " pl ",     # personal leave
    " sl ",     # sick leave
    "annual leave",
    "personal leave",
    "sick leave",
    "bereavement leave",
    "parental leave",
    "leave request",
    "on leave",
    "day off",
]

MAX_DESC_LEN = 2499          # Clockify hard limit is 2500; stay under
DEFAULT_DURATION_HOURS = 3   # fallback for single-commit days
MIN_DURATION_HOURS = 0.5     # minimum per entry
MAX_DURATION_HOURS = 8

# Hour spreading: redistribute heavy days to preceding empty weekdays
SPREAD_THRESHOLD_H = 5.0    # only spread days with more than this
SPREAD_LOOKBACK_DAYS = 3    # max gap days to look back
SPREAD_KEEP_MIN_H = 3.0     # keep at least this many hours on the heavy day

# ── Slack ─────────────────────────────────────────────────────────────────────
SLACK_API = "https://slack.com/api"

# Email domain (lowercase, no @) -> Clockify project name
SLACK_EMAIL_DOMAIN_PROJECT_MAP: dict[str, str] = {
    "bobbi.com.au":       "BOBBI",
    "firstpage.com.au":   "First Page AU",
    "teamgamdom.com":     "Gamdom",
    "firstpage.nz":       "First Page NZ",
    "outsourcey.com":     "Outsourcey",
    "superistgroup.com":  "Superist Group",
}
SLACK_FALLBACK_PROJECT = "Slack - Unassigned"

# Conversation types to scan: im = 1:1 DMs, mpim = group DMs, plus channels
SLACK_CONVERSATION_TYPES = "im,mpim,public_channel,private_channel"
# Max members resolved when picking a representative for a channel/group with
# no usable senders in the window (channels can have hundreds of members)
SLACK_MEMBER_RESOLVE_CAP = 30
# Scan history this far back before the sync window so thread parents started
# earlier are still seen — their replies inside the window are otherwise
# invisible (conversations.history never returns thread replies).
SLACK_THREAD_LOOKBACK_DAYS = 180

# Bounds for OpenAI-estimated conversation hours per (email, day)
SLACK_CONV_MIN_HOURS = 0.25
SLACK_CONV_MAX_HOURS = 3.0
# Upper bound on messages fed to OpenAI for one (email, day) group
SLACK_MAX_MSGS_FOR_PROMPT = 80

# ============================================================================
# GitHub helpers
# ============================================================================

def _gh_headers(token: str | None) -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _gh_get(url: str, params: dict, token: str | None) -> requests.Response:
    """GET with automatic retry on 403 (rate-limit)."""
    for attempt in range(3):
        resp = requests.get(url, headers=_gh_headers(token), params=params)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            wait = int(resp.headers.get("Retry-After", 60))
            print(f"    [!] GitHub rate-limit hit. Waiting {wait}s (attempt {attempt+1}/3)...")
            _time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp  # unreachable, but keeps linters happy


def fetch_commits_search_api(username: str, date_from: str, date_to: str, token: str | None) -> list[dict]:
    """Primary: GitHub Search Commits API — works across all repos the token can see."""
    query = f"author:{username} author-date:{date_from}..{date_to}"
    url = f"{GITHUB_API}/search/commits"
    commits: list[dict] = []
    page = 1

    while True:
        params = {"q": query, "sort": "author-date", "order": "asc", "per_page": 100, "page": page}
        resp = _gh_get(url, params, token)
        data = resp.json()
        items = data.get("items", [])

        for item in items:
            repo_name = item.get("repository", {}).get("name", "unknown")
            repo_full = item.get("repository", {}).get("full_name", "unknown")
            message = item.get("commit", {}).get("message", "")
            author_date = item.get("commit", {}).get("author", {}).get("date", "")
            sha = item.get("sha", "")[:7]
            commits.append({
                "repo": repo_name,
                "repo_full": repo_full,
                "message": message.split("\n")[0].strip(),  # first line only
                "date": author_date,
                "sha": sha,
            })

        total = data.get("total_count", 0)
        fetched = page * 100
        if not items or fetched >= total or fetched >= 1000:
            break
        page += 1
        _time.sleep(1)  # stay under search rate-limit

    return commits


def fetch_commits_events_api(username: str, date_from: str, date_to: str, token: str | None) -> list[dict]:
    """Supplement: GitHub Events API — catches recent pushes (up to 90 days / 300 events)."""
    url = f"{GITHUB_API}/users/{username}/events"
    dt_from = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
    dt_to = datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    commits: list[dict] = []
    page = 1

    while page <= 10:  # events API max 10 pages
        print(f"      Events API page {page}/10...", end="", flush=True)
        params = {"per_page": 100, "page": page}
        try:
            resp = _gh_get(url, params, token)
        except requests.HTTPError:
            print(" error")
            break
        events = resp.json()
        if not events:
            print(" empty")
            break

        for ev in events:
            if ev.get("type") != "PushEvent":
                continue
            repo_full = ev.get("repo", {}).get("name", "")
            repo_name = repo_full.split("/")[-1] if "/" in repo_full else repo_full

            for c in ev.get("payload", {}).get("commits", []):
                # Only include commits authored by this user
                author_email = c.get("author", {}).get("email", "")
                author_name = c.get("author", {}).get("name", "")
                if username.lower() not in author_name.lower() and username.lower() not in author_email.lower():
                    continue

                # Use event created_at as approximate commit time
                event_date = ev.get("created_at", "")
                try:
                    dt = datetime.fromisoformat(event_date.replace("Z", "+00:00"))
                except ValueError:
                    continue

                if dt < dt_from or dt > dt_to:
                    continue

                sha = c.get("sha", "")[:7]
                commits.append({
                    "repo": repo_name,
                    "repo_full": repo_full,
                    "message": c.get("message", "").split("\n")[0].strip(),
                    "date": event_date,
                    "sha": sha,
                })

        push_count = sum(1 for ev in events if ev.get("type") == "PushEvent")
        print(f" {len(events)} events ({push_count} pushes)")
        page += 1
        _time.sleep(0.5)

    return commits



def _resolve_repo_owner(repo_name: str, orgs: list[str], token: str | None) -> str | None:
    """Find which org owns `repo_name` by probing each. Returns the org name or None."""
    for org in orgs:
        full = f"{org}/{repo_name}"
        try:
            resp = requests.get(
                f"{GITHUB_API}/repos/{full}",
                headers=_gh_headers(token),
                timeout=15,
            )
            if resp.status_code == 200:
                return org
            if resp.status_code in (301, 302):  # moved — follow
                new_full = resp.json().get("full_name")
                if new_full and "/" in new_full:
                    return new_full.split("/", 1)[0]
        except requests.RequestException:
            continue
    return None


def fetch_commits_branches_api(
    repo_names: list[str], orgs: list[str], username: str,
    date_from: str, date_to: str, token: str | None,
) -> list[dict]:
    """
    Walk every branch of every known repo, collect commits authored by `username`
    in the date range. Catches commits that live only on feature branches (which
    the Search Commits API misses because it indexes the default branch).

    `repo_names` are bare repo names. For each, the walker tries each of `orgs`
    until one returns a 200 on /repos/{org}/{repo}.
    """
    since = f"{date_from}T00:00:00Z"
    until = f"{date_to}T23:59:59Z"

    # Dedupe within this function by full SHA
    by_sha: dict[str, dict] = {}

    for repo_name in repo_names:
        owner = _resolve_repo_owner(repo_name, orgs, token)
        if not owner:
            print(f"      {repo_name}: not found in {orgs}")
            continue
        full = f"{owner}/{repo_name}"

        # 1) List branches
        branches: list[dict] = []
        page = 1
        while page <= 5:
            try:
                br_resp = _gh_get(f"{GITHUB_API}/repos/{full}/branches", {"per_page": 100, "page": page}, token)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code in (404, 403):
                    break
                print(f"    [WARN] {full}: list branches failed: {e}")
                break
            batch = br_resp.json()
            if not isinstance(batch, list) or not batch:
                break
            branches.extend(batch)
            if len(batch) < 100:
                break
            page += 1

        if not branches:
            continue

        print(f"      {full}: {len(branches)} branches")

        # 2) For each branch, fetch commits by author in range
        for br in branches:
            br_name = br.get("name", "")
            if not br_name:
                continue
            p = 1
            while p <= 5:
                params = {
                    "author": username,
                    "since": since,
                    "until": until,
                    "sha": br_name,
                    "per_page": 100,
                    "page": p,
                }
                try:
                    resp = _gh_get(f"{GITHUB_API}/repos/{full}/commits", params, token)
                except requests.HTTPError as e:
                    # 409 = empty repo; stop scanning this branch
                    break
                items = resp.json()
                if not isinstance(items, list) or not items:
                    break
                for item in items:
                    full_sha = item.get("sha", "")
                    if not full_sha or full_sha in by_sha:
                        continue
                    commit = item.get("commit", {})
                    by_sha[full_sha] = {
                        "repo": repo_name,
                        "repo_full": full,
                        "message": commit.get("message", "").split("\n")[0].strip(),
                        "date": commit.get("author", {}).get("date", ""),
                        "sha": full_sha[:7],
                    }
                if len(items) < 100:
                    break
                p += 1
                _time.sleep(0.15)
            _time.sleep(0.05)

    return list(by_sha.values())


def fetch_all_commits(username: str, date_from: str, date_to: str, token: str | None) -> list[dict]:
    """Merge Search API + Events API + per-branch walker; dedupe by SHA."""
    print("    Querying Search Commits API...")
    search_commits = fetch_commits_search_api(username, date_from, date_to, token)
    print(f"    -> {len(search_commits)} commits from Search API")

    print("    Querying Events API (supplement)...")
    events_commits = fetch_commits_events_api(username, date_from, date_to, token)
    print(f"    -> {len(events_commits)} commits from Events API")

    print(f"    Walking branches of known repos across orgs {GITHUB_ORGS}...")
    known_repos = sorted(set(REPO_PROJECT_MAP.keys()))
    branch_commits = fetch_commits_branches_api(
        known_repos, GITHUB_ORGS, username, date_from, date_to, token,
    )
    print(f"    -> {len(branch_commits)} commits from branch walker")

    # Deduplicate by truncated SHA — all three sources use the same 7-char form
    seen: set[str] = set()
    merged: list[dict] = []
    for c in search_commits + events_commits + branch_commits:
        if c["sha"] not in seen:
            seen.add(c["sha"])
            merged.append(c)

    return merged


# ============================================================================
# Clockify helpers
# ============================================================================

def _cfy_headers(api_key: str) -> dict:
    return {"X-Api-Key": api_key, "Content-Type": "application/json"}


def get_workspace_id(api_key: str) -> str:
    resp = requests.get(f"{CLOCKIFY_API}/workspaces", headers=_cfy_headers(api_key))
    resp.raise_for_status()
    workspaces = resp.json()
    if not workspaces:
        sys.exit("[ERROR] No Clockify workspaces found for this API key.")
    ws = workspaces[0]
    print(f"    Workspace: {ws['name']}  (ID: {ws['id']})")
    return ws["id"]


def get_projects(api_key: str, workspace_id: str) -> dict[str, str]:
    """Return {project_name: project_id} for all projects in the workspace."""
    projects: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            f"{CLOCKIFY_API}/workspaces/{workspace_id}/projects",
            headers=_cfy_headers(api_key),
            params={"page": page, "page-size": 200, "archived": "false"},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        projects.extend(batch)
        if len(batch) < 200:
            break
        page += 1

    return {p["name"]: p["id"] for p in projects}


def _sanitize_desc(desc: str) -> str:
    """Clockify rejects '<' and '>' in descriptions (error code 501)."""
    return desc.replace("<", "(").replace(">", ")")


def create_time_entry(
    api_key: str,
    workspace_id: str,
    project_id: str,
    description: str,
    start_utc: datetime,
    end_utc: datetime,
) -> dict:
    body = {
        "start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "description": _sanitize_desc(description),
        "projectId": project_id,
        "billable": True,
    }
    resp = requests.post(
        f"{CLOCKIFY_API}/workspaces/{workspace_id}/time-entries",
        headers=_cfy_headers(api_key),
        json=body,
    )
    resp.raise_for_status()
    return resp.json()


def get_user_id(api_key: str) -> str:
    resp = requests.get(f"{CLOCKIFY_API}/user", headers=_cfy_headers(api_key))
    resp.raise_for_status()
    return resp.json()["id"]


def delete_entries_in_range(
    api_key: str, workspace_id: str, user_id: str,
    date_from: str, date_to: str, project_ids: set[str],
    dry_run: bool = False,
) -> int:
    """
    Delete time entries created by this script within the date range.
    Identifies script entries by checking if the description starts with '[' (our format).
    Only deletes entries whose projectId matches one of the mapped Clockify projects.
    """
    start = f"{date_from}T00:00:00Z"
    end = f"{date_to}T23:59:59Z"
    deleted = 0
    page = 1

    while True:
        resp = requests.get(
            f"{CLOCKIFY_API}/workspaces/{workspace_id}/user/{user_id}/time-entries",
            headers=_cfy_headers(api_key),
            params={"start": start, "end": end, "page-size": 200, "page": page},
        )
        resp.raise_for_status()
        entries = resp.json()
        if not entries:
            break

        for e in entries:
            desc = e.get("description", "")
            proj_id = e.get("projectId", "")
            # Only delete entries that look like ours AND belong to a mapped project
            if not desc.startswith("["):
                continue
            if proj_id not in project_ids:
                continue

            entry_start = e["timeInterval"]["start"][:10]
            if dry_run:
                print(f"    [DRY DEL] {entry_start}  {desc[:70]}...")
            else:
                r = requests.delete(
                    f"{CLOCKIFY_API}/workspaces/{workspace_id}/time-entries/{e['id']}",
                    headers=_cfy_headers(api_key),
                )
                if r.status_code == 204:
                    print(f"    [DEL] {entry_start}  {desc[:70]}...")
                    deleted += 1
                else:
                    print(f"    [ERR] Failed to delete {e['id']}: {r.status_code}")
                _time.sleep(0.2)

        if len(entries) < 200:
            break
        page += 1

    return deleted


# ============================================================================
# Google Meet + Gemini Notes
# ============================================================================

def _get_google_creds():
    """Authenticate with Google OAuth (opens browser on first run, caches token)."""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request as GRequest

    creds = None
    if os.path.exists(GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, GOOGLE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GRequest())
        else:
            if not os.path.exists(GOOGLE_CREDS_FILE):
                print(f"    [WARN] Google credentials not found at {GOOGLE_CREDS_FILE}")
                return None
            flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDS_FILE, GOOGLE_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GOOGLE_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def fetch_google_meet_events(date_from: str, date_to: str) -> list[dict]:
    """Fetch Google Calendar events that are Google Meet meetings."""
    creds = _get_google_creds()
    if not creds:
        return []

    from googleapiclient.discovery import build
    service = build("calendar", "v3", credentials=creds)

    time_min = f"{date_from}T00:00:00Z"
    time_max = f"{date_to}T23:59:59Z"

    meetings: list[dict] = []
    page_token = None

    while True:
        result = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
            pageToken=page_token,
        ).execute()

        for ev in result.get("items", []):
            # Only include events with Google Meet / video conferencing
            has_meet = bool(ev.get("hangoutLink") or ev.get("conferenceData"))
            if not has_meet:
                continue

            summary = ev.get("summary", "Untitled Meeting")
            start_raw = ev.get("start", {})
            end_raw = ev.get("end", {})

            # Parse start/end (could be dateTime or date)
            start_str = start_raw.get("dateTime", start_raw.get("date", ""))
            end_str = end_raw.get("dateTime", end_raw.get("date", ""))

            try:
                start_dt = datetime.fromisoformat(start_str)
                end_dt = datetime.fromisoformat(end_str)
            except ValueError:
                continue

            duration_h = (end_dt - start_dt).total_seconds() / 3600
            if duration_h <= 0 or duration_h > 12:
                continue

            # Melbourne day for grouping
            melb_day = start_dt.astimezone(MELB_TZ).strftime("%Y-%m-%d")

            meetings.append({
                "summary": summary,
                "start": start_dt,
                "end": end_dt,
                "duration_h": round(duration_h, 2),
                "day": melb_day,
                "event_id": ev.get("id", ""),
                "attendees": [a.get("email", "") for a in ev.get("attendees", [])],
            })

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return meetings


def _search_gemini_notes(creds, meeting_summary: str, meeting_day: str) -> str | None:
    """Search Google Drive for Gemini meeting notes matching this meeting."""
    from googleapiclient.discovery import build

    drive = build("drive", "v3", credentials=creds)

    # Gemini notes are typically named "Meeting notes - <title>" or contain the meeting title
    queries = [
        f"name contains 'Meeting notes' and name contains '{meeting_summary[:40]}' and mimeType = 'application/vnd.google-apps.document'",
        f"name contains '{meeting_summary[:40]}' and name contains 'notes' and mimeType = 'application/vnd.google-apps.document'",
    ]

    for q in queries:
        try:
            results = drive.files().list(
                q=q,
                spaces="drive",
                fields="files(id, name, createdTime)",
                orderBy="createdTime desc",
                pageSize=5,
            ).execute()

            for f in results.get("files", []):
                created = f.get("createdTime", "")[:10]
                # Match if created within 1 day of the meeting
                if created >= meeting_day and created <= (datetime.fromisoformat(meeting_day) + timedelta(days=1)).strftime("%Y-%m-%d"):
                    # Read the doc content
                    docs = build("docs", "v1", credentials=creds)
                    doc = docs.documents().get(documentId=f["id"]).execute()
                    text = ""
                    for element in doc.get("body", {}).get("content", []):
                        for para in element.get("paragraph", {}).get("elements", []):
                            text += para.get("textRun", {}).get("content", "")
                    if text.strip():
                        return text.strip()
        except Exception:
            continue

    return None


def should_skip_meeting(summary: str) -> bool:
    """Return True if this meeting should be excluded (leave, social, etc.)."""
    title_lower = f" {summary.lower()} "  # pad with spaces so " al " matches at edges
    return any(kw in title_lower for kw in MEETING_SKIP_KEYWORDS)


def match_meeting_to_project(summary: str) -> str:
    """Match a meeting title to a Clockify project name using keyword mapping."""
    title_lower = summary.lower()
    for keywords, project_name in MEETING_KEYWORD_MAP:
        if any(kw in title_lower for kw in keywords):
            return project_name
    return MEETING_FALLBACK_PROJECT


def build_meeting_description(
    meeting: dict, notes_text: str | None, openai_key: str | None,
) -> str:
    """Build a Clockify description for a Google Meet entry."""
    summary = meeting["summary"]
    day = meeting["day"]
    duration = meeting["duration_h"]
    attendees = ", ".join(meeting["attendees"][:10]) if meeting["attendees"] else "N/A"

    if openai_key and notes_text:
        try:
            prompt = f"""You are writing a concise meeting summary for a developer's Clockify timesheet.

Meeting: {summary}
Date: {day}
Duration: {duration:.1f} hours
Attendees: {attendees}

Gemini Meeting Notes:
{notes_text[:3000]}

Write a professional, concise time-entry description with:
1. **Meeting** — title and who attended (brief)
2. **Key Discussion Points** — bullet points of what was discussed
3. **Action Items / Outcomes** — what was decided or needs to be done

Rules:
- Start with [Meeting] {summary} - {day}
- Keep strictly under 2400 characters
- Be specific but concise
- Write in third person"""

            resp = requests.post(
                OPENAI_API,
                headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 800, "temperature": 0.3},
                timeout=30,
            )
            resp.raise_for_status()
            desc = resp.json()["choices"][0]["message"]["content"].strip()
            if desc and len(desc) < MAX_DESC_LEN:
                return desc
        except Exception as e:
            print(f"    [WARN] OpenAI failed for meeting desc: {e}")

    # Fallback
    header = f"[Meeting] {summary} - {day}\n\nDuration: {duration:.1f}h\nAttendees: {attendees}\n"
    if notes_text:
        available = MAX_DESC_LEN - len(header) - 30
        notes_trimmed = notes_text[:available]
        return header + "\nNotes:\n" + notes_trimmed
    return header + "\nNo Gemini notes found for this meeting."


# ============================================================================
# Slack
# ============================================================================

def _slack_call(token: str, endpoint: str, params: dict | None = None) -> dict:
    """GET a Slack Web API method with rate-limit backoff; raises on ok=false."""
    params = dict(params or {})
    last_exc: Exception | None = None
    for _attempt in range(4):
        resp = requests.get(
            f"{SLACK_API}/{endpoint}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2"))
            print(f"    [slack rate-limit] sleeping {wait}s on {endpoint}")
            _time.sleep(wait)
            continue
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            last_exc = e
            _time.sleep(1)
            continue
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack API {endpoint} failed: {data.get('error')}")
        return data
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Slack API {endpoint} exhausted retries")


def _slack_list_conversations(token: str) -> list[dict]:
    """List all DM + group-DM conversations the authed user is part of."""
    convs: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict = {"types": SLACK_CONVERSATION_TYPES, "limit": 200, "exclude_archived": True}
        if cursor:
            params["cursor"] = cursor
        data = _slack_call(token, "users.conversations", params)
        convs.extend(data.get("channels", []))
        cursor = data.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            break
        _time.sleep(0.3)
    return convs


def _slack_conv_members(token: str, channel_id: str) -> list[str]:
    """List member user IDs of a conversation (used for group DMs)."""
    members: list[str] = []
    cursor: str | None = None
    while True:
        params: dict = {"channel": channel_id, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            data = _slack_call(token, "conversations.members", params)
        except (requests.HTTPError, RuntimeError) as e:
            print(f"    [WARN] slack members for {channel_id}: {e}")
            break
        members.extend(data.get("members", []))
        cursor = data.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            break
        _time.sleep(0.3)
    return members


def _slack_fetch_replies(
    token: str, channel_id: str, thread_ts: str, oldest_ts: str, latest_ts: str,
) -> list[dict]:
    """Fetch replies of one thread, filtered to [oldest_ts, latest_ts]."""
    replies: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict = {
            "channel": channel_id,
            "ts": thread_ts,
            "oldest": oldest_ts,
            "latest": latest_ts,
            "limit": 200,
            "inclusive": True,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            data = _slack_call(token, "conversations.replies", params)
        except (requests.HTTPError, RuntimeError) as e:
            print(f"    [WARN] slack replies for {channel_id}/{thread_ts}: {e}")
            break
        for r in data.get("messages", []):
            if r.get("ts") != thread_ts:  # parent comes back too; skip it
                replies.append(r)
        if not data.get("has_more"):
            break
        cursor = data.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            break
        _time.sleep(0.4)
    return replies


def _slack_fetch_history(
    token: str, channel_id: str, oldest_ts: str, latest_ts: str,
    thread_scan_oldest_ts: str | None = None,
) -> list[dict]:
    """
    Fetch messages in [oldest_ts, latest_ts], including thread replies.

    conversations.history never returns thread replies, so the channel is
    scanned from thread_scan_oldest_ts (defaults to oldest_ts) to also see
    thread parents started before the window; in-window replies of those
    threads come from conversations.replies. Top-level messages outside
    [oldest_ts, latest_ts] are dropped, and everything is deduped by ts.
    """
    scan_oldest = thread_scan_oldest_ts or oldest_ts
    raw: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict = {
            "channel": channel_id,
            "oldest": scan_oldest,
            "latest": latest_ts,
            "limit": 200,
            "inclusive": True,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            data = _slack_call(token, "conversations.history", params)
        except (requests.HTTPError, RuntimeError) as e:
            print(f"    [WARN] slack history for {channel_id}: {e}")
            break
        raw.extend(data.get("messages", []))
        if not data.get("has_more"):
            break
        cursor = data.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            break
        _time.sleep(0.4)

    oldest_f, latest_f = float(oldest_ts), float(latest_ts)
    msgs: list[dict] = []
    seen_ts: set[str] = set()

    def _add(m: dict) -> None:
        ts_raw = m.get("ts", "")
        if not ts_raw or ts_raw in seen_ts:
            return
        try:
            ts = float(ts_raw)
        except ValueError:
            return
        if ts < oldest_f or ts > latest_f:
            return
        seen_ts.add(ts_raw)
        msgs.append(m)

    for m in raw:
        _add(m)
        is_thread_parent = m.get("reply_count") and m.get("thread_ts") == m.get("ts")
        if is_thread_parent and float(m.get("latest_reply") or 0) >= oldest_f:
            for r in _slack_fetch_replies(token, channel_id, m["ts"], oldest_ts, latest_ts):
                _add(r)

    msgs.sort(key=lambda m: float(m.get("ts", "0")))
    return msgs


def _slack_resolve_user(token: str, user_id: str, cache: dict[str, dict]) -> dict:
    if not user_id:
        return {"id": "", "name": "system", "email": ""}
    if user_id in cache:
        return cache[user_id]
    try:
        data = _slack_call(token, "users.info", {"user": user_id})
        u = data.get("user", {})
        info = {
            "id": user_id,
            "name": u.get("real_name") or u.get("name") or user_id,
            "email": (u.get("profile", {}).get("email") or "").lower(),
            "is_bot": u.get("is_bot", False),
        }
    except Exception:
        info = {"id": user_id, "name": user_id, "email": "", "is_bot": False}
    cache[user_id] = info
    _time.sleep(0.15)
    return info


def match_email_to_project(email: str) -> str | None:
    """Return project name for an email's domain, or None if no match."""
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].lower()
    return SLACK_EMAIL_DOMAIN_PROJECT_MAP.get(domain)


def fetch_slack_activity(
    token: str, date_from: str, date_to: str,
) -> tuple[dict[tuple[str, str], list[dict]], list[dict], dict[str, dict]]:
    """
    Returns (dm_groups, huddles, user_cache).

    dm_groups: {(email, YYYY-MM-DD in Melbourne): [messages]} — 1:1 DMs, group
               DMs, and channels (thread replies included), messages filtered
               to real content (no system/huddle markers). For group DMs and
               channels, email is a representative participant's — preferring
               one whose domain maps to a Clockify project.
    huddles:   list of {"email", "start_utc", "end_utc", "duration_s", "participants", ...}
    """
    oldest_dt = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=MELB_TZ)
    latest_dt = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=MELB_TZ)
    oldest_ts = f"{oldest_dt.timestamp():.6f}"
    latest_ts = f"{latest_dt.timestamp():.6f}"
    thread_scan_oldest_ts = f"{(oldest_dt - timedelta(days=SLACK_THREAD_LOOKBACK_DAYS)).timestamp():.6f}"

    user_cache: dict[str, dict] = {}
    me = _slack_call(token, "auth.test")
    my_id = me.get("user_id", "")
    print(f"    Authed as {me.get('user')} ({my_id}) in {me.get('team')}")

    convs = _slack_list_conversations(token)
    print(f"    {len(convs)} conversations (DMs + group DMs + channels)")

    dm_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    huddles: list[dict] = []

    for conv in convs:
        cid = conv["id"]
        is_group_conv = conv.get("is_mpim") or conv.get("is_channel") or conv.get("is_group")

        if not is_group_conv:
            # 1:1 DM — resolve the counterpart before fetching so bots are skipped cheaply
            other_user_id = conv.get("user", "")
            if not other_user_id:
                continue
            if other_user_id == my_id:
                continue  # skip self-DM ("notes to self" channel)
            other = _slack_resolve_user(token, other_user_id, user_cache)
            other_email = other.get("email", "")
            if other.get("is_bot") or not other_email:
                continue  # skip bots / slackbot / users without email
            conv_name = other.get("name", other_email)

        msgs = _slack_fetch_history(token, cid, oldest_ts, latest_ts, thread_scan_oldest_ts)
        if not msgs:
            continue

        if is_group_conv:
            # Group DM or channel: attribute time to a representative counterpart,
            # preferring one whose email domain maps to a Clockify project.
            # Candidates come from message senders in the window; fall back to
            # the member list (capped) when nobody but me spoke.
            sender_ids: list[str] = []
            for m in msgs:
                uid = m.get("user") or ""
                if uid and uid != my_id and uid not in sender_ids:
                    sender_ids.append(uid)
            candidates = [_slack_resolve_user(token, uid, user_cache) for uid in sender_ids]
            candidates = [u for u in candidates if u.get("email") and not u.get("is_bot")]
            if not candidates:
                member_ids = [
                    uid for uid in _slack_conv_members(token, cid)
                    if uid and uid != my_id
                ][:SLACK_MEMBER_RESOLVE_CAP]
                candidates = [_slack_resolve_user(token, uid, user_cache) for uid in member_ids]
                candidates = [u for u in candidates if u.get("email") and not u.get("is_bot")]
            if not candidates:
                continue
            rep = next(
                (u for u in candidates if match_email_to_project(u["email"])),
                candidates[0],
            )
            other_email = rep["email"]
            if conv.get("is_mpim"):
                conv_name = conv.get("name") or rep.get("name") or cid
            else:
                conv_name = f"#{conv.get('name') or cid}"

        for m in msgs:
            ts_raw = m.get("ts", "0")
            try:
                ts = float(ts_raw)
            except ValueError:
                continue
            msg_dt = datetime.fromtimestamp(ts, MELB_TZ)
            day = msg_dt.strftime("%Y-%m-%d")
            subtype = m.get("subtype", "")

            if subtype == "huddle_thread":
                room = m.get("room") or {}
                d_start = int(room.get("date_start") or 0)
                d_end = int(room.get("date_end") or 0)
                if not d_start or not d_end or d_end <= d_start:
                    # Active or missing end — skip for now, will pick up next run
                    continue
                duration_s = d_end - d_start
                start_utc = datetime.fromtimestamp(d_start, tz=timezone.utc)
                end_utc = datetime.fromtimestamp(d_end, tz=timezone.utc)
                parts: list[dict] = []
                for pid in (room.get("participant_history") or room.get("participants") or []):
                    if pid and not pid.startswith("B"):
                        parts.append(_slack_resolve_user(token, pid, user_cache))
                huddles.append({
                    "email": other_email,
                    "day": day,
                    "start_utc": start_utc,
                    "end_utc": end_utc,
                    "duration_s": duration_s,
                    "participants": parts,
                    "counterpart_name": conv_name,
                })
                continue

            if subtype and subtype != "thread_broadcast":
                # Skip other system messages (channel_join, bot_message, etc.)
                continue
            text = (m.get("text") or "").strip()
            if not text:
                continue

            sender_id = m.get("user") or ""
            sender = _slack_resolve_user(token, sender_id, user_cache) if sender_id else {}
            is_me = (sender_id == my_id)

            dm_groups[(other_email, day)].append({
                "ts": ts,
                "time": msg_dt.strftime("%H:%M"),
                "is_me": is_me,
                "sender_name": "me" if is_me else (sender.get("name") or conv_name or "them"),
                "text": text,
            })

        _time.sleep(0.25)

    # Keep only (email, day) groups where the user actually participated
    filtered: dict[tuple[str, str], list[dict]] = {}
    for key, msgs in dm_groups.items():
        if any(m["is_me"] for m in msgs):
            filtered[key] = sorted(msgs, key=lambda m: m["ts"])

    return filtered, huddles, user_cache


def _slack_message_transcript(messages: list[dict], max_msgs: int) -> str:
    """Render DM messages as a compact transcript for the OpenAI prompt."""
    trimmed = messages[-max_msgs:] if len(messages) > max_msgs else messages
    lines = []
    for m in trimmed:
        text = m["text"].replace("\n", " ")
        if len(text) > 220:
            text = text[:220] + "..."
        lines.append(f"[{m['time']}] {m['sender_name']}: {text}")
    header = ""
    if len(messages) > max_msgs:
        header = f"(showing last {max_msgs} of {len(messages)} messages)\n"
    return header + "\n".join(lines)


def build_slack_conversation_entry(
    email: str, day: str, messages: list[dict],
    huddle_minutes_same_day: int, openai_key: str | None,
) -> tuple[float, str]:
    """
    Ask OpenAI to estimate time spent on this DM and draft a description.
    Returns (hours, description). Falls back to a message-count heuristic if OpenAI fails.
    """
    transcript = _slack_message_transcript(messages, SLACK_MAX_MSGS_FOR_PROMPT)
    my_msg_count = sum(1 for m in messages if m["is_me"])
    their_msg_count = sum(1 for m in messages if not m["is_me"])

    if openai_key:
        try:
            prompt = f"""You are estimating active time spent on a Slack DM for a developer's Clockify timesheet.

Counterpart: {email}
Date: {day}
Messages I sent: {my_msg_count}
Messages they sent: {their_msg_count}
Separate huddle/call time already logged that day: {huddle_minutes_same_day} minutes (do NOT count this — estimate text-chat time only).

Transcript:
{transcript}

Tasks:
1) Estimate the ACTIVE time the developer spent reading, thinking, and replying in this DM thread.
   - Ignore long gaps with no activity.
   - Typical rapid back-and-forth = ~1 minute per exchange of turns.
   - Complex technical troubleshooting or code review = more.
   - Must be between {SLACK_CONV_MIN_HOURS} and {SLACK_CONV_MAX_HOURS} hours.
2) Write a concise time-entry description:
   - First line: [Slack] {email} - {day}
   - Then 2-6 bullets summarising what was discussed (in first person).
   - Strictly under 2000 characters.
   - Do not repeat raw message text.

Respond ONLY as JSON: {{"estimated_hours": <float>, "description": "<string>"}}"""

            resp = requests.post(
                OPENAI_API,
                headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 800,
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                },
                timeout=45,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            import json as _json
            parsed = _json.loads(content)
            hours = float(parsed.get("estimated_hours", 0))
            desc = (parsed.get("description") or "").strip()
            hours = max(SLACK_CONV_MIN_HOURS, min(hours, SLACK_CONV_MAX_HOURS))
            if desc and len(desc) < MAX_DESC_LEN:
                return hours, desc
        except Exception as e:
            print(f"    [WARN] OpenAI failed for Slack DM {email} {day}: {e}")

    # Heuristic fallback: ~1 min per message pair, clamped
    total = max(1, my_msg_count + their_msg_count)
    est_h = max(SLACK_CONV_MIN_HOURS, min(total / 60.0, SLACK_CONV_MAX_HOURS))
    lines = "\n".join(f"- {m['sender_name']} {m['time']}: {m['text'][:140]}" for m in messages[:12])
    desc = (
        f"[Slack] {email} - {day}\n\n"
        f"DM exchange ({my_msg_count} sent, {their_msg_count} received). Highlights:\n{lines}"
    )
    return est_h, desc[:MAX_DESC_LEN]


def build_slack_huddle_description(huddle: dict) -> str:
    parts = huddle.get("participants") or []
    dur_min = huddle["duration_s"] // 60
    # Clockify rejects '<' and '>' in descriptions; use plain parentheses
    header = f"[Huddle] {huddle['counterpart_name']} ({huddle['email']}) - {huddle['day']}"
    body = f"\n\nSlack huddle, {dur_min} minutes."
    if parts:
        names = ", ".join(p.get("name", p.get("id", "?")) for p in parts)
        body += f"\nParticipants: {names}"
    return (header + body)[:MAX_DESC_LEN]


def _slack_entry_start_end(messages: list[dict], hours: float) -> tuple[datetime, datetime]:
    """Use the first message's timestamp as start; end = start + estimated hours."""
    first = min(messages, key=lambda m: m["ts"])
    start_utc = datetime.fromtimestamp(first["ts"], tz=timezone.utc)
    end_utc = start_utc + timedelta(hours=hours)
    return start_utc, end_utc


# ============================================================================
# Aggregation & formatting
# ============================================================================

def group_commits(commits: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group commits into buckets of (repo_name, YYYY-MM-DD in Melbourne time)."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in commits:
        dt = _parse_dt(c["date"])
        if dt:
            day = dt.astimezone(MELB_TZ).strftime("%Y-%m-%d")
        else:
            day = c["date"][:10]  # fallback: raw string
        groups[(c["repo"], day)].append(c)
    return dict(groups)


def _parse_dt(iso_str: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def compute_start_end(commits: list[dict], day: str) -> tuple[datetime, datetime, float]:
    """
    Return (start_utc, end_utc, hours) for a group of commits on the same day.
    Uses actual commit timestamps when available; falls back to 09:00 Melbourne time.
    All returned datetimes are UTC (for Clockify API).
    """
    timestamps = sorted(filter(None, (_parse_dt(c["date"]) for c in commits)))

    if len(timestamps) >= 2:
        start = timestamps[0]
        span_h = (timestamps[-1] - timestamps[0]).total_seconds() / 3600
        # Add buffer for planning, code review, testing, and context switching
        hours = max(MIN_DURATION_HOURS, min(span_h + 1.25, MAX_DURATION_HOURS))
    elif len(timestamps) == 1:
        start = timestamps[0]
        hours = DEFAULT_DURATION_HOURS
    else:
        # Fallback: 09:00 Melbourne time on that day, converted to UTC
        start = datetime.strptime(day, "%Y-%m-%d").replace(hour=9, tzinfo=MELB_TZ).astimezone(timezone.utc)
        hours = DEFAULT_DURATION_HOURS

    # Ensure start is in UTC for Clockify
    start = start.astimezone(timezone.utc)
    end = start + timedelta(hours=hours)
    return start, end, hours


def compute_spread_plan(
    groups: dict[tuple[str, str], list[dict]],
) -> tuple[dict[tuple[str, str], float], list[tuple[str, str, float, list[dict]]]]:
    """
    Spread hours from heavy commit days to preceding empty weekdays.

    Returns:
        hour_overrides: {(repo, day): reduced_hours} for heavy days
        spread_entries: [(repo, gap_day, hours, source_commits)] for new gap-day entries
    """
    # Build per-repo: which days have commits, and their raw hours
    repo_days: dict[str, dict[str, float]] = defaultdict(dict)
    for (repo, day), commits in groups.items():
        _, _, hours = compute_start_end(commits, day)
        repo_days[repo][day] = hours

    hour_overrides: dict[tuple[str, str], float] = {}
    spread_entries: list[tuple[str, str, float, list[dict]]] = []

    for repo, day_hours in repo_days.items():
        sorted_days = sorted(day_hours.keys())

        for day_str in sorted_days:
            hours = day_hours[day_str]
            if hours <= SPREAD_THRESHOLD_H:
                continue

            # Find preceding empty weekdays (stop at a day that already has commits)
            day_date = date.fromisoformat(day_str)
            gap_days: list[str] = []

            for lookback in range(1, SPREAD_LOOKBACK_DAYS + 1):
                prev = day_date - timedelta(days=lookback)
                if prev.weekday() >= 5:  # skip Saturday/Sunday
                    continue
                prev_str = prev.isoformat()
                if prev_str in day_hours:  # this repo already has work here
                    break
                gap_days.append(prev_str)

            if not gap_days:
                continue

            # Distribute: split hours across gap days + heavy day
            total = hours
            num_slots = len(gap_days) + 1
            per_slot = total / num_slots
            keep_on_heavy = max(SPREAD_KEEP_MIN_H, per_slot)
            spillover = total - keep_on_heavy
            per_gap = spillover / len(gap_days)

            # Override the heavy day's hours
            hour_overrides[(repo, day_str)] = keep_on_heavy

            # Create spread entries for gap days
            source_commits = groups[(repo, day_str)]
            for gap_day in gap_days:
                spread_entries.append((repo, gap_day, per_gap, source_commits))

    return hour_overrides, spread_entries


def build_spread_description(
    repo: str, gap_day: str, source_commits: list[dict], openai_key: str | None = None,
) -> str:
    """Build a description for a spread (gap-day) entry."""
    commit_summary = "\n".join(f"- {c['message']}" for c in source_commits[:8])

    if openai_key:
        try:
            prompt = f"""You are writing a concise time-entry description for a developer's Clockify timesheet.

Repository: {repo}
Date: {gap_day}
This day had no commits, but the developer was working on changes that were committed on following days.
The upcoming commits were:
{commit_summary}

Write a brief, professional description of the preparatory work done this day (planning, research, local development, code review, testing).

Rules:
- Start with [{repo}] {gap_day}
- Keep strictly under 2400 characters
- Write in first person
- Be specific about likely prep work based on the commit context
- Do not mention that commits were on a different day"""

            resp = requests.post(
                OPENAI_API,
                headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 600, "temperature": 0.3},
                timeout=30,
            )
            resp.raise_for_status()
            desc = resp.json()["choices"][0]["message"]["content"].strip()
            if desc and len(desc) < MAX_DESC_LEN:
                return desc
        except Exception as e:
            print(f"    [WARN] OpenAI failed for spread entry, using fallback: {e}")

    # Fallback
    lines = "\n".join(f"- {c['message']}" for c in source_commits[:5])
    return f"[{repo}] {gap_day}\n\nDevelopment work — planning, research, local development and testing.\n\nRelated changes:\n{lines}"


def build_description(repo: str, day: str, commits: list[dict], openai_key: str | None = None) -> str:
    """
    Build a Clockify entry description using OpenAI GPT.
    Falls back to simple formatting if OpenAI fails.
    Strictly < 2500 characters.
    """
    commit_lines = [f"- {c['sha']}  {c['message']}" for c in commits]
    commit_text = "\n".join(commit_lines)

    if openai_key:
        try:
            desc = _generate_description_openai(repo, day, commit_text, openai_key)
            if desc and len(desc) < MAX_DESC_LEN:
                return desc
            # If too long, truncate
            if desc and len(desc) >= MAX_DESC_LEN:
                return desc[: MAX_DESC_LEN - 20] + "\n\n... [truncated]"
        except Exception as e:
            print(f"    [WARN] OpenAI failed, using fallback: {e}")

    # Fallback: simple formatting
    return _build_description_fallback(repo, day, commits)


def _generate_description_openai(repo: str, day: str, commit_text: str, api_key: str) -> str:
    """Call OpenAI GPT to generate a professional time-entry description."""
    prompt = f"""You are writing a concise time-entry description for a developer's Clockify timesheet.

Repository: {repo}
Date: {day}
Commits:
{commit_text}

Write a clear, professional description with two sections:
1. **What was done** — Summarise the work in plain English (not just repeating commit messages). Group related changes together.
2. **Why it was done** — Infer the purpose/motivation from the commit context (e.g. bug fix, feature delivery, maintenance, client request).

Rules:
- Start with [{repo}] {day}
- Keep the TOTAL response strictly under 2400 characters
- Be specific but concise — this is a timesheet, not a report
- Use bullet points
- Do not include the raw commit hashes or SHAs
- Write in first person ("Fixed...", "Implemented...", "Updated...")"""

    resp = requests.post(
        OPENAI_API,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 800,
            "temperature": 0.3,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _build_description_fallback(repo: str, day: str, commits: list[dict]) -> str:
    """Simple keyword-based fallback if OpenAI is unavailable."""
    header = f"[{repo}] Work on {day}\n\n"
    commit_lines = [f"- {c['sha']}  {c['message']}" for c in commits]
    what_section = "What was done:\n" + "\n".join(commit_lines)

    blob = " ".join(c["message"].lower() for c in commits)
    reasons: list[str] = []
    if any(w in blob for w in ("fix", "bug", "patch", "hotfix", "resolve")):
        reasons.append("Bug fixes and stability improvements")
    if any(w in blob for w in ("feat", "add", "new", "implement", "create", "introduce")):
        reasons.append("New feature development")
    if any(w in blob for w in ("refactor", "clean", "reorganize", "restructure", "simplify")):
        reasons.append("Code refactoring and maintenance")
    if any(w in blob for w in ("test", "spec", "coverage", "jest", "pytest")):
        reasons.append("Testing improvements")
    if any(w in blob for w in ("deploy", "ci", "cd", "pipeline", "build", "docker", "release")):
        reasons.append("CI/CD and deployment work")
    if any(w in blob for w in ("update", "upgrade", "bump", "version", "deps")):
        reasons.append("Dependency and version updates")
    if any(w in blob for w in ("style", "css", "ui", "design", "layout")):
        reasons.append("UI/styling improvements")
    if not reasons:
        reasons.append("General development work")

    why_section = "\nWhy:\n" + "\n".join(f"- {r}" for r in reasons)
    desc = header + what_section + why_section

    if len(desc) <= MAX_DESC_LEN:
        return desc
    return desc[: MAX_DESC_LEN - 20] + "\n\n... [truncated]"


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync GitHub commits to Clockify time entries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--from", dest="date_from", type=str, default=None,
        help="Start date YYYY-MM-DD (default: 1st of current month)",
    )
    parser.add_argument(
        "--to", dest="date_to", type=str, default=None,
        help="End date YYYY-MM-DD (default: last day of current month)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview entries without creating them in Clockify",
    )
    parser.add_argument(
        "--delete-range", action="store_true",
        help="Delete existing script-created entries in the date range before creating new ones",
    )
    parser.add_argument(
        "--no-meets", action="store_true",
        help="Skip Google Meet / Gemini notes integration",
    )
    parser.add_argument(
        "--no-slack", action="store_true",
        help="Skip Slack DM + huddle integration",
    )
    args = parser.parse_args()

    # -- Resolve date range ---------------------------------------------------
    today = date.today()
    if args.date_from:
        date_from = args.date_from
    else:
        date_from = today.replace(day=1).isoformat()

    if args.date_to:
        date_to = args.date_to
    else:
        next_month = today.replace(day=28) + timedelta(days=4)
        date_to = (next_month - timedelta(days=next_month.day)).isoformat()

    print(f"\n{'='*60}")
    print(f"  GitHub -> Clockify Sync")
    print(f"  Range : {date_from}  to  {date_to}")
    print(f"  TZ    : Australia/Melbourne")
    print(f"  User  : {GITHUB_USERNAME}")
    print(f"{'='*60}\n")

    # -- Tokens (loaded from .env or environment) --------------------------------
    github_token = os.environ.get("GITHUB_TOKEN")
    clockify_key = os.environ.get("CLOCKIFY_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if not clockify_key:
        sys.exit("[ERROR] CLOCKIFY_API_KEY not set. Add it to .env or export it.")

    if not github_token:
        print("[WARN] GITHUB_TOKEN not set — only public repos will be searched.")
        print("       For private/org repos:  export GITHUB_TOKEN=ghp_xxxx\n")
    if openai_key:
        print("[+] OpenAI GPT enabled for description generation\n")

    # ── Step 1: Fetch GitHub commits ─────────────────────────────────────────
    print("[1/6] Fetching GitHub commits...")
    commits = fetch_all_commits(GITHUB_USERNAME, date_from, date_to, github_token)
    print(f"       Total unique commits: {len(commits)}\n")

    # ── Step 2: Fetch Google Meet events ─────────────────────────────────────
    meetings: list[dict] = []
    if not args.no_meets:
        print("[2/6] Fetching Google Meet events...")
        try:
            meetings = fetch_google_meet_events(date_from, date_to)
            print(f"       {len(meetings)} meetings found\n")
        except Exception as e:
            print(f"       [WARN] Google Meet fetch failed: {e}")
            print("       Continuing without meetings...\n")
    else:
        print("[2/6] Skipping Google Meet (--no-meets)\n")

    # ── Step 2b: Fetch Slack DMs + huddles ───────────────────────────────────
    slack_dm_groups: dict[tuple[str, str], list[dict]] = {}
    slack_huddles: list[dict] = []
    slack_token = os.environ.get("SLACK_USER_TOKEN")

    if not args.no_slack and slack_token:
        print("[2b]  Fetching Slack DMs + huddles...")
        try:
            slack_dm_groups, slack_huddles, _ = fetch_slack_activity(slack_token, date_from, date_to)
            print(f"       {len(slack_dm_groups)} DM (email, day) groups, {len(slack_huddles)} huddles\n")
        except Exception as e:
            print(f"       [WARN] Slack fetch failed: {e}")
            print("       Continuing without Slack...\n")
    elif args.no_slack:
        print("[2b]  Skipping Slack (--no-slack)\n")
    else:
        print("[2b]  Skipping Slack (SLACK_USER_TOKEN not set)\n")

    if not commits and not meetings and not slack_dm_groups and not slack_huddles:
        print("[DONE] No commits, meetings, or Slack activity found. Nothing to sync.")
        return

    # ── Step 3: Connect to Clockify ──────────────────────────────────────────
    print("[3/6] Connecting to Clockify...")
    workspace_id = get_workspace_id(clockify_key)
    project_name_to_id = get_projects(clockify_key, workspace_id)
    print(f"       {len(project_name_to_id)} active projects found\n")

    # Ensure the fallback projects exist
    if MEETING_FALLBACK_PROJECT not in project_name_to_id and meetings:
        print(f"    [WARN] Clockify project '{MEETING_FALLBACK_PROJECT}' not found.")
        print(f"           Unmapped meetings will be skipped. Create it in Clockify to capture them.\n")
    if GIT_FALLBACK_PROJECT not in project_name_to_id:
        print(f"    [WARN] Clockify project '{GIT_FALLBACK_PROJECT}' not found.")
        print(f"           Unmapped repos will be skipped. Create it in Clockify to capture them.\n")

    # ── Step 4: Delete existing entries if --delete-range ────────────────────
    if args.delete_range:
        print("[4/6] Deleting existing script entries in range...")
        user_id = get_user_id(clockify_key)
        # Include both repo-mapped and meeting-mapped project IDs
        all_project_names = set(REPO_PROJECT_MAP.values()) | {
            MEETING_FALLBACK_PROJECT, GIT_FALLBACK_PROJECT, SLACK_FALLBACK_PROJECT,
        }
        for keywords, proj in MEETING_KEYWORD_MAP:
            all_project_names.add(proj)
        all_project_names.update(SLACK_EMAIL_DOMAIN_PROJECT_MAP.values())
        mapped_project_ids = set()
        for proj_name in all_project_names:
            pid = project_name_to_id.get(proj_name)
            if pid:
                mapped_project_ids.add(pid)
        deleted = delete_entries_in_range(
            clockify_key, workspace_id, user_id,
            date_from, date_to, mapped_project_ids,
            dry_run=args.dry_run,
        )
        label = "would delete" if args.dry_run else "deleted"
        print(f"       {deleted} entries {label}\n")
    else:
        print("[4/6] Skipping delete (use --delete-range to clean before sync)\n")

    # ── Step 5: Group commits & spread hours ────────────────────────────────
    print("[5/6] Grouping commits by repository and day...")
    groups = group_commits(commits)
    print(f"       {len(groups)} commit groups formed")

    hour_overrides, spread_entries = compute_spread_plan(groups)
    if spread_entries:
        print(f"       {len(spread_entries)} spread entries added (heavy days redistributed)")
    print()

    # ── Step 6: Create Clockify entries ──────────────────────────────────────
    print("[6/6] Creating Clockify time entries...\n")

    created = 0
    skipped_no_project = 0
    failed = 0

    # Helper to create or preview an entry
    def _create_entry(repo: str, day: str, project_name: str, project_id: str,
                      description: str, start_utc: datetime, end_utc: datetime,
                      hours: float, label: str, count_label: str) -> bool:
        nonlocal created, failed
        if args.dry_run:
            print(f"  [DRY] {day}  {repo:30s} -> {project_name:20s}  {hours:4.1f}h  {count_label}")
            preview = description.replace("\n", " | ")[:150]
            print(f"        {preview}...\n")
            created += 1
            return True
        else:
            try:
                entry = create_time_entry(clockify_key, workspace_id, project_id, description, start_utc, end_utc)
                entry_id = entry.get("id", "?")
                print(f"  [OK]  {day}  {repo:30s} -> {project_name:20s}  {hours:4.1f}h  {count_label}  id={entry_id}")
                created += 1
                _time.sleep(0.3)
                return True
            except requests.HTTPError as e:
                failed += 1
                print(f"  [ERR] {day}  {repo} -> {project_name}: {e}")
                if e.response is not None:
                    print(f"        {e.response.text[:300]}")
                return False

    # --- A) Create entries for actual commit groups ---
    for (repo, day), group in sorted(groups.items()):
        project_name = REPO_PROJECT_MAP.get(repo.lower())
        if not project_name:
            project_name = GIT_FALLBACK_PROJECT

        project_id = project_name_to_id.get(project_name)
        if not project_id:
            print(f"  [SKIP] Clockify project '{project_name}' not found in workspace (repo: {repo})")
            skipped_no_project += len(group)
            continue

        description = build_description(repo, day, group, openai_key)
        start_utc, end_utc, hours = compute_start_end(group, day)

        # Apply hour override if this day was spread
        if (repo, day) in hour_overrides:
            hours = hour_overrides[(repo, day)]
            end_utc = start_utc + timedelta(hours=hours)

        count_label = f"({len(group)} commit{'s' if len(group) != 1 else ''})"
        _create_entry(repo, day, project_name, project_id, description, start_utc, end_utc, hours, "commit", count_label)

    # --- B) Create spread entries for gap days ---
    for repo, gap_day, gap_hours, source_commits in sorted(spread_entries, key=lambda x: (x[0], x[1])):
        project_name = REPO_PROJECT_MAP.get(repo.lower())
        if not project_name:
            project_name = GIT_FALLBACK_PROJECT
        project_id = project_name_to_id.get(project_name)
        if not project_id:
            continue

        description = build_spread_description(repo, gap_day, source_commits, openai_key)
        start_utc = datetime.strptime(gap_day, "%Y-%m-%d").replace(hour=9, tzinfo=MELB_TZ).astimezone(timezone.utc)
        end_utc = start_utc + timedelta(hours=gap_hours)

        count_label = "(spread)"
        _create_entry(repo, gap_day, project_name, project_id, description, start_utc, end_utc, gap_hours, "spread", count_label)

    # --- C) Create entries for Google Meet meetings ---
    meet_created = 0
    meet_skipped = 0
    google_creds = None

    if meetings:
        print("\n  --- Google Meet Entries ---\n")
        google_creds = _get_google_creds()

    for meeting in sorted(meetings, key=lambda m: m["start"]):
        # Skip leave, social, OOO events
        if should_skip_meeting(meeting["summary"]):
            print(f"  [SKIP] Non-work meeting: {meeting['summary']}")
            meet_skipped += 1
            continue

        project_name = match_meeting_to_project(meeting["summary"])
        project_id = project_name_to_id.get(project_name)

        if not project_id:
            print(f"  [SKIP] No Clockify project '{project_name}' for meeting: {meeting['summary']}")
            meet_skipped += 1
            continue

        # Search for Gemini notes
        notes_text = None
        if google_creds:
            try:
                notes_text = _search_gemini_notes(google_creds, meeting["summary"], meeting["day"])
                if notes_text:
                    print(f"    [NOTES] Found Gemini notes for: {meeting['summary'][:50]}")
            except Exception as e:
                print(f"    [WARN] Notes search failed: {e}")

        description = build_meeting_description(meeting, notes_text, openai_key)
        start_utc = meeting["start"].astimezone(timezone.utc)
        end_utc = meeting["end"].astimezone(timezone.utc)
        hours = meeting["duration_h"]

        count_label = "(meet)"
        if _create_entry(
            meeting["summary"][:30], meeting["day"], project_name, project_id,
            description, start_utc, end_utc, hours, "meet", count_label,
        ):
            meet_created += 1

    # --- D) Create entries for Slack huddles (exact duration) ---
    slack_huddle_created = 0
    slack_conv_created = 0
    slack_skipped = 0

    def _slack_project_for_email(email: str) -> tuple[str, str] | None:
        proj_name = match_email_to_project(email) or SLACK_FALLBACK_PROJECT
        pid = project_name_to_id.get(proj_name)
        if not pid:
            return None
        return proj_name, pid

    # Sum huddle minutes per (email, day) so the conversation estimator can subtract them
    huddle_min_by_email_day: dict[tuple[str, str], int] = defaultdict(int)
    for h in slack_huddles:
        huddle_min_by_email_day[(h["email"], h["day"])] += h["duration_s"] // 60

    if slack_huddles:
        print("\n  --- Slack Huddle Entries ---\n")

    for h in sorted(slack_huddles, key=lambda x: x["start_utc"]):
        proj = _slack_project_for_email(h["email"])
        if not proj:
            print(f"  [SKIP] No Clockify project for huddle with {h['email']}")
            slack_skipped += 1
            continue
        proj_name, project_id = proj
        description = build_slack_huddle_description(h)
        hours = h["duration_s"] / 3600.0
        count_label = "(huddle)"
        if _create_entry(
            f"huddle:{h['email'][:22]}", h["day"], proj_name, project_id,
            description, h["start_utc"], h["end_utc"], hours, "huddle", count_label,
        ):
            slack_huddle_created += 1

    # --- E) Create entries for Slack DM conversations (OpenAI-estimated time) ---
    if slack_dm_groups:
        print("\n  --- Slack Conversation Entries ---\n")

    for (email, day), messages in sorted(slack_dm_groups.items()):
        proj = _slack_project_for_email(email)
        if not proj:
            print(f"  [SKIP] No Clockify project for DM with {email}")
            slack_skipped += 1
            continue
        proj_name, project_id = proj

        huddle_mins = huddle_min_by_email_day.get((email, day), 0)
        hours, description = build_slack_conversation_entry(
            email, day, messages, huddle_mins, openai_key,
        )
        start_utc, end_utc = _slack_entry_start_end(messages, hours)
        count_label = f"({len(messages)} msgs)"
        if _create_entry(
            f"slack:{email[:22]}", day, proj_name, project_id,
            description, start_utc, end_utc, hours, "slack", count_label,
        ):
            slack_conv_created += 1

    # ── Summary ──────────────────────────────────────────────────────────────
    label = "WOULD CREATE" if args.dry_run else "CREATED"
    git_count = created - meet_created - slack_huddle_created - slack_conv_created
    print(f"\n{'='*60}")
    print(f"  {label}       : {created} time entries")
    print(f"    Git entries      : {git_count}")
    if meet_created or meet_skipped:
        print(f"    Meet entries     : {meet_created}")
    if slack_huddle_created or slack_conv_created:
        print(f"    Slack huddles    : {slack_huddle_created}")
        print(f"    Slack DMs        : {slack_conv_created}")
    print(f"  Skipped (no proj)  : {skipped_no_project} commits")
    if meet_skipped:
        print(f"  Skipped meetings   : {meet_skipped} (no matching project)")
    if slack_skipped:
        print(f"  Skipped Slack      : {slack_skipped} (no matching project)")
    if failed:
        print(f"  Failed             : {failed} entries")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
