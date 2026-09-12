"""
Poisson-Modell fuer die naechsten anstehenden Spiele.

Kernidee je Begegnung:
1. Direkter Vergleich (Head-to-Head) der letzten 5 Jahre zwischen genau diesen
   beiden Teams wird ermittelt und zu 50% in die Tor-Erwartung eingerechnet.
   Deckt 1. Bundesliga sowie Duelle in 2./3. Liga ab, sofern beide Teams
   aktuell in der 1. Liga spielen (z.B. wenn ein Team zwischenzeitlich
   abgestiegen war). Vollstaendige 2./3.-Liga-Historie fuer alle jemals
   dort aktiven Teams ist nicht importiert -- nur die fuer aktuelle
   Erstliga-Paarungen relevanten Duelle.
2. Team-Staerke (Angriff/Abwehr) wird nicht mehr starr zwischen "2-Saison-
   Schnitt" und "nur aktuelle Saison" umgeschaltet, sondern durchgehend
   nach Dixon-Coles (1997) zeitgewichtet: jedes Spiel geht mit dem Gewicht
   exp(-XI * Tage_seit_Anpfiff) in die Staerke-Berechnung ein. XI=0.0065
   pro halber Woche (Originalwert aus dem Paper) heisst: ein Spiel von vor
   107 Tagen zaehlt nur noch halb so viel wie eines von gestern. Das bildet
   Kaderwechsel, Trainerwechsel und Formschwankungen automatisch ab, ohne
   dass man dafuer eine harte Saison-Grenze ziehen muss.
3. Fehlende Stammspieler (laut Aufstellung, gemessen an Torbeteiligung der
   laufenden Saison) werten die Angriffs-/Abwehrstaerke leicht ab.
4. Wo Expected-Goals (xG) von Understat vorliegen (Spalten xg_heim/xg_gast),
   fliessen sie zu 50% in die Tor-Rate pro Spiel mit ein (sowohl in die
   allgemeine Team-Staerke als auch in den H2H-Vergleich). xG glaettet
   Zufallsspitzen (ein abgefaelschter Distanzschuss zaehlt torstatistisch wie
   ein Elfmeter) und macht die Einschaetzung bei kleinen Stichproben robuster.
5. Der Gesamt-Kaderwert (Transfermarkt-Basis, teams.kaderwert_euro) fliesst
   als zusaetzlicher, logarithmisch skalierter Faktor in die Angriffsstaerke
   ein -- der teurere Kader gewinnt historisch ueberproportional oft
   (siehe FC Bayern), daher wird das explizit mitgewichtet.
6. Dixon-Coles Tau-Korrektur: die unabhaengige Poisson-Annahme unterschaetzt
   systematisch knappe/torarme Ergebnisse (0:0, 1:0, 0:1, 1:1). Ein kleiner
   Korrekturfaktor (RHO, Literaturwert ca. -0.13) gleicht das direkt in der
   Ergebnis-Wahrscheinlichkeitsmatrix aus.

Nutzung:
  python model_poisson.py --tage-voraus 3
"""
import argparse
import math
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
H2H_JAHRE = 5  # Betrachtungszeitraum fuer den direkten Vergleich
H2H_GEWICHT = 0.5  # Anteil des direkten Vergleichs an der Tor-Erwartung
KADERWERT_GEWICHT = 0.15  # Einfluss der Kaderwert-Differenz auf die Angriffsstaerke
XI = 0.0065 / 3.5  # Dixon-Coles Zeitgewichtung, umgerechnet auf Tage (Original: pro Halbwoche)
RHO = -0.13  # Dixon-Coles Tau-Korrektur fuer knappe Ergebnisse (Literaturwert)
STAERKE_JAHRE = 3  # wie weit zurueck ueberhaupt Spiele geladen werden, bevor XI sie ausblendet
MODELL_VERSION = "poisson_v5_dixoncoles"

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def liga_kennzahlen_zeitgewichtet(bis_datum: datetime):
    """
    Ersetzt die alte starre "2-Saison-Schnitt vs. aktuelle Saison"-Umschaltung
    durch eine durchgehende Dixon-Coles-Zeitgewichtung: jedes Spiel zaehlt mit
    exp(-XI * Tage_seit_Anpfiff), also verblasst der Einfluss eines Spiels
    kontinuierlich statt abrupt an einer Saison-Grenze zu kippen.
    """
    ab_datum = bis_datum - timedelta(days=365 * STAERKE_JAHRE)
    spiele = (
        sb.table("spiele")
        .select("heim_team_id, gast_team_id, tore_heim, tore_gast, xg_heim, xg_gast, anstoss")
        .eq("liga", "bl1")
        .gte("anstoss", ab_datum.isoformat())
        .lt("anstoss", bis_datum.isoformat())
        .not_.is_("tore_heim", "null")
        .execute()
        .data
    )

    team_stats = {}
    gesamt_tore_w, gesamt_gewicht_spiele = 0.0, 0.0

    for s in spiele:
        anstoss = datetime.fromisoformat(s["anstoss"].replace("Z", "+00:00"))
        tage_her = max((bis_datum - anstoss).days, 0)
        gewicht = math.exp(-XI * tage_her)

        eff_heim = s["tore_heim"] if s.get("xg_heim") is None else 0.5 * s["tore_heim"] + 0.5 * s["xg_heim"]
        eff_gast = s["tore_gast"] if s.get("xg_gast") is None else 0.5 * s["tore_gast"] + 0.5 * s["xg_gast"]

        gesamt_tore_w += gewicht * (eff_heim + eff_gast)
        gesamt_gewicht_spiele += gewicht

        for team_id, geschossen, kassiert in (
            (s["heim_team_id"], eff_heim, eff_gast),
            (s["gast_team_id"], eff_gast, eff_heim),
        ):
            t = team_stats.setdefault(team_id, {"gewicht": 0.0, "tore_w": 0.0, "gegentore_w": 0.0})
            t["gewicht"] += gewicht
            t["tore_w"] += gewicht * geschossen
            t["gegentore_w"] += gewicht * kassiert

    liga_avg = gesamt_tore_w / gesamt_gewicht_spiele / 2 if gesamt_gewicht_spiele else 1.3

    staerken = {}
    for team_id, t in team_stats.items():
        if t["gewicht"] <= 0:
            continue
        staerken[team_id] = {
            "angriff": (t["tore_w"] / t["gewicht"]) / liga_avg,
            "abwehr": (t["gegentore_w"] / t["gewicht"]) / liga_avg,
        }
    return staerken, liga_avg


