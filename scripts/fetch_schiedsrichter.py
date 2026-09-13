"""
Schiedsrichter-Ansetzungen automatisch von dfb.de/mehr-fussball/schiris
abgreifen. Der DFB veroeffentlicht dort laufend Artikel im festen Muster
"<Name> pfeift/leitet ... die (Bundesliga-)Partie ... zwischen <Team A> und
<Team B>", typischerweise ca. 3 Tage vor dem jeweiligen Spieltag. Diese Seite
hat keinen Bot-Schutz und ist per einfachem GET lesbar.

Wir extrahieren Schiedsrichter-Name + beide Teams aus jedem Artikel-Snippet
auf der Feed-Seite und tragen sie beim passenden offenen Spiel ein
(status=geplant, schiedsrichter noch leer). 2. Liga/Frauen-Artikel werden
ignoriert (wir matchen nur gegen unsere eigene bl1-Spieltabelle, daher
laufen Fehltreffer ohnehin ins Leere).

Nutzung:
  python fetch_schiedsrichter.py
"""
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

FEED_URL = "https://www.dfb.de/mehr-fussball/schiris"

PRAEFIX_MUSTER = [
    r"^1\.\s*fc\s*", r"^1\.\s*fsv\s*", r"^fc\s*", r"^sv\s*", r"^sc\s*",
    r"^tsg\s*", r"^vfb\s*", r"^vfl\s*", r"^rb\s*", r"^bayer\s*(04)?\s*",
    r"^borussia\s*", r"^eintracht\s*", r"^1\.\s*",
]


def normalisiere(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = name.lower().strip()
    for muster in PRAEFIX_MUSTER:
        name = re.sub(muster, "", name)
    return re.sub(r"[^a-z0-9]", "", name)


def team_id_karte() -> dict:
    teams = sb.table("teams").select("id,name").execute().data
    return {normalisiere(t["name"]): t["id"] for t in teams}


def finde_team_id(name: str, karte: dict):
    key = normalisiere(name)
    if key in karte:
        return karte[key]
    for norm_name, tid in karte.items():
        if key and (key in norm_name or norm_name in key):
            return tid
    return None


# Beispieltext: "... leitet heute (ab 20.30 Uhr, live bei DAZN) die
# Bundesligapartie des 22. Spieltags zwischen dem 1. FSV Mainz 05 und
# Borussia Moenchengladbach." -- Name steht davor als eigener Satzteil.
ARTIKEL_MUSTER = re.compile(
    r"(?P<name>[A-ZÄÖÜ][\wÄÖÜäöüß.\-]+\s+[A-ZÄÖÜ][\wÄÖÜäöüß.\-]+)\s+"
    r"(?:pfeift|leitet)\s+[^.]{0,140}?"
    r"zwischen\s+(?:dem\s+|der\s+)?(?P<teamA>[\wÄÖÜäöüß0-9.\- ]+?)\s+(?:und|gegen)\s+(?:dem\s+|der\s+)?(?P<teamB>[\wÄÖÜäöüß0-9.\- ]+?)[\.\)]",
    re.UNICODE,
)

NAME_PRAEFIXE_MUSTER = re.compile(
    r"^(Schiedsrichter(in)?|DFB\-Schiedsrichter(in)?|FIFA\-Schiedsrichter(in)?)\s+",
    re.UNICODE,
)


def bereinige_name(name: str) -> str:
    return NAME_PRAEFIXE_MUSTER.sub("", name).strip()


def hole_ansetzungen():
    headers = {"User-Agent": "Mozilla/5.0 (compatible; bundesliga-vorhersage/1.0)"}
    r = requests.get(FEED_URL, headers=headers, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    treffer = []
    AUSSCHLUSS = ("Frauen", "Juniorinnen", "2. Bundesliga", "3. Liga", "U 21", "U21")
    for m in ARTIKEL_MUSTER.finditer(text):
        kontext = text[max(0, m.start() - 150):m.end()]
        if any(w in kontext for w in AUSSCHLUSS):
            continue
        name = bereinige_name(m.group("name").strip())
        teamA = m.group("teamA").strip()
        teamB = m.group("teamB").strip()
        if "Bundesliga" not in name:  # Rauschen aus Kategorie-Labels vermeiden
            treffer.append((name, teamA, teamB))
    return treffer


def main():
    karte = team_id_karte()
    treffer = hole_ansetzungen()
    print(f"{len(treffer)} Ansetzungs-Kandidaten auf der Feed-Seite gefunden.")

    jetzt = datetime.now(timezone.utc)
    bis = jetzt + timedelta(days=10)

    aktualisiert = 0
    for name, teamA, teamB in treffer:
        idA = finde_team_id(teamA, karte)
        idB = finde_team_id(teamB, karte)
        if idA is None or idB is None:
            continue  # keine bl1-Paarung (2. Liga/Frauen/Pokal) -> ignorieren

        spiel = (
            sb.table("spiele")
            .select("id,schiedsrichter,anstoss")
            .eq("liga", "bl1")
            .eq("status", "geplant")
            .gte("anstoss", jetzt.isoformat())
            .lte("anstoss", bis.isoformat())
            .or_(f"and(heim_team_id.eq.{idA},gast_team_id.eq.{idB}),and(heim_team_id.eq.{idB},gast_team_id.eq.{idA})")
            .execute()
            .data
        )
        for s in spiel:
            if s.get("schiedsrichter"):
                continue
            sb.table("spiele").update({"schiedsrichter": name}).eq("id", s["id"]).execute()
            aktualisiert += 1
            print(f"  Spiel {s['id']} ({teamA} - {teamB}): Schiedsrichter {name} eingetragen.")

    print(f"{aktualisiert} Spiele aktualisiert.")


if __name__ == "__main__":
    main()
