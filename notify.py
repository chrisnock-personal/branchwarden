"""
notify.py
~~~~~~~~~
Microsoft Teams and Slack channels: which channels a PR goes to, the queue of
messages, and turning one message into a Teams Adaptive Card (posted to a
Workflows webhook) or a Slack Block Kit message (posted to an incoming
webhook).

A message is stored as a plain dict and only rendered when it's sent, so
@mentions use the People mapping as it is at that moment. Text may contain
@{login} for a mention and `code` for a branch name.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import requests
from psycopg2.extras import Json

import analysis
import db
import settings

log = logging.getLogger("branchwarden.notify")

TIMEOUT = 15
RETRY_STEPS = (60, 300, 900, 3600, 3 * 3600, 6 * 3600)   # then give up
MENTION = re.compile(r"@\{([A-Za-z0-9_.\-\[\]]+)\}")
CODE = re.compile(r"`([^`]+)`")

TEAMS_COLOR = {"warn": "Warning", "bad": "Attention", "good": "Good", "info": "Accent", "muted": "Default"}
SLACK_EMOJI = {"warn": ":hourglass_flowing_sand:", "bad": ":warning:", "good": ":white_check_mark:",
               "info": ":speech_balloon:", "muted": ":information_source:"}


class PermanentFailure(Exception):
    pass


class RetryLater(Exception):
    pass


# ─── Routing ────────────────────────────────────────────────────────────────

def channel_matches(channel: dict, full_name: str, teams: Iterable[str]) -> bool:
    repos = settings.split_list(channel.get("repos", ""))
    wanted = {t.lower().lstrip("@") for t in settings.split_list(channel.get("teams", ""))}
    if not repos and not wanted:
        return True
    if repos and analysis.matches(full_name, repos):
        return True
    return bool(wanted & {t.lower() for t in teams})


def channels_for(conn, target: str, full_name: Optional[str] = None, teams: Iterable[str] = ()) -> list[dict]:
    """Enabled channels for a rule's target: 'routed' (by the PR's repo and
    requested teams), 'all', or one channel's id."""
    chans = db.rows(conn, "SELECT * FROM channels WHERE enabled ORDER BY id")
    if target == "all":
        return chans
    if target and target.isdigit():
        return [c for c in chans if c["id"] == int(target)]
    if full_name is None:
        return chans
    return [c for c in chans if channel_matches(c, full_name, teams)]


def enqueue(conn, channels: list[dict], kind: str, message: dict, pr_id: Optional[int] = None) -> int:
    for c in channels:
        db.execute(conn, "INSERT INTO notifications (channel_id, kind, pr_id, payload) VALUES (%s, %s, %s, %s)",
                   (c["id"], kind, pr_id, Json(message)))
    return len(channels)


# ─── Rendering ──────────────────────────────────────────────────────────────

def _people(conn) -> dict[str, dict]:
    return {p["login"]: p for p in db.rows(conn, "SELECT * FROM people")}


def mentioned(message: dict) -> list[str]:
    text = " ".join([message.get("heading", ""), message.get("text", "")]
                    + [str(v) for _, v in message.get("facts", [])]
                    + [ln.get("text", "") for sec in message.get("sections", []) for ln in sec["lines"]])
    out: list[str] = []
    for m in MENTION.findall(text):
        if m.lower() not in out:
            out.append(m.lower())
    return out


def _teams_text(text: str, people: dict, entities: dict) -> str:
    def mention(m):
        login = m.group(1).lower()
        p = people.get(login)
        if p and p.get("teams_id"):
            name = p.get("name") or login
            tag = f"<at>{name}</at>"
            entities[tag] = {"type": "mention", "text": tag, "mentioned": {"id": p["teams_id"], "name": name}}
            return tag
        return f"**@{p['name'] if p and p.get('name') else login}**"
    return MENTION.sub(mention, text)


def teams_card(message: dict, people: dict) -> dict:
    entities: dict = {}
    t = lambda s: _teams_text(str(s), people, entities)  # noqa: E731
    body: list[dict] = [{"type": "TextBlock", "text": t(message["heading"]), "weight": "Bolder",
                         "size": "Medium", "color": TEAMS_COLOR.get(message.get("tone", "info"), "Default"),
                         "wrap": True}]
    if message.get("title"):
        title = message["title"]
        if message.get("title_url"):
            title = f"[{title}]({message['title_url']})"
        body.append({"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True})
    if message.get("facts"):
        body.append({"type": "FactSet", "facts": [{"title": k, "value": t(v)} for k, v in message["facts"]]})
    if message.get("text"):
        body.append({"type": "TextBlock", "text": t(message["text"]), "wrap": True})
    for section in message.get("sections", []):
        body.append({"type": "TextBlock", "text": section["title"], "weight": "Bolder", "wrap": True,
                     "spacing": "Medium"})
        for line in section["lines"]:
            parts = []
            if line.get("link"):
                parts.append(f"[{line.get('label', 'link')}]({line['link']})")
            if line.get("code"):
                parts.append(f"`{line['code']}`")
            if line.get("text"):
                parts.append(t(line["text"]))
            body.append({"type": "TextBlock", "text": " ".join(parts), "wrap": True, "spacing": "Small"})
        if section.get("more"):
            body.append({"type": "TextBlock", "text": section["more"], "isSubtle": True, "wrap": True})
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
            "version": "1.4", "body": body, "msteams": {"width": "Full"}}
    if entities:
        card["msteams"]["entities"] = list(entities.values())
    if message.get("buttons"):
        card["actions"] = [{"type": "Action.OpenUrl", "title": label, "url": url}
                           for label, url in message["buttons"] if url]
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                "contentUrl": None, "content": card}]}


