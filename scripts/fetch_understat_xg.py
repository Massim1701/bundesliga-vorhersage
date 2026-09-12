"""
Holt xG-Daten (Expected Goals) von understat.com per Headless-Browser.

Understat laedt seine Daten (window.datesData, window.teamsData) per JavaScript
nach -- ein einfacher HTTP-Request sieht sie nicht. Deshalb rendert dieses
Skript die Seite per Playwright/Chromium und liest die Werte direkt aus dem
window-Objekt.

Matching-Logik: Understat-Team-Namen unterscheiden sich von unseren
OpenLigaDB-Namen ("Bayern Munich" vs. "FC Bayern München"), daher feste
Zuordnungstabelle unten. Ein Match wird ueber (saison, heim_team_id,
gast_team_id) gefunden -- in einer Einfachrunden-Liga eindeutig, da jedes
Team-Paar pro Saison genau einmal in dieser Heim/Gast-Konstellation spielt.

Nutzung:
  python fetch_understat_xg.py --saison 2026
"""
import argparse
import os
import time

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# Understat-Teamname -> unsere interne teams.id (aktuelle 18 Bundesliga-Teams)
TEAM_MAPPING = {
    "Bayern Munich": 2,
    "VfB Stuttgart": 3,
    "RasenBallsport Leipzig": 4,
    "Borussia M.Gladbach": 5,
    "Mainz 05": 6,
    "Paderborn": 7,
    "Union Berlin": 8,
    "Eintracht Frankfurt": 9,
    "FC Cologne": 10,
    "Hoffenheim": 11,
    "Elversberg": 12,
    "Bayer Leverkusen": 13,
    "Borussia Dortmund": 14,
    "Hamburger SV": 15,
    "Freiburg": 16,
    "Werder Bremen": 17,
    "Augsburg": 18,
    "Schalke 04": 19,
}


def hole_dates_data(saison: str) -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ))
        page.goto(f"https://understat.com/league/Bundesliga/{saison}", timeout=30000)
        page.wait_for_timeout(1500)  # kurz warten, bis window.datesData gesetzt ist
        dates_data = page.evaluate("Object.values(window.datesData || {})")
        browser.close()
        return dates_data


def sync_xg(saison: str):
    matches = hole_dates_data(saison)
    aktualisiert, uebersprungen = 0, 0

    for m in matches:
        if not m.get("isResult"):
            continue  # Spiel noch nicht gespielt -> kein xG vorhanden

        heim_name = m["h"]["title"]
        gast_name = m["a"]["title"]
        heim_id = TEAM_MAPPING.get(heim_name)
        gast_id = TEAM_MAPPING.get(gast_name)
        if heim_id is None or gast_id is None:
            uebersprungen += 1
            continue

        xg_heim = round(float(m["xG"]["h"]), 2)
        xg_gast = round(float(m["xG"]["a"]), 2)

        res = (
            sb.table("spiele")
            .update({"xg_heim": xg_heim, "xg_gast": xg_gast})
            .eq("saison", saison)
            .eq("liga", "bl1")
            .eq("heim_team_id", heim_id)
            .eq("gast_team_id", gast_id)
            .execute()
        )
        if res.data:
            aktualisiert += 1
        else:
            uebersprungen += 1

    print(f"Saison {saison}: {aktualisiert} Spiele mit xG aktualisiert, {uebersprungen} uebersprungen.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--saison", required=True, help="Start-Jahr der Saison, z.B. 2026")
    args = parser.parse_args()
    sync_xg(args.saison)
