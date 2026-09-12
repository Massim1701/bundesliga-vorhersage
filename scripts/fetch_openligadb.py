"""
Laedt Bundesliga-Teams, Spielplan und Ergebnisse von der kostenlosen,
schluessel-freien OpenLigaDB-API und schreibt sie nach Supabase.

Nutzung:
  python fetch_openligadb.py --init --seasons 2021 2022 2023 2024 2025
  python fetch_openligadb.py                 # nur aktuelle Saison aktualisieren
"""
import argparse
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
LIGA_SHORTCUT = "bl1"  # 1. Bundesliga in OpenLigaDB
BASE = "https://api.openligadb.de"

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def upsert_team(team_json: dict) -> int:
    row = {
        "openligadb_id": team_json["teamId"],
        "name": team_json["teamName"],
        "kurzname": team_json.get("shortName"),
        "logo_url": team_json.get("teamIconUrl"),
    }
    res = (
        sb.table("teams")
        .upsert(row, on_conflict="openligadb_id")
        .execute()
    )
    return res.data[0]["id"]


def team_id_cache() -> dict:
    res = sb.table("teams").select("id, openligadb_id").execute()
    return {r["openligadb_id"]: r["id"] for r in res.data}


def fetch_season(saison: str):
    url = f"{BASE}/getmatchdata/{LIGA_SHORTCUT}/{saison}"
    matches = requests.get(url, timeout=30).json()

    for m in matches:
        for side in ("team1", "team2"):
            upsert_team(m[side])

    cache = team_id_cache()

    rows = []
    for m in matches:
        ergebnis = next(
            (r for r in m.get("matchResults", []) if r.get("resultTypeID") == 2),
            None,
        )
        rows.append(
            {
                "openligadb_id": m["matchID"],
                "saison": saison,
                "spieltag": m["group"]["groupOrderID"],
                "heim_team_id": cache[m["team1"]["teamId"]],
                "gast_team_id": cache[m["team2"]["teamId"]],
                "anstoss": m["matchDateTimeUTC"],
                "status": "beendet" if m["matchIsFinished"] else "geplant",
                "tore_heim": ergebnis["pointsTeam1"] if ergebnis else None,
                "tore_gast": ergebnis["pointsTeam2"] if ergebnis else None,
            }
        )

    if rows:
        sb.table("spiele").upsert(rows, on_conflict="openligadb_id").execute()
    print(f"Saison {saison}: {len(rows)} Spiele synchronisiert.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true", help="mehrere Saisons laden")
    parser.add_argument("--seasons", nargs="*", default=[])
    args = parser.parse_args()

    if args.init and args.seasons:
        for saison in args.seasons:
            fetch_season(saison)
    else:
        aktuelle_saison = str(datetime.now(timezone.utc).year)
        fetch_season(aktuelle_saison)