def tau_korrektur(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    """Dixon-Coles Korrekturfaktor fuer knappe/torarme Ergebnisse (0:0, 1:0, 0:1, 1:1)."""
    if x == 0 and y == 0:
        return 1 - lam * mu * rho
    if x == 0 and y == 1:
        return 1 + lam * rho
    if x == 1 and y == 0:
        return 1 + mu * rho
    if x == 1 and y == 1:
        return 1 - rho
    return 1.0


def h2h_erwartung(heim_id: int, gast_id: int, aktuelle_saison: str):
    """
    Direkter Vergleich der letzten H2H_JAHRE zwischen genau diesen beiden Teams,
    unabhaengig davon wer damals zuhause spielte. Gibt (erw_heim, erw_gast, anzahl)
    zurueck, oder None wenn es in diesem Zeitraum keine Begegnung gab.
    Aktuell nur 1.-Liga-Daten (spiele-Tabelle) -- 2./3. Liga noch nicht importiert.
    """
    ab_saison = str(int(aktuelle_saison) - H2H_JAHRE)

    hin = (
        sb.table("spiele")
        .select("tore_heim, tore_gast, xg_heim, xg_gast, saison")
        .eq("heim_team_id", heim_id)
        .eq("gast_team_id", gast_id)
        .gte("saison", ab_saison)
        .not_.is_("tore_heim", "null")
        .execute()
        .data
    )
    rueck = (
        sb.table("spiele")
        .select("tore_heim, tore_gast, xg_heim, xg_gast, saison")
        .eq("heim_team_id", gast_id)
        .eq("gast_team_id", heim_id)
        .gte("saison", ab_saison)
        .not_.is_("tore_heim", "null")
        .execute()
        .data
    )

    anzahl = len(hin) + len(rueck)
    if anzahl == 0:
        return None

    def eff(tore, xg):
        return tore if xg is None else 0.5 * tore + 0.5 * xg

    heim_tore = sum(eff(s["tore_heim"], s.get("xg_heim")) for s in hin) + sum(
        eff(s["tore_gast"], s.get("xg_gast")) for s in rueck
    )
    gast_tore = sum(eff(s["tore_gast"], s.get("xg_gast")) for s in hin) + sum(
        eff(s["tore_heim"], s.get("xg_heim")) for s in rueck
    )
    return heim_tore / anzahl, gast_tore / anzahl, anzahl


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


def kaderwert(team_id: int) -> int | None:
    """
    Gesamt-Kaderwert des Teams in Euro (Transfermarkt-Basis, aktuell manuell
    gepflegt in teams.kaderwert_euro, solange die transfermarkt-api-Instanz
    nicht zuverlaessig laeuft). Teurere Kader gewinnen historisch deutlich
    haeufiger -- siehe z.B. FC Bayern -- daher fliesst der Wert zusaetzlich
    zu Form/H2H mit ein.
    """
    res = (
        sb.table("teams")
        .select("kaderwert_euro")
        .eq("id", team_id)
        .single()
        .execute()
        .data
    )
    return res.get("kaderwert_euro") if res else None


def kaderwert_faktoren(heim_id: int, gast_id: int) -> tuple[float, float]:
    """
    Wandelt das Verhaeltnis der Kaderwerte in zwei multiplikative Faktoren
    fuer die Angriffsstaerke um. Logarithmisch skaliert, damit ein
    Bayern-vs-Elversberg-Verhaeltnis (Faktor ~19) die Vorhersage nicht
    komplett sprengt, aber trotzdem spuerbar reinschlaegt.
    """
    mw_heim = kaderwert(heim_id)
    mw_gast = kaderwert(gast_id)
    if not mw_heim or not mw_gast:
        return 1.0, 1.0
    verhaeltnis = math.log(mw_heim / mw_gast)
    delta = KADERWERT_GEWICHT * math.tanh(verhaeltnis / 2)
    return 1 + delta, 1 - delta


def matrix_vorhersage(
    erw_heim: float, erw_gast: float, max_tore: int = 6):
    p_heim = p_unentschieden = p_gast = 0.0
    bestes_ergebnis, beste_wkeit = (0, 0), 0.0

    for h in range(max_tore + 1):
        for g in range(max_tore + 1):
            p = poisson.pmf(h, erw_heim) * poisson.pmf(g, erw_gast)
            p *= tau_korrektur(h, g, erw_heim, erw_gast, RHO)
            if p > beste_wkeit:
                beste_wkeit, bestes_ergebnis = p, (h, g)
            if h > g:
                p_heim += p
            elif h == g:
                p_unentschieden += p
            else:
                p_gast += p

    # Normierung: die Tau-Korrektur verschiebt nur torarme Ergebnisse,
    # kann die Summe aber minimal von 1 wegbewegen.
    gesamt = p_heim + p_unentschieden + p_gast
    if gesamt > 0:
        p_heim, p_unentschieden, p_gast = p_heim / gesamt, p_unentschieden / gesamt, p_gast / gesamt

    return p_heim, p_unentschieden, p_gast, bestes_ergebnis


def berechne_vorhersagen(tage_voraus: int = 3):
    aktuelle_saison = str(datetime.now().year)
    jetzt = datetime.now(timezone.utc)
    bis = jetzt + timedelta(days=tage_voraus)

    staerken, liga_avg = liga_kennzahlen_zeitgewichtet(jetzt)

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

        h2h = h2h_erwartung(heim, gast, aktuelle_saison)

        if heim not in staerken or gast not in staerken:
            print(f"Spiel {spiel['id']}: nicht genug Historie, uebersprungen.")
            continue
        basis = "zeitgewichtete Staerke (Dixon-Coles)" + (" + H2H" if h2h else "")

        angriff_heim = staerken[heim]["angriff"]
        abwehr_heim = staerken[heim]["abwehr"]
        angriff_gast = staerken[gast]["angriff"]
        abwehr_gast = staerken[gast]["abwehr"]
        avg = liga_avg

        fehlt_heim = fehlende_stammspieler(spiel["id"], heim, aktuelle_saison)
        fehlt_gast = fehlende_stammspieler(spiel["id"], gast, aktuelle_saison)
        angriff_heim *= max(0.5, 1 - PLAYER_WEIGHT * fehlt_heim)
        angriff_gast *= max(0.5, 1 - PLAYER_WEIGHT * fehlt_gast)

        kw_faktor_heim, kw_faktor_gast = kaderwert_faktoren(heim, gast)
        angriff_heim *= kw_faktor_heim
        angriff_gast *= kw_faktor_gast

        erw_heim = avg * angriff_heim * abwehr_gast * HEIMVORTEIL
        erw_gast = avg * angriff_gast * abwehr_heim

        if h2h is not None:
            h2h_heim, h2h_gast, h2h_n = h2h
            erw_heim = (1 - H2H_GEWICHT) * erw_heim + H2H_GEWICHT * h2h_heim
            erw_gast = (1 - H2H_GEWICHT) * erw_gast + H2H_GEWICHT * h2h_gast

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
        h2h_info = f", H2H: {h2h[2]} Spiele" if h2h else ""
        kw_info = f", Kaderwert-Faktor: {kw_faktor_heim:.2f}/{kw_faktor_gast:.2f}" if kw_faktor_heim != 1.0 else ""
        print(
            f"Spiel {spiel['id']}: Tipp {tipp_h}:{tipp_g} "
            f"(H {p_heim:.0%} / U {p_x:.0%} / A {p_gast:.0%}) [{basis}{h2h_info}{kw_info}]"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tage-voraus", type=int, default=3)
    args = parser.parse_args()
    berechne_vorhersagen(args.tage_voraus)
