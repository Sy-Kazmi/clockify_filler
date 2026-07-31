#!/usr/bin/env python3
"""
slack_fetch_test.py — Fetch Slack conversations and messages, write to log file.

No Clockify writes, no time calculations — just a read-only inspection so we can
eyeball what Slack returns before wiring it into the Clockify sync.

USAGE:
    python slack_fetch_test.py                       # last 7 days, default log path
    python slack_fetch_test.py --from 2026-04-01 --to 2026-04-20
    python slack_fetch_test.py --out /tmp/slack.log
    python slack_fetch_test.py --types im            # DMs only
    python slack_fetch_test.py --types im,mpim,public_channel,private_channel

Requires SLACK_USER_TOKEN in .env.
"""

import argparse
import json
import os
import sys
import time as _time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_FILE = os.path.join(_SCRIPT_DIR, ".env")
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

SLACK_API = "https://slack.com/api"
MELB_TZ = ZoneInfo("Australia/Melbourne")


def slack_call(token: str, endpoint: str, params: dict | None = None) -> dict:
    """GET a Slack Web API method. Raises on transport error or ok=false."""
    params = dict(params or {})
    for attempt in range(4):
        resp = requests.get(
            f"{SLACK_API}/{endpoint}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2"))
            print(f"    [rate-limit] sleeping {wait}s on {endpoint}")
            _time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            err = data.get("error", "unknown")
            raise RuntimeError(f"Slack API {endpoint} failed: {err}")
        return data
    raise RuntimeError(f"Slack API {endpoint} exhausted retries")


def list_conversations(token: str, types: str) -> list[dict]:
    convs: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict = {"types": types, "limit": 200, "exclude_archived": True}
        if cursor:
            params["cursor"] = cursor
        data = slack_call(token, "users.conversations", params)
        convs.extend(data.get("channels", []))
        cursor = data.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            break
        _time.sleep(0.3)
    return convs


