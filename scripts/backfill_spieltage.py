"""
Rueckwirkendes Nachrechnen von Vorhersagen fuer bereits gespielte Spieltage,
die vor Start dieser Pipeline stattfanden (hier: Spieltag 1+2 der Saison 2026).

Wichtig: es wird bewusst NICHT der heutige Datenstand fuer die Team-Staerke
verwendet, sondern der Stand zum Anstoss des jeweiligen Spieltags -- sonst
wuerde die "Vorhersage" faelschlich schon das Ergebnis spaeterer Spieltage
kennen. Kaderwert ist eine Ausnahme (teams.kaderwert_euro ist nicht
historisiert, es fliesst der aktuelle Wert ein -- kleine Ungenauigkeit,
da sich Kader zwischen Spieltag 1 und heute kaum aendern).

Nutzung:
  python backfill_spieltage.py 1 2
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


def backfill(spieltage, saison="2026"):
    for spieltag in spieltage:
        spiele = (
            sb.table("spiele")
            .select("id, heim_team_id, gast_team_id, anstoss")
            .eq("saison", saison)
            .eq("liga", "bl1")
            .eq("spieltag", spieltag)
            .execute()
            .data
        )
        if not spiele:
            print(f"Spieltag {spieltag}: keine Spiele gefunden, uebersprungen.")
            continue

        bis_datum = min(datetime.fromisoformat(s["anstoss"].replace("Z", "+00:00")) for s in spiele)
        print(f"--- Spieltag {spieltag}: Datenstand {bis_datum.isoformat()} ---")

        staerken, liga_avg, form = liga_kennzahlen_zeitgewichtet(bis_datum)
        konzentration = liga_konzentration(bis_datum)

        for spiel in spiele:
            heim, gast = spiel["heim_team_id"], spiel["gast_team_id"]
            h2h = h2h_erwartung(heim, gast, saison)

            if heim not in staerken or gast not in staerken:
                print(f"  Spiel {spiel['id']}: nicht genug Historie, uebersprungen.")
                continue

            angriff_heim = staerken[heim]["angriff"]
            abwehr_heim = staerken[heim]["abwehr"]
            angriff_gast = staerken[gast]["angriff"]
            abwehr_gast = staerken[gast]["abwehr"]

            fehlt_heim = fehlende_stammspieler(spiel["id"], heim, saison)
            fehlt_gast = fehlende_stammspieler(spiel["id"], gast, saison)
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
                "spiel_id": spiel["id"],
                "modell_version": MODELL_VERSION + "_backfill",
                "erwartete_tore_heim": round(erw_heim, 2),
                "erwartete_tore_gast": round(erw_gast, 2),
                "wahrscheinlichkeit_heimsieg": round(p_heim, 4),
                "wahrscheinlichkeit_unentschieden": round(p_x, 4),
                "wahrscheinlichkeit_gastsieg": round(p_gast, 4),
                "tipp_tore_heim": tipp_h,
                "tipp_tore_gast": tipp_g,
                "basis_aufstellung": fehlt_heim + fehlt_gast > 0,
            }, on_conflict="spiel_id").execute()
            print(f"  Spiel {spiel['id']}: Tipp {tipp_h}:{tipp_g} (H {p_heim:.0%} / U {p_x:.0%} / A {p_gast:.0%})")


if __name__ == "__main__":
    spieltage = [int(x) for x in sys.argv[1:]] or [1, 2]
    backfill(spieltage)
