"""
Zwei Aufgaben ueber die API-Football (https://www.api-football.com/, kostenloser
Tier reicht fuer eine Liga/Tag):

1. --stats     Aktualisiert Saison-Statistiken (Tore, Vorlagen, Karten) je Spieler.
2. --lineups   Prueft anstehende Spiele der naechsten 2 Stunden und zieht,
               sobald verfuegbar, die Aufstellung (idR ~1h vor Anpfiff online).

In GitHub Actions per Cron mehrfach am Spieltag laufen lassen (siehe
.github/workflows/spieltag.yml), damit die Aufstellung zeitnah erfasst wird.
"""
import argparse
import os
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
API_KEY = os.environ["API_FOOTBALL_KEY"]
BUNDESLIGA_LEAGUE_ID = 78  # API-Football League-ID fuer die 1. Bundesliga

HEADERS = {"x-apisports-key": API_KEY}
BASE = "https://v3.football.api-sports.io"

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def _team_lookup() -> dict:
    res = sb.table("teams").select("id, name, api_football_id").execute()
    return {r["name"]: r for r in res.data}


def sync_lineups():
    now = datetime.now(timezone.utc)
    bis = now + timedelta(hours=2)

    anstehende = (
        sb.table("spiele")
        .select("id, anstoss, heim_team_id, gast_team_id, openligadb_id")
        .gte("anstoss", now.isoformat())
        .lte("anstoss", bis.isoformat())
        .eq("status", "geplant")
        .execute()
        .data
    )

    if not anstehende:
        print("Keine Spiele in den naechsten 2 Stunden.")
        return

    for spiel in anstehende:
        # API-Football-Fixture ueber Datum + Teams finden (einfache Zuordnung;
        # fuer mehr Robustheit koennte man fixture-IDs einmalig mappen und cachen)
        fixtures = requests.get(
            f"{BASE}/fixtures",
            headers=HEADERS,
            params={"league": BUNDESLIGA_LEAGUE_ID, "date": spiel["anstoss"][:10]},
            timeout=20,
        ).json()

        fixture_id = None
        for fx in fixtures.get("response", []):
            if fx["fixture"]["id"]:
                fixture_id = fx["fixture"]["id"]  # ggf. per Teamname praezisieren
                break

        if not fixture_id:
            continue

        lineups = requests.get(
            f"{BASE}/fixtures/lineups",
            headers=HEADERS,
            params={"fixture": fixture_id},
            timeout=20,
        ).json()

        if not lineups.get("response"):
            print(f"Spiel {spiel['id']}: Aufstellung noch nicht veroeffentlicht.")
            continue

        for team_lineup in lineups["response"]:
            for block, ist_startelf in (("startXI", True), ("substitutes", False)):
                for entry in team_lineup.get(block, []):
                    spieler_name = entry["player"]["name"]
                    spieler = (
                        sb.table("spieler")
                        .select("id")
                        .ilike("name", f"%{spieler_name}%")
                        .limit(1)
                        .execute()
                        .data
                    )
                    if not spieler:
                        continue
                    sb.table("aufstellungen").upsert(
                        {
                            "spiel_id": spiel["id"],
                            "spieler_id": spieler[0]["id"],
                            "startelf": ist_startelf,
                            "position": entry["player"].get("pos"),
                        },
                        on_conflict="spiel_id,spieler_id",
                    ).execute()

        print(f"Spiel {spiel['id']}: Aufstellung gespeichert.")


def sync_stats(saison: str):
    teams = _team_lookup()
    for name, team in teams.items():
        if not team["api_football_id"]:
            continue
        page = 1
        while True:
            res = requests.get(
                f"{BASE}/players",
                headers=HEADERS,
                params={"team": team["api_football_id"], "season": saison, "page": page},
                timeout=20,
            ).json()

            for entry in res.get("response", []):
                stats = entry["statistics"][0]
                sb.table("spieler_statistiken").upsert(
                    {
                        "spieler_id": _spieler_id(entry["player"]["name"]),
                        "saison": saison,
                        "einsaetze": stats["games"]["appearences"] or 0,
                        "tore": stats["goals"]["total"] or 0,
                        "vorlagen": stats["goals"]["assists"] or 0,
                        "gelbe_karten": stats["cards"]["yellow"] or 0,
                        "rote_karten": stats["cards"]["red"] or 0,
                        "minuten": stats["games"]["minutes"] or 0,
                    },
                    on_conflict="spieler_id,saison",
                ).execute()

            paging = res.get("paging", {})
            if paging.get("current", 1) >= paging.get("total", 1):
                break
            page += 1
        print(f"{name}: Spielerstatistiken {saison} aktualisiert.")


def _spieler_id(name: str):
    res = sb.table("spieler").select("id").ilike("name", f"%{name}%").limit(1).execute()
    return res.data[0]["id"] if res.data else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lineups", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--saison", default=str(datetime.now().year))
    args = parser.parse_args()

    if args.lineups:
        sync_lineups()
    if args.stats:
        sync_stats(args.saison)
