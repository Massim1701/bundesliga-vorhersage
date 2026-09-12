"""
Poisson-Modell fuer die naechsten anstehenden Spiele.

Angriffs-/Abwehrstaerke wird aus den Ergebnissen der laufenden + letzten Saison
berechnet. Falls fuer ein Spiel bereits eine Aufstellung vorliegt, werden fehlende
Stammspieler (gemessen an Torbeteiligung der letzten Saison) leicht abgewertet.

Nutzung:
  python model_poisson.py --tage-voraus 3
"""
import argparse
from datetime import datetime, timedelta, timezone
import os

from dotenv import load_dotenv
from scipy.stats import poisson
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
HEIMVORTEIL = 1.15  # grober Faktor, spaeter aus Daten ableitbar
PLAYER_WEIGHT = 0.06  # Abwertung Angriffs-/Abwehrstaerke je fehlendem Top-Scorer
MODELL_VERSION = "poisson_v1"

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def liga_kennzahlen(saisons: list[str]):
    spiele = (
        sb.table("spiele")
        .select("heim_team_id, gast_team_id, tore_heim, tore_gast")
        .in_("saison", saisons)
        .not_.is_("tore_heim", "null")
        .execute()
        .data
    )

    team_stats = {}
    gesamt_tore, gesamt_spiele = 0, 0

    for s in spiele:
        gesamt_tore += s["tore_heim"] + s["tore_gast"]
        gesamt_spiele += 1
        for team_id, geschossen, kassiert in (
            (s["heim_team_id"], s["tore_heim"], s["tore_gast"]),
            (s["gast_team_id"], s["tore_gast"], s["tore_heim"]),
        ):
            t = team_stats.setdefault(team_id, {"spiele": 0, "tore": 0, "gegentore": 0})
            t["spiele"] += 1
            t["tore"] += geschossen
            t["gegentore"] += kassiert

    liga_avg = gesamt_tore / gesamt_spiele / 2 if gesamt_spiele else 1.3

    staerken = {}
    for team_id, t in team_stats.items():
        staerken[team_id] = {
            "angriff": (t["tore"] / t["spiele"]) / liga_avg,
            "abwehr": (t["gegentore"] / t["spiele"]) / liga_avg,
        }
    return staerken, liga_avg


def top_scorer_ids(team_id: int, saison: str, n: int = 3) -> set[int]:
    res = (
        sb.table("spieler")
        .select("id, spieler_statistiken(tore, saison)")
        .eq("team_id", team_id)
        .execute()
        .data
    )
    scored = []
    for spieler in res:
        for stat in spieler.get("spieler_statistiken", []):
            if stat["saison"] == saison:
                scored.append((spieler["id"], stat["tore"]))
    scored.sort(key=lambda x: x[1], reverse=True)
    return {sid for sid, _ in scored[:n]}


def fehlende_stammspieler(spiel_id: int, team_id: int, saison: str) -> int:
    aufstellung = (
        sb.table("aufstellungen")
        .select("spieler_id")
        .eq("spiel_id", spiel_id)
        .eq("team_id", team_id)
        .execute()
        .data
    )
    if not aufstellung:
        return 0  # noch keine Aufstellung da -> keine Abwertung moeglich
    im_kader = {a["spieler_id"] for a in aufstellung}
    top = top_scorer_ids(team_id, saison)
    return len(top - im_kader)


def matrix_vorhersage(erw_heim: float, erw_gast: float, max_tore: int = 6):
    p_heim = p_unentschieden = p_gast = 0.0
    bestes_ergebnis, beste_wkeit = (0, 0), 0.0

    for h in range(max_tore + 1):
        for g in range(max_tore + 1):
            p = poisson.pmf(h, erw_heim) * poisson.pmf(g, erw_gast)
            if p > beste_wkeit:
                beste_wkeit, bestes_ergebnis = p, (h, g)
            if h > g:
                p_heim += p
            elif h == g:
                p_unentschieden += p
            else:
                p_gast += p

    return p_heim, p_unentschieden, p_gast, bestes_ergebnis


def berechne_vorhersagen(tage_voraus: int = 3):
    aktuelle_saison = str(datetime.now().year)
    vorjahr = str(int(aktuelle_saison) - 1)
    staerken, liga_avg = liga_kennzahlen([vorjahr, aktuelle_saison])

    jetzt = datetime.now(timezone.utc)
    bis = jetzt + timedelta(days=tage_voraus)

    spiele = (
        sb.table("spiele")
        .select("id, heim_team_id, gast_team_id, anstoss")
        .gte("anstoss", jetzt.isoformat())
        .lte("anstoss", bis.isoformat())
        .eq("status", "geplant")
        .execute()
        .data
    )

    for spiel in spiele:
        heim, gast = spiel["heim_team_id"], spiel["gast_team_id"]
        if heim not in staerken or gast not in staerken:
            print(f"Spiel {spiel['id']}: nicht genug Historie, uebersprungen.")
            continue

        angriff_heim = staerken[heim]["angriff"]
        abwehr_heim = staerken[heim]["abwehr"]
        angriff_gast = staerken[gast]["angriff"]
        abwehr_gast = staerken[gast]["abwehr"]

        fehlt_heim = fehlende_stammspieler(spiel["id"], heim, aktuelle_saison)
        fehlt_gast = fehlende_stammspieler(spiel["id"], gast, aktuelle_saison)
        angriff_heim *= max(0.5, 1 - PLAYER_WEIGHT * fehlt_heim)
        angriff_gast *= max(0.5, 1 - PLAYER_WEIGHT * fehlt_gast)

        erw_heim = liga_avg * angriff_heim * abwehr_gast * HEIMVORTEIL
        erw_gast = liga_avg * angriff_gast * abwehr_heim

        p_heim, p_x, p_gast, (tipp_h, tipp_g) = matrix_vorhersage(erw_heim, erw_gast)

        sb.table("vorhersagen").upsert(
            {
                "spiel_id": spiel["id"],
                "modell_version": MODELL_VERSION,
                "erwartete_tore_heim": round(erw_heim, 2),
                "erwartete_tore_gast": round(erw_gast, 2),
                "wahrscheinlichkeit_heimsieg": round(p_heim, 4),
                "wahrscheinlichkeit_unentschieden": round(p_x, 4),
                "wahrscheinlichkeit_gastsieg": round(p_gast, 4),
                "tipp_tore_heim": tipp_h,
                "tipp_tore_gast": tipp_g,
                "basis_aufstellung": fehlt_heim + fehlt_gast > 0,
            },
            on_conflict="spiel_id",
        ).execute()
        print(
            f"Spiel {spiel['id']}: Tipp {tipp_h}:{tipp_g} "
            f"(H {p_heim:.0%} / U {p_x:.0%} / A {p_gast:.0%})"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tage-voraus", type=int, default=3)
    args = parser.parse_args()
    berechne_vorhersagen(args.tage_voraus)
