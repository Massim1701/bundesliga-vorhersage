"""
Fehleranalyse statt nur Trefferquote: schaut sich alle falschen Tendenz-Tipps
an und sucht nach wiederkehrenden Mustern, damit wir aus Fehlern lernen statt
nur zu zaehlen, wie oft wir daneben lagen.

Erkannte Muster:
  1. Remis-Unterschaetzung: Spiel endete unentschieden, wir hatten Heim-
     oder Auswaertssieg favorisiert.
  2. Knapper Fall: die drei Wahrscheinlichkeiten lagen nah beieinander
     (Top-Wert minus Zweit-Wert < KNAPP_SCHWELLE) -- der Tipp war
     letztlich ein Muenzwurf, unabhaengig vom Ausgang.
  3. Heimvorteil-Verfehlung: wir hatten Auswaertssieg favorisiert, das
     Heimteam hat aber gewonnen.
  4. Auswaerts-Ueberraschung: wir hatten Heimsieg favorisiert, das
     Auswaertsteam hat aber deutlich (Tordifferenz >= 2) gewonnen.
  5. Aufsteiger-Beteiligung: eines der Teams ohne/mit wenig BL1-Historie
     (bl2-Fallback) war an dem Fehltipp beteiligt.

Nutzung:
  python fehleranalyse.py --saison 2026
"""
import argparse
import os

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

KNAPP_SCHWELLE = 0.08  # Unterschied Top-Wert zu Zweit-Wert, ab dem ein Tipp als "knapp" gilt


AUFSTEIGER_NAMEN = ("Elversberg", "Schalke", "Paderborn")


def tendenz(p_h, p_u, p_g):
    werte = {"H": p_h, "U": p_u, "G": p_g}
    return max(werte, key=werte.get)


def analysiere(saison: str):
    rows = (
        sb.table("spiele")
        .select(
            "spieltag, tore_heim, tore_gast, "
            "heim:teams!spiele_heim_team_id_fkey(name), "
            "gast:teams!spiele_gast_team_id_fkey(name), "
            "vorhersagen(wahrscheinlichkeit_heimsieg,wahrscheinlichkeit_unentschieden,wahrscheinlichkeit_gastsieg,modell_version)"
        )
        .eq("saison", saison)
        .eq("liga", "bl1")
        .eq("status", "beendet")
        .not_.is_("tore_heim", "null")
        .execute()
        .data
    )

    fehltipps = []
    n_gesamt = 0
    for r in rows:
        v = r["vorhersagen"][0] if isinstance(r["vorhersagen"], list) else r["vorhersagen"]
        if not v:
            continue
        n_gesamt += 1
        p_h, p_u, p_g = v["wahrscheinlichkeit_heimsieg"], v["wahrscheinlichkeit_unentschieden"], v["wahrscheinlichkeit_gastsieg"]
        vorhergesagt = tendenz(p_h, p_u, p_g)
        diff = r["tore_heim"] - r["tore_gast"]
        echt = "H" if diff > 0 else "G" if diff < 0 else "U"
        if vorhergesagt == echt:
            continue

        werte_sortiert = sorted([p_h, p_u, p_g], reverse=True)
        knapp = (werte_sortiert[0] - werte_sortiert[1]) < KNAPP_SCHWELLE
        aufsteiger_beteiligt = any(n in r["heim"]["name"] or n in r["gast"]["name"] for n in AUFSTEIGER_NAMEN)

        fehltipps.append({
            "spieltag": r["spieltag"],
            "heim": r["heim"]["name"], "gast": r["gast"]["name"],
            "tore_heim": r["tore_heim"], "tore_gast": r["tore_gast"],
            "vorhergesagt": vorhergesagt, "echt": echt,
            "p_h": p_h, "p_u": p_u, "p_g": p_g,
            "knapp": knapp, "aufsteiger": aufsteiger_beteiligt,
            "remis_verpasst": echt == "U" and vorhergesagt != "U",
            "heimvorteil_verfehlt": vorhergesagt == "G" and echt == "H",
            "auswaerts_ueberraschung": vorhergesagt == "H" and echt == "G" and abs(diff) >= 2,
        })

    if not fehltipps:
        print("Keine Fehltipps in diesem Zeitraum -- entweder perfekt oder noch zu wenig Daten.")
        return

    n_falsch = len(fehltipps)
    print(f"Saison {saison}: {n_falsch} von {n_gesamt} Tendenz-Tipps falsch ({n_falsch / n_gesamt:.1%})\n")

    remis = sum(1 for f in fehltipps if f["remis_verpasst"])
    knapp = sum(1 for f in fehltipps if f["knapp"])
    heimvorteil = sum(1 for f in fehltipps if f["heimvorteil_verfehlt"])
    auswaerts = sum(1 for f in fehltipps if f["auswaerts_ueberraschung"])
    aufsteiger = sum(1 for f in fehltipps if f["aufsteiger"])

    print("Muster unter den Fehltipps:")
    print(f"  Remis nicht erkannt:         {remis}/{n_falsch}")
    print(f"  Knapper Fall (Muenzwurf):    {knapp}/{n_falsch}")
    print(f"  Heimvorteil verfehlt:        {heimvorteil}/{n_falsch}  (Auswaertssieg getippt, Heim gewinnt)")
    print(f"  Auswaerts-Ueberraschung:     {auswaerts}/{n_falsch}  (Heimsieg getippt, Gast gewinnt deutlich)")
    print(f"  Aufsteiger/bl2-Fallback:     {aufsteiger}/{n_falsch}")
    print()

    print("Einzelne Fehltipps:")
    for f in fehltipps:
        marker = []
        if f["remis_verpasst"]:
            marker.append("Remis")
        if f["knapp"]:
            marker.append("knapp")
        if f["heimvorteil_verfehlt"]:
            marker.append("Heimvorteil")
        if f["auswaerts_ueberraschung"]:
            marker.append("Auswaerts-Ueberraschung")
        if f["aufsteiger"]:
            marker.append("Aufsteiger")
        print(
            f"  ST{f['spieltag']:>2}  {f['heim']:<22} {f['tore_heim']}:{f['tore_gast']} {f['gast']:<22} "
            f"(H {f['p_h']:.0%}/U {f['p_u']:.0%}/A {f['p_g']:.0%})  [{', '.join(marker) or '-'}]"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--saison", required=True)
    args = parser.parse_args()
    analysiere(args.saison)
