"""
Fallback fuer Teams ohne bl1-Historie im Betrachtungszeitraum (frisch
aufgestiegen oder zu lange raus): Staerke wird aus der kompletten
bl2/2025-Vorsaison abgeleitet (34 Spiele), relativ zum bl2-eigenen
Liga-Schnitt -- dieselbe Grundidee wie schon in der historie_5jahre-View
fuer Aufsteiger verwendet.

Nutzung:
  python backfill_mit_bl2_fallback.py <spiel_id> <heim_id> <gast_id>
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
from model_poisson import (
    sb, MODELL_VERSION,
    liga_kennzahlen_zeitgewichtet, liga_konzentration,
    h2h_erwartung, fehlende_stammspieler, kaderwert_faktoren,
    formkurve_faktoren, matrix_vorhersage, HEIMVORTEIL, PLAYER_WEIGHT,
    H2H_GEWICHT,
)


def bl2_fallback_staerke(team_id: int):
    heim = sb.table("spiele").select("tore_heim,tore_gast").eq("liga", "bl2").eq("saison", "2025") \
        .eq("heim_team_id", team_id).not_.is_("tore_heim", "null").execute().data
    gast = sb.table("spiele").select("tore_heim,tore_gast").eq("liga", "bl2").eq("saison", "2025") \
        .eq("gast_team_id", team_id).not_.is_("tore_gast", "null").execute().data
    alle = sb.table("spiele").select("tore_heim,tore_gast").eq("liga", "bl2").eq("saison", "2025") \
        .not_.is_("tore_heim", "null").execute().data

    bl2_avg = sum(s["tore_heim"] + s["tore_gast"] for s in alle) / len(alle) / 2
    tore = sum(s["tore_heim"] for s in heim) + sum(s["tore_gast"] for s in gast)
    gegentore = sum(s["tore_gast"] for s in heim) + sum(s["tore_heim"] for s in gast)
    n = len(heim) + len(gast)
    return {"angriff": (tore / n) / bl2_avg, "abwehr": (gegentore / n) / bl2_avg}


def berechne_einzelspiel(spiel_id, heim, gast, bis_datum, saison="2026"):
    staerken, liga_avg, form = liga_kennzahlen_zeitgewichtet(bis_datum)
    konzentration = liga_konzentration(bis_datum)

    for tid in (heim, gast):
        if tid not in staerken:
            staerken[tid] = bl2_fallback_staerke(tid)
            print(f"  Team {tid}: bl2-Fallback-Staerke {staerken[tid]}")

    h2h = h2h_erwartung(heim, gast, saison)

    angriff_heim = staerken[heim]["angriff"]
    abwehr_heim = staerken[heim]["abwehr"]
    angriff_gast = staerken[gast]["angriff"]
    abwehr_gast = staerken[gast]["abwehr"]

    fehlt_heim = fehlende_stammspieler(spiel_id, heim, saison)
    fehlt_gast = fehlende_stammspieler(spiel_id, gast, saison)
    angriff_heim *= max(0.5, 1 - PLAYER_WEIGHT * fehlt_heim)
    angriff_gast *= max(0.5, 1 - PLAYER_WEIGHT * fehlt_gast)

    kw_faktor_heim, kw_faktor_gast = kaderwert_faktoren(heim, gast)
    angriff_heim *= kw_faktor_heim
    angriff_gast *= kw_faktor_gast

    fk_angriff_heim, fk_abwehr_heim = formkurve_faktoren(heim, staerken, form)
    fk_angriff_gast, fk_abwehr_gast = formkurve_faktoren(gast, staerken, form)
    angriff_heim *= fk_angriff_heim
    angriff_gast *= fk_angriff_gast
    abwehr_heim *= fk_abwehr_heim
    abwehr_gast *= fk_abwehr_gast

    kz_angriff_heim, kz_abwehr_heim = konzentration.get(heim, (1.0, 1.0))
    kz_angriff_gast, kz_abwehr_gast = konzentration.get(gast, (1.0, 1.0))
    angriff_heim *= kz_angriff_heim
    angriff_gast *= kz_angriff_gast
    abwehr_heim *= kz_abwehr_heim
    abwehr_gast *= kz_abwehr_gast

    erw_heim = liga_avg * angriff_heim * abwehr_gast * HEIMVORTEIL
    erw_gast = liga_avg * angriff_gast * abwehr_heim

    if h2h is not None:
        h2h_heim, h2h_gast, _ = h2h
        erw_heim = (1 - H2H_GEWICHT) * erw_heim + H2H_GEWICHT * h2h_heim
        erw_gast = (1 - H2H_GEWICHT) * erw_gast + H2H_GEWICHT * h2h_gast

    p_heim, p_x, p_gast, (tipp_h, tipp_g) = matrix_vorhersage(erw_heim, erw_gast)

    sb.table("vorhersagen").upsert({
        "spiel_id": spiel_id,
        "modell_version": MODELL_VERSION + "_backfill_bl2fallback",
        "erwartete_tore_heim": round(erw_heim, 2),
        "erwartete_tore_gast": round(erw_gast, 2),
        "wahrscheinlichkeit_heimsieg": round(p_heim, 4),
        "wahrscheinlichkeit_unentschieden": round(p_x, 4),
        "wahrscheinlichkeit_gastsieg": round(p_gast, 4),
        "tipp_tore_heim": tipp_h,
        "tipp_tore_gast": tipp_g,
        "basis_aufstellung": fehlt_heim + fehlt_gast > 0,
    }, on_conflict="spiel_id").execute()
    print(f"  Spiel {spiel_id}: Tipp {tipp_h}:{tipp_g} (H {p_heim:.0%} / U {p_x:.0%} / A {p_gast:.0%})")


if __name__ == "__main__":
    spiel_id, heim, gast = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    bis_datum = datetime(2026, 8, 28, 18, 30, tzinfo=timezone.utc)  # Anstoss Spieltag 1
    berechne_einzelspiel(spiel_id, heim, gast, bis_datum)
