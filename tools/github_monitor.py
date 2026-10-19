#!/usr/bin/env python3
# ══════════════════════════════════════════════════════════════════════
#  MAGI — GitHub Monitor
#  Monitorea el repo Kyu-San/magi y notifica cambios.
#  Ejecutar via cron: */30 * * * * python3 ~/magi/tools/github_monitor.py
# ══════════════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

OWNER = "Kyu-San"
REPO = "magi"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
# Fallback: leer de archivo
if not TOKEN:
    token_file = Path("/home/kyusan/magi/.github_token")
    if token_file.exists():
        TOKEN = token_file.read_text().strip()
STATE_FILE = Path("/home/kyusan/magi/logs/github_state.json")
LOG_FILE = Path("/home/kyusan/magi/logs/github_monitor.log")
NOTIFY = True  # notificaciones de escritorio (notify-send)


def api(path: str) -> dict | list:
    url = f"https://api.github.com{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def notify(title: str, body: str):
    """Notificación de escritorio + log."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {title}: {body}"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line)
    if NOTIFY:
        try:
            subprocess.run(
                ["notify-send", "-a", "MAGI-GitHub", title, body],
                timeout=5,
                capture_output=True,
            )
        except Exception:
            pass


def send_telegram(title: str, body: str):
    """Envía notificación a Telegram si hay chat_id configurado."""
    token_file = Path("/home/kyusan/magi/.telegram_token")
    chat_file = Path("/home/kyusan/magi/.telegram_chat_id")
    if not token_file.exists():
        return
    token = token_file.read_text().strip()
    if not chat_file.exists():
        # El bot de Telegram guarda el chat_id cuando recibe un mensaje
        # Si no existe, intentar una vez más (puede que el bot aún no haya recibido mensajes)
        return
    chat_id = chat_file.read_text().strip()
    try:
        text = f"🔔 <b>{title}</b>\n\n{body}"
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps(
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        ).encode()
        req = Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=10) as resp:
            pass
        print(f"[Telegram] Notificación enviada a {chat_id}")
    except Exception as exc:
        print(f"[Telegram] Error: {exc}")
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def check_repo(state: dict) -> dict:
    """Verifica cambios en el repo principal."""
    changes = []
    new_state = dict(state)

    # ── Commits recientes ──
    commits = api(f"/repos/{OWNER}/{REPO}/commits?per_page=5")
    last_sha = state.get("last_commit_sha")
    new_commits = []
    for c in commits:
        if c["sha"] == last_sha:
            break
        new_commits.append(c)
    if new_commits and last_sha is not None:
        for c in reversed(new_commits):
            msg = c["commit"]["message"].split("\n")[0][:80]
            author = c["commit"]["author"]["name"]
            changes.append(f"📝 Nuevo commit: {msg} (por {author})")
    if commits:
        new_state["last_commit_sha"] = commits[0]["sha"]

    # ── Issues ──
    issues = api(f"/repos/{OWNER}/{REPO}/issues?state=all&per_page=20&sort=updated")
    known_issues = set(state.get("known_issues", []))
    for issue in issues:
        if issue.get("pull_request"):
            continue
        key = f"{issue['number']}:{issue['state']}"
        if key not in known_issues:
            if issue["state"] == "open":
                changes.append(
                    f"🐛 Nuevo issue #{issue['number']}: {issue['title'][:60]}"
                )
            else:
                changes.append(
                    f"✅ Issue #{issue['number']} cerrado: {issue['title'][:60]}"
                )
            known_issues.add(key)
    new_state["known_issues"] = list(known_issues)

    # ── Pull Requests ──
    prs = api(f"/repos/{OWNER}/{REPO}/pulls?state=all&per_page=20&sort=updated")
    known_prs = set(state.get("known_prs", []))
    for pr in prs:
        key = f"{pr['number']}:{pr['state']}"
        if key not in known_prs:
            if pr["state"] == "open":
                changes.append(f"🔀 Nuevo PR #{pr['number']}: {pr['title'][:60]}")
            else:
                changes.append(
                    f"🔀 PR #{pr['number']} {pr['state']}: {pr['title'][:60]}"
                )
            known_prs.add(key)
    new_state["known_prs"] = list(known_prs)

    # ── Estrellas y forks ──
    repo_info = api(f"/repos/{OWNER}/{REPO}")
    stars = repo_info.get("stargazers_count", 0)
    forks = repo_info.get("forks_count", 0)
    if stars > state.get("stars", 0) and state.get("stars") is not None:
        diff = stars - state["stars"]
        changes.append(f"⭐ +{diff} estrella(s) (total: {stars})")
    if forks > state.get("forks", 0) and state.get("forks") is not None:
        diff = forks - state["forks"]
        changes.append(f"🍴 +{diff} fork(s) (total: {forks})")
    new_state["stars"] = stars
    new_state["forks"] = forks

    # ── Releases ──
    try:
        releases = api(f"/repos/{OWNER}/{REPO}/releases?per_page=5")
        known_releases = set(state.get("known_releases", []))
        for rel in releases:
            if rel["tag_name"] not in known_releases:
                changes.append(
                    f"🚀 Nuevo release: {rel['tag_name']} — {rel.get('name', '')[:50]}"
                )
                known_releases.add(rel["tag_name"])
        new_state["known_releases"] = list(known_releases)
    except Exception:
        pass

    return changes, new_state


def main():
    state = load_state()
    is_first_run = not state

    changes, new_state = check_repo(state)

    if is_first_run:
        notify(
            "MAGI GitHub Monitor",
            f"Monitor iniciado para {OWNER}/{REPO}. Estado base guardado.",
        )
        save_state(new_state)
        return

    if changes:
        body = "\n".join(changes)
        notify(f"MAGI GitHub — {len(changes)} cambio(s)", body)
        send_telegram(f"MAGI GitHub — {len(changes)} cambio(s)", body)
    else:
        ts = datetime.now().strftime("%H:%M")
        print(f"[{ts}] Sin cambios en {OWNER}/{REPO}")

    save_state(new_state)


if __name__ == "__main__":
    main()
