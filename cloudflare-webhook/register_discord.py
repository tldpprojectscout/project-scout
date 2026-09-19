#!/usr/bin/env python3
"""
register_discord.py — register the /scout and /verify slash commands with Discord (run once, and after edits).
/verify has no options: it grants the TLDP Student role to whoever runs it. The invite link is the gate,
so keep invites single-use; no name matching is involved.

  DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... python register_discord.py

Also reads ../secrets_local.py if the env vars are missing. Global command:
propagates to every server the app is invited to within about an hour.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import secrets_local as s
except ImportError:  # env-only
    s = None

APP = os.environ.get("DISCORD_APP_ID") or getattr(s, "DISCORD_APP_ID", "")
TOKEN = os.environ.get("DISCORD_BOT_TOKEN") or getattr(s, "DISCORD_BOT_TOKEN", "")

MAJORS = [("Quant", "quant"), ("Finance / FinTech", "fintech"), ("Software Engineering", "swe"),
          ("Cybersecurity", "cyber"), ("Data Analytics", "data"), ("Accounting", "accounting"), ("Project Management", "pm"), ("Digital Marketing", "marketing")]
LANES = [("Learning path (your year in order — start here)", "path"),
         ("Start here (idea lists, roadmaps, beginner basics)", "start"),
         ("Build this (fresh repos)", "build"), ("Contribute (good first issues)", "oss"),
         ("Research (fresh paper code)", "research"),
         ("Orgs (non-profit / public / private sector, resume-ready)", "orgs"),
         ("NYC hackathons (in-person, live from Devpost + MLH)", "hackathons"),
         ("Case studies (collections you can contribute to)", "cases"),
         ("Hugging Face (models, datasets & Spaces to build on)", "hf")]
COMMANDS = [{
    "name": "verify",
    "description": "Unlock every TLDP channel - run this once after you join",
    "options": [],
}, {
    "name": "scout",
    "description": "Pre-screened GitHub project ideas, open-source issues and paper code by major",
    "options": [
        {"type": 3, "name": "major", "description": "Your major", "required": False,
         "choices": [{"name": n, "value": v} for n, v in MAJORS]},
        {"type": 3, "name": "lane", "description": "What kind of repos (default: fresh repos to build)", "required": False,
         "choices": [{"name": n, "value": v} for n, v in LANES]},
        {"type": 3, "name": "keywords", "description": "Narrow the search, e.g. honeypot (letters, digits, spaces, . , & + # -)", "required": False,
         "max_length": 60},
        {"type": 3, "name": "level", "description": "Beginner (default this fall), Intermediate, or Advanced = hardest first", "required": False,
         "choices": [{"name": "Beginner", "value": "beginner"}, {"name": "Intermediate", "value": "intermediate"}, {"name": "Advanced (challenge me)", "value": "advanced"}]},
    ],
}]


def main() -> int:
    if not APP or not TOKEN:
        print(__doc__)
        return 1
    req = urllib.request.Request(f"https://discord.com/api/v10/applications/{APP}/commands", method="PUT",
                                 data=json.dumps(COMMANDS).encode(),
                                 headers={"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json",
                                          "User-Agent": "project-scout (github.com/tldpprojectscout/project-scout, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("registered:", [c["name"] for c in json.load(r)])
    except urllib.error.HTTPError as e:
        print("discord", e.code, e.read()[:300].decode(errors="replace"))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
