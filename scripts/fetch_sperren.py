"""
Sperren (und Verletzungen) je Team fuer die naechsten Spiele ueber die
API-Football /injuries-Endpoint. Diese Endpoint deckt laut API-Football
explizit "suspended" und "injured" Spieler ab, die fuers naechste Spiel
fehlen. Wird woechentlich montags aufgerufen (siehe .github/workflows/
spieltag.yml), damit vor dem kommenden Wochenende bekannt ist, wer fehlt.

Hinweis: Die genauen Feldnamen der API-Football-Antwort konnten in dieser
Session nicht live gegen einen echten API-Key getestet werden (kein Zugriff
auf API_FOOTBALL_KEY aus der Sandbox). Das Skript ist defensiv geschrieben
(mehrere moegliche Feldnamen-Varianten) und gibt bei unerwarteter Struktur
die Rohantwort in den Logs aus, statt still zu scheitern -- beim ersten
echten Lauf in GitHub Actions kurz die Logs pruefen.

Nutzung:
  python fetch_sperren.py
"""
import os

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
API_KEY = os.environ["API_FOOTBALL_KEY"]
BUNDESLIGA_LEAGUE_ID = 78

HEADERS = {"x-apisports-key": API_KEY}
BASE = "https://v3.football.api-sports.io"

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def _spieler_id(name: str):
    res = sb.table("spieler").select("id").ilike("name", f"%{name}%").limit(1).execute()
    return res.data[0]["id"] if res.data else None


def hole_sperren_fuer_team(api_team_id: int, saison: str):
    r = requests.get(
        f"{BASE}/injuries",
        headers=HEADERS,
        params={"league": BUNDESLIGA_LEAGUE_ID, "season": saison, "team": api_team_id},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    einträge = data.get("response", [])
    if einträge and "player" not in einträge[0]:
        print(f"  Unerwartete Antwortstruktur fuer team={api_team_id}: {einträge[0]}")
    return einträge


def main():
    saison = "2026"
    teams = sb.table("teams").select("id,name,api_football_id").execute().data

    gefunden = 0
    for t in teams:
        if not t.get("api_football_id"):
            continue
        einträge = hole_sperren_fuer_team(t["api_football_id"], saison)
        for e in einträge:
            player = e.get("player", {})
            name = player.get("name")
            grund = player.get("reason") or player.get("type") or "unbekannt"
            if not name:
                continue
            # nur echte Sperren, keine Verletzungen (Verletzungen laufen schon
            # separat ueber die Aufstellungs-/Kader-Logik mit)
            if "suspend" not in grund.lower() and "sperr" not in grund.lower():
                continue
            spieler_id = _spieler_id(name)
            if not spieler_id:
                print(f"  Kein Spieler-Match fuer '{name}' ({t['name']})")
                continue
            sb.table("sperren").upsert({
                "spieler_id": spieler_id,
                "team_id": t["id"],
                "grund": grund,
            }, on_conflict="spieler_id").execute()
            gefunden += 1
            print(f"  {name} ({t['name']}): {grund}")

    print(f"{gefunden} Sperren eingetragen/aktualisiert.")


if __name__ == "__main__":
    main()
