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
    "gamdom":                       "Gamedom",
    "i18n":                         "Gamedom",
    "gamdon-reporting":             "Gamedom",
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
    (["gamdom", "gamedom"],                  "Gamedom"),
    (["lisnic"],                             "Lisnic"),
    (["nick"],                               "Nick's Projects"),
    (["outsourcey"],                          "Outsourcey"),
    (["sentr"],                              "SENTR 2.0"),
    (["super young", "superyoung"],          "Super Young"),
]
MEETING_FALLBACK_PROJECT = "Google Meet - Unassigned"

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
        params = {"per_page": 100, "page": page}
        try:
            resp = _gh_get(url, params, token)
        except requests.HTTPError:
            break
        events = resp.json()
        if not events:
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

        page += 1
        _time.sleep(0.5)

    return commits


def _discover_mapped_repos(token: str | None) -> dict[str, str]:
    """Return {repo_name_lower: full_name} for all accessible repos that match our mapping."""
    if not token:
        return {}
    url = f"{GITHUB_API}/user/repos"
    mapping_keys = set(REPO_PROJECT_MAP.keys())
    matched: dict[str, str] = {}
    page = 1
    while True:
        resp = _gh_get(url, {"per_page": 100, "page": page, "affiliation": "owner,collaborator,organization_member"}, token)
        batch = resp.json()
        if not batch:
            break
        for repo in batch:
            name_lower = repo["name"].lower()
            if name_lower in mapping_keys:
                matched[name_lower] = repo["full_name"]
        if len(batch) < 100:
            break
        page += 1
    return matched


def fetch_commits_repo_branches(username: str, date_from: str, date_to: str, token: str | None) -> list[dict]:
    """Fetch commits from ALL branches of each mapped repo — catches staging/feature work."""
    if not token:
        return []
    repo_map = _discover_mapped_repos(token)
    if not repo_map:
        return []

    print(f"    Found {len(repo_map)} mapped repos to scan")
    commits: list[dict] = []
    since = f"{date_from}T00:00:00Z"
    until = f"{date_to}T23:59:59Z"

    for repo_lower, full_name in sorted(repo_map.items()):
        # List branches
        try:
            resp = _gh_get(f"{GITHUB_API}/repos/{full_name}/branches", {"per_page": 100}, token)
            branches = [b["name"] for b in resp.json()]
        except requests.HTTPError:
            continue

        seen_shas: set[str] = set()
        repo_count = 0

        for branch in branches:
            page = 1
            while True:
                try:
                    resp = _gh_get(
                        f"{GITHUB_API}/repos/{full_name}/commits",
                        {"author": username, "since": since, "until": until, "sha": branch, "per_page": 100, "page": page},
                        token,
                    )
                except requests.HTTPError:
                    break
                items = resp.json()
                if not items or not isinstance(items, list):
                    break
                for item in items:
                    sha7 = item.get("sha", "")[:7]
                    if sha7 in seen_shas:
                        continue
                    seen_shas.add(sha7)
                    commits.append({
                        "repo": full_name.split("/")[-1],
                        "repo_full": full_name,
                        "message": item.get("commit", {}).get("message", "").split("\n")[0].strip(),
                        "date": item.get("commit", {}).get("author", {}).get("date", ""),
                        "sha": sha7,
                    })
                    repo_count += 1
                if len(items) < 100:
                    break
                page += 1
            _time.sleep(0.2)

        if repo_count:
            print(f"      {full_name}: {repo_count} commits across {len(branches)} branches")

    return commits


def fetch_all_commits(username: str, date_from: str, date_to: str, token: str | None) -> list[dict]:
    """Merge results from repo-branch scan + Search API + Events API, deduplicate by SHA."""
    print("    Scanning mapped repos (all branches)...")
    repo_commits = fetch_commits_repo_branches(username, date_from, date_to, token)
    print(f"    -> {len(repo_commits)} commits from repo branches")

    print("    Querying Search Commits API (default branches + unmapped repos)...")
    search_commits = fetch_commits_search_api(username, date_from, date_to, token)
    print(f"    -> {len(search_commits)} commits from Search API")

    print("    Querying Events API (supplement)...")
    events_commits = fetch_commits_events_api(username, date_from, date_to, token)
    print(f"    -> {len(events_commits)} commits from Events API")

    # Deduplicate by SHA — repo-branch results take priority
    seen: set[str] = set()
    merged: list[dict] = []
    for c in repo_commits + search_commits + events_commits:
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
        "description": description,
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

    if not commits and not meetings:
        print("[DONE] No commits or meetings found. Nothing to sync.")
        return

    # ── Step 3: Connect to Clockify ──────────────────────────────────────────
    print("[3/6] Connecting to Clockify...")
    workspace_id = get_workspace_id(clockify_key)
    project_name_to_id = get_projects(clockify_key, workspace_id)
    print(f"       {len(project_name_to_id)} active projects found\n")

    # Ensure the fallback meeting project exists
    if MEETING_FALLBACK_PROJECT not in project_name_to_id and meetings:
        print(f"    [WARN] Clockify project '{MEETING_FALLBACK_PROJECT}' not found.")
        print(f"           Unmapped meetings will be skipped. Create it in Clockify to capture them.\n")

    # ── Step 4: Delete existing entries if --delete-range ────────────────────
    if args.delete_range:
        print("[4/6] Deleting existing script entries in range...")
        user_id = get_user_id(clockify_key)
        # Include both repo-mapped and meeting-mapped project IDs
        all_project_names = set(REPO_PROJECT_MAP.values()) | {MEETING_FALLBACK_PROJECT}
        for keywords, proj in MEETING_KEYWORD_MAP:
            all_project_names.add(proj)
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
    skipped_unmapped = 0
    skipped_no_project = 0
    failed = 0
    unmapped_repos: set[str] = set()

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
            unmapped_repos.add(repo)
            skipped_unmapped += len(group)
            continue

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
            continue
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
        _create_entry(
            meeting["summary"][:30], meeting["day"], project_name, project_id,
            description, start_utc, end_utc, hours, "meet", count_label,
        )
        meet_created += 1

    # ── Summary ──────────────────────────────────────────────────────────────
    label = "WOULD CREATE" if args.dry_run else "CREATED"
    print(f"\n{'='*60}")
    print(f"  {label}       : {created} time entries")
    if meet_created or meet_skipped:
        print(f"    Git entries      : {created - meet_created}")
        print(f"    Meet entries     : {meet_created}")
    print(f"  Skipped (unmapped) : {skipped_unmapped} commits")
    print(f"  Skipped (no proj)  : {skipped_no_project} commits")
    if meet_skipped:
        print(f"  Skipped meetings   : {meet_skipped} (no matching project)")
    if failed:
        print(f"  Failed             : {failed} entries")
    if unmapped_repos:
        print(f"  Unmapped repos     : {', '.join(sorted(unmapped_repos))}")
        print(f"  (Add these to REPO_PROJECT_MAP in the script to include them)")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