def _slack_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_text(text: str, people: dict) -> str:
    def mention(m):
        login = m.group(1).lower()
        p = people.get(login)
        if p and p.get("slack_id"):
            return f"<@{p['slack_id']}>"
        return f"*@{p['name'] if p and p.get('name') else login}*"
    return MENTION.sub(mention, _slack_escape(text))


def _slack_line(line: dict, people: dict) -> str:
    parts = []
    if line.get("link"):
        parts.append(f"<{line['link']}|{_slack_escape(line.get('label', 'link'))}>")
    if line.get("code"):
        parts.append(f"`{_slack_escape(line['code'])}`")
    if line.get("text"):
        parts.append(_slack_text(line["text"], people))
    return " ".join(parts)


def slack_message(message: dict, people: dict) -> dict:
    s = lambda v: _slack_text(v, people)  # noqa: E731
    emoji = SLACK_EMOJI.get(message.get("tone", "info"), "")
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn",
                                                       "text": f"{emoji} *{s(message['heading'])}*".strip()}}]
    if message.get("title"):
        title = _slack_escape(message["title"])
        if message.get("title_url"):
            title = f"<{message['title_url']}|{title}>"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}})
    if message.get("facts"):
        fields = [{"type": "mrkdwn", "text": f"*{_slack_escape(k)}*\n{s(v)}"} for k, v in message["facts"]]
        for i in range(0, len(fields), 10):
            blocks.append({"type": "section", "fields": fields[i:i + 10]})
    if message.get("text"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": s(message["text"])}})
    for section in message.get("sections", []):
        lines = [f"*{_slack_escape(section['title'])}*"] + [_slack_line(ln, people) for ln in section["lines"]]
        if section.get("more"):
            lines.append(f"_{_slack_escape(section['more'])}_")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}})
    if message.get("buttons"):
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": label}, "url": url}
            for label, url in message["buttons"] if url]})
    fallback = message["heading"] + (f": {message['title']}" if message.get("title") else "")
    return {"text": MENTION.sub(lambda m: "@" + m.group(1), fallback), "blocks": blocks}


def render(kind: str, message: dict, people: dict) -> dict:
    return teams_card(message, people) if kind == "teams" else slack_message(message, people)


# ─── Sending ────────────────────────────────────────────────────────────────

def post(url: str, payload: dict) -> None:
    try:
        resp = requests.post(url, json=payload, timeout=TIMEOUT)
    except requests.RequestException as exc:
        # Not str(exc): it contains the URL, which is the secret.
        raise RetryLater(f"can't connect ({type(exc).__name__})") from None
    if resp.status_code in (429,) or resp.status_code >= 500:
        raise RetryLater(f"HTTP {resp.status_code}")
    if resp.status_code >= 400:
        detail = resp.text.strip()[:120]
        raise PermanentFailure(f"HTTP {resp.status_code}" + (f": {detail}" if detail else ""))


SLACK_API = "https://slack.com/api/chat.postMessage"
# Slack API errors worth trying again; anything else is a setup problem.
SLACK_RETRY = {"ratelimited", "service_unavailable", "fatal_error", "internal_error", "request_timeout"}
# Events also shown in the channel, not only inside the PR's thread.
BROADCAST = {"merged", "closed"}


