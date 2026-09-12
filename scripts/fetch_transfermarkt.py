"""
Laedt Kader, Positionen und Marktwerte je Team ueber die oeffentliche Instanz
des Open-Source-Projekts transfermarkt-api (https://github.com/felipeall/transfermarkt-api).

Transfermarkt.de selbst bietet keine offizielle API an; direktes Scraping der
Seite verstoesst gegen deren AGB. Dieses Skript nutzt daher die strukturierte,
bereits aufbereitete Zwischenschicht. Fuer Dauerbetrieb: eigenes Deployment
des Projekts erwaegen (TRANSFERMARKT_API_BASE in .env anpassen).

Nutzung:
  python fetch_transfermarkt.py --sync-kader
"""
import argparse
import os
import time

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TM_API = os.environ.get("TRANSFERMARKT_API_BASE", "https://transfermarkt-api.fly.dev")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def find_club_id(team_name: str) -> str | None:
    res = requests.get(f"{TM_API}/clubs/search/{team_name}", timeout=20).json()
    treffer = res.get("results", [])
    return treffer[0]["id"] if treffer else None


def sync_kader():
    teams = sb.table("teams").select("id, name, transfermarkt_id").execute().data

    for team in teams:
        tm_id = team["transfermarkt_id"]
        if not tm_id:
            tm_id = find_club_id(team["name"])
            if not tm_id:
                print(f"Kein Transfermarkt-Treffer fuer {team['name']}, uebersprungen.")
                continue
            sb.table("teams").update({"transfermarkt_id": tm_id}).eq(
                "id", team["id"]
            ).execute()

        time.sleep(1)  # kein aggressives Polling der oeffentlichen Instanz
        squad = requests.get(f"{TM_API}/clubs/{tm_id}/players", timeout=20).json()

        rows = []
        for p in squad.get("players", []):
            marktwert = p.get("marketValue")  # z.B. "€25.00m" oder Zahl je nach Version
            marktwert_euro = None
            if isinstance(marktwert, str) and marktwert not in ("-", ""):
                marktwert_euro = _parse_marktwert(marktwert)
            elif isinstance(marktwert, (int, float)):
                marktwert_euro = int(marktwert)

            rows.append(
                {
                    "transfermarkt_id": int(p["id"]),
                    "team_id": team["id"],
                    "name": p.get("name"),
                    "position": p.get("position"),
                    "marktwert_euro": marktwert_euro,
                    "nationalitaet": (p.get("nationality") or [None])[0],
                }
            )

        if rows:
            sb.table("spieler").upsert(rows, on_conflict="transfermarkt_id").execute()
        print(f"{team['name']}: {len(rows)} Spieler synchronisiert.")


def _parse_marktwert(text: str) -> int | None:
    text = text.replace("€", "").strip()
    multiplier = 1
    if text.endswith("m"):
        multiplier, text = 1_000_000, text[:-1]
    elif text.endswith("k"):
        multiplier, text = 1_000, text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sync-kader", action="store_true")
    args = parser.parse_args()

    if args.sync_kader:
        sync_kader()