def fetch_history(token: str, channel_id: str, oldest_ts: str, latest_ts: str) -> list[dict]:
    msgs: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict = {
            "channel": channel_id,
            "oldest": oldest_ts,
            "latest": latest_ts,
            "limit": 200,
            "inclusive": True,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            data = slack_call(token, "conversations.history", params)
        except (requests.HTTPError, RuntimeError) as e:
            print(f"    [ERR] history for {channel_id}: {e}")
            break
        msgs.extend(data.get("messages", []))
        if not data.get("has_more"):
            break
        cursor = data.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            break
        _time.sleep(0.5)
    return msgs


def get_user(token: str, user_id: str, cache: dict[str, dict]) -> dict:
    if user_id in cache:
        return cache[user_id]
    try:
        data = slack_call(token, "users.info", {"user": user_id})
        u = data.get("user", {})
        info = {
            "id": user_id,
            "name": u.get("real_name") or u.get("name") or user_id,
            "email": u.get("profile", {}).get("email", ""),
            "is_bot": u.get("is_bot", False),
        }
    except Exception as e:
        info = {"id": user_id, "name": user_id, "email": "", "error": str(e)}
    cache[user_id] = info
    _time.sleep(0.15)
    return info


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="date_from", default=None, help="YYYY-MM-DD (default: 7 days ago)")
    parser.add_argument("--to", dest="date_to", default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--out", default=os.path.join(_SCRIPT_DIR, "slack_fetch.log"))
    parser.add_argument(
        "--types",
        default="im,mpim,public_channel,private_channel",
        help="Slack conversation types, comma-separated",
    )
    parser.add_argument("--raw-json", default=None, help="Optional path to also dump raw JSON")
    args = parser.parse_args()

    token = os.environ.get("SLACK_USER_TOKEN")
    if not token:
        sys.exit("[ERROR] SLACK_USER_TOKEN not set. Add it to .env")

    today = date.today()
    date_from = args.date_from or (today - timedelta(days=7)).isoformat()
    date_to = args.date_to or today.isoformat()

    oldest_dt = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=MELB_TZ)
    latest_dt = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=MELB_TZ)
    oldest_ts = f"{oldest_dt.timestamp():.6f}"
    latest_ts = f"{latest_dt.timestamp():.6f}"

    print(f"\n{'='*60}")
    print(f"  Slack fetch test")
    print(f"  Range : {date_from} to {date_to} (Melbourne)")
    print(f"  Types : {args.types}")
    print(f"  Out   : {args.out}")
    print(f"{'='*60}\n")

    me = slack_call(token, "auth.test")
    my_id = me.get("user_id", "")
    print(f"[+] Authed as {me.get('user')} ({my_id}) in {me.get('team')}\n")

    print("[1/3] Listing conversations...")
    convs = list_conversations(token, args.types)
    print(f"       {len(convs)} conversations\n")

    user_cache: dict[str, dict] = {}
    lines: list[str] = []
    raw_dump: list[dict] = []
    huddles: list[dict] = []

    lines.append(f"=== Slack fetch {date_from} to {date_to} (Melbourne) ===")
    lines.append(f"Authed as: {me.get('user')} ({my_id}) / team: {me.get('team')}")
    lines.append(f"Conversations matched: {len(convs)}\n")

    print("[2/3] Fetching history per conversation...")
    total_msgs = 0
    convs_with_msgs = 0

    for idx, conv in enumerate(convs, 1):
        cid = conv["id"]
        is_im = conv.get("is_im", False)
        is_mpim = conv.get("is_mpim", False)
        is_private = conv.get("is_private", False)

        if is_im:
            conv_type = "IM"
            other_user_id = conv.get("user", "")
            other = get_user(token, other_user_id, user_cache) if other_user_id else {"name": "?", "email": ""}
            label = f"DM with {other['name']}"
            other_email = other.get("email", "")
        elif is_mpim:
            conv_type = "MPIM"
            label = conv.get("name", "(mpim)")
            other_email = ""
        else:
            conv_type = "PRIVATE_CHANNEL" if is_private else "PUBLIC_CHANNEL"
            label = f"#{conv.get('name', cid)}"
            other_email = ""

        print(f"  [{idx:3d}/{len(convs)}] {conv_type:16s} {label[:50]:50s}", end="", flush=True)
        msgs = fetch_history(token, cid, oldest_ts, latest_ts)
        print(f" -> {len(msgs)} msgs")

        if not msgs:
            continue
        convs_with_msgs += 1
        total_msgs += len(msgs)

        lines.append(f"\n{'─'*70}")
        lines.append(f"[{conv_type}] {label}")
        lines.append(f"  channel_id: {cid}")
        if other_email:
            lines.append(f"  other_email: {other_email}")
        lines.append(f"  messages: {len(msgs)}")
        lines.append("")

        for m in sorted(msgs, key=lambda x: float(x.get("ts", "0"))):
            ts = float(m.get("ts", "0"))
            when = datetime.fromtimestamp(ts, MELB_TZ).strftime("%Y-%m-%d %H:%M:%S")
            user_id = m.get("user") or m.get("bot_id") or ""
            if user_id and user_id not in user_cache and not user_id.startswith("B"):
                get_user(token, user_id, user_cache)
            uname = user_cache.get(user_id, {}).get("name", user_id or "system")
            uemail = user_cache.get(user_id, {}).get("email", "")
            text = (m.get("text", "") or "").replace("\n", " ")
            if len(text) > 300:
                text = text[:300] + "..."
            subtype = m.get("subtype", "")
            marker = f" [{subtype}]" if subtype else ""
            email_part = f" <{uemail}>" if uemail else ""
            lines.append(f"  [{when}] {uname}{email_part}{marker}: {text}")

            if subtype == "huddle_thread":
                room = m.get("room") or {}
                d_start = int(room.get("date_start") or 0)
                d_end = int(room.get("date_end") or 0)
                participant_ids = room.get("participant_history") or room.get("participants") or []
                # Resolve participant identities (skip bots and self-cache)
                parts: list[dict] = []
                for pid in participant_ids:
                    if pid and not pid.startswith("B") and pid not in user_cache:
                        get_user(token, pid, user_cache)
                    parts.append(user_cache.get(pid, {"id": pid, "name": pid, "email": ""}))
                duration_s = max(0, d_end - d_start) if d_start and d_end else 0
                huddles.append({
                    "channel_id": cid,
                    "conv_type": conv_type,
                    "conv_label": label,
                    "other_email": other_email,
                    "date_start": d_start,
                    "date_end": d_end,
                    "duration_s": duration_s,
                    "participants": parts,
                    "room_id": room.get("id", ""),
                    "msg_ts": ts,
                    "active": bool(d_start and not d_end),
                })

        raw_dump.append({
            "conversation": conv,
            "type": conv_type,
            "label": label,
            "other_email": other_email,
            "messages": msgs,
        })

        _time.sleep(0.3)

    # ── Huddles section ──
    lines.append(f"\n{'='*70}")
    lines.append(f"HUDDLES ({len(huddles)} total)")
    lines.append(f"{'='*70}")

    total_huddle_s = 0
    huddle_s_by_email: dict[str, int] = {}

    for h in sorted(huddles, key=lambda x: x["msg_ts"]):
        start_str = datetime.fromtimestamp(h["date_start"], MELB_TZ).strftime("%Y-%m-%d %H:%M:%S") if h["date_start"] else "?"
        end_str = datetime.fromtimestamp(h["date_end"], MELB_TZ).strftime("%H:%M:%S") if h["date_end"] else ("active" if h["active"] else "?")
        dur_s = h["duration_s"]
        dur_str = f"{dur_s // 60}m {dur_s % 60}s" if dur_s else ("(active)" if h["active"] else "(no end_ts)")
        total_huddle_s += dur_s

        lines.append("")
        lines.append(f"[{h['conv_type']}] {h['conv_label']}")
        lines.append(f"  start   : {start_str}")
        lines.append(f"  end     : {end_str}")
        lines.append(f"  duration: {dur_str}")
        if h["other_email"]:
            lines.append(f"  dm_email: {h['other_email']}")
        if h["participants"]:
            lines.append(f"  participants:")
            for p in h["participants"]:
                em = p.get("email", "") or ""
                lines.append(f"    - {p.get('name', p.get('id', '?'))}{' <' + em + '>' if em else ''}")
            # Attribute duration to each DM counterpart email for later project mapping
            if h["conv_type"] == "IM" and h["other_email"]:
                huddle_s_by_email[h["other_email"]] = huddle_s_by_email.get(h["other_email"], 0) + dur_s

    lines.append("")
    lines.append(f"Total huddle time: {total_huddle_s // 60}m {total_huddle_s % 60}s "
                 f"({total_huddle_s / 3600:.2f}h)")
    if huddle_s_by_email:
        lines.append("Huddle time by DM counterpart email:")
        for email, secs in sorted(huddle_s_by_email.items(), key=lambda x: -x[1]):
            lines.append(f"  {email:40s} {secs // 60}m {secs % 60}s")

    lines.append(f"\n{'='*70}")
    lines.append(f"Summary: {total_msgs} messages across {convs_with_msgs}/{len(convs)} conversations")
    lines.append(f"Huddles : {len(huddles)} (total {total_huddle_s // 60}m {total_huddle_s % 60}s)")
    lines.append(f"Unique users resolved: {len(user_cache)}")
    lines.append(f"{'='*70}")

    print(f"\n[3/3] Writing log to {args.out}")
    with open(args.out, "w") as f:
        f.write("\n".join(lines))

    if args.raw_json:
        with open(args.raw_json, "w") as f:
            json.dump({"users": user_cache, "conversations": raw_dump}, f, indent=2, default=str)
        print(f"       Raw JSON -> {args.raw_json}")

    print(
        f"\n[DONE] {total_msgs} messages / {convs_with_msgs} convs / "
        f"{len(huddles)} huddles ({total_huddle_s // 60}m total) / "
        f"{len(user_cache)} users\n"
    )


if __name__ == "__main__":
    main()