def slack_api_post(token: str, payload: dict) -> str:
    """Post through chat.postMessage; returns the message's ts."""
    try:
        resp = requests.post(SLACK_API, json=payload, timeout=TIMEOUT,
                             headers={"Authorization": f"Bearer {token}"})
    except requests.RequestException as exc:
        raise RetryLater(f"can't connect ({type(exc).__name__})") from None
    if resp.status_code == 429 or resp.status_code >= 500:
        raise RetryLater(f"HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise PermanentFailure(f"HTTP {resp.status_code}: not a Slack API answer") from None
    if not body.get("ok"):
        error = body.get("error", "unknown error")
        if error in SLACK_RETRY:
            raise RetryLater(f"Slack: {error}")
        raise PermanentFailure(f"Slack: {error}")
    return body.get("ts", "")


def dispatch(conn, values: dict, channel: dict, message: dict, people: dict, *,
             kind: str = "", pr_id: Optional[int] = None) -> None:
    """Send one message to one channel. A Slack channel with a bot token posts
    through the API, keeping each PR's messages in one thread; everything
    else goes to the channel's webhook."""
    cid = channel["id"]
    token = values.get(settings.channel_token_key(cid))
    if channel["kind"] == "slack" and token and channel.get("slack_channel"):
        payload = {**slack_message(message, people), "channel": channel["slack_channel"],
                   "unfurl_links": False, "unfurl_media": False}
        thread = None
        if pr_id and kind != "digest":
            row = db.one(conn, "SELECT ts FROM threads WHERE channel_id = %s AND pr_id = %s", (cid, pr_id))
            thread = row["ts"] if row else None
        if thread:
            payload["thread_ts"] = thread
            payload["reply_broadcast"] = kind in BROADCAST
        ts = slack_api_post(token, payload)
        if pr_id and not thread and ts and kind != "digest":
            db.execute(conn, """INSERT INTO threads (channel_id, pr_id, ts) VALUES (%s, %s, %s)
                                ON CONFLICT DO NOTHING""", (cid, pr_id, ts))
        return
    url = values.get(settings.channel_url_key(cid))
    if not url:
        raise PermanentFailure("no webhook URL or bot token is set")
    post(url, render(channel["kind"], message, people))


def send(conn, channel: dict, message: dict, people: Optional[dict] = None) -> None:
    dispatch(conn, settings.load(conn), channel, message, people if people is not None else _people(conn))


def deliver(conn, limit: int = 50) -> tuple[int, int]:
    """Send what's due; returns (sent, failed)."""
    due = db.rows(conn, """
        SELECT n.*, c.kind AS channel_kind, c.enabled AS channel_enabled, c.slack_channel
          FROM notifications n JOIN channels c ON c.id = n.channel_id
         WHERE n.status = 'pending' AND n.next_attempt_at <= NOW()
         ORDER BY n.id LIMIT %s FOR UPDATE OF n SKIP LOCKED""", (limit,))
    if not due:
        return 0, 0
    values = settings.load(conn)
    people = _people(conn)
    sent = failed = 0
    for n in due:
        channel = {"id": n["channel_id"], "kind": n["channel_kind"], "slack_channel": n["slack_channel"]}
        try:
            if not n["channel_enabled"]:
                raise PermanentFailure("the channel is switched off")
            dispatch(conn, values, channel, n["payload"], people, kind=n["kind"], pr_id=n["pr_id"])
        except RetryLater as exc:
            step = n["attempts"]
            if step >= len(RETRY_STEPS):
                db.execute(conn, """UPDATE notifications SET status = 'failed', attempts = attempts + 1,
                                           last_error = %s WHERE id = %s""", (f"gave up: {exc}", n["id"]))
                failed += 1
            else:
                db.execute(conn, """UPDATE notifications SET attempts = attempts + 1, last_error = %s,
                                           next_attempt_at = %s WHERE id = %s""",
                           (str(exc), datetime.now(timezone.utc) + timedelta(seconds=RETRY_STEPS[step]), n["id"]))
            continue
        except PermanentFailure as exc:
            db.execute(conn, """UPDATE notifications SET status = 'failed', attempts = attempts + 1,
                                       last_error = %s WHERE id = %s""", (str(exc), n["id"]))
            failed += 1
            continue
        db.execute(conn, """UPDATE notifications SET status = 'sent', attempts = attempts + 1,
                                   last_error = NULL, sent_at = NOW() WHERE id = %s""", (n["id"],))
        sent += 1
    return sent, failed


def channel_health(conn) -> list[dict]:
    """Each channel with how it's doing, for the dashboard."""
    out = []
    for c in db.rows(conn, """
            SELECT c.*,
                   (SELECT COUNT(*) FROM notifications n WHERE n.channel_id = c.id AND n.status = 'sent'
                       AND n.sent_at > NOW() - INTERVAL '24 hours') AS sent_24h,
                   (SELECT COUNT(*) FROM notifications n WHERE n.channel_id = c.id AND n.status = 'pending')
                       AS pending,
                   (SELECT row_to_json(x) FROM (SELECT status, last_error, created_at FROM notifications n
                       WHERE n.channel_id = c.id AND (n.status <> 'pending' OR n.attempts > 0)
                       ORDER BY n.id DESC LIMIT 1) x) AS latest
              FROM channels c ORDER BY c.id"""):
        latest = c["latest"] or {}
        if not c["enabled"]:
            status, tone, detail = "off", "muted", "Switched off"
        elif latest.get("status") == "failed" or (latest.get("status") == "pending" and latest.get("last_error")):
            status, tone, detail = "failing", "bad", latest.get("last_error") or "failed"
        elif latest:
            status, tone = "ok", "good"
            detail = f"{c['sent_24h']} sent in 24 h"
        else:
            status, tone, detail = "idle", "muted", "Nothing sent yet"
        out.append({**c, "kind_label": "Teams" if c["kind"] == "teams" else "Slack",
                    "status": status, "tone": tone, "detail": detail})
    return out
