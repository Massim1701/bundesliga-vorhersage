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
7. Toranalyse nach Spielminute (Tabelle "tore", pro Tor mit exakter Minute
   via OpenLigaDB): Teams, die ueberproportional viele ihrer Tore/Gegentore
   erst ab der 75. Minute kassieren bzw. schiessen, zeigen ein Fitness-/
   Konzentrationsmuster -- nachlassende Physis in der Schlussphase fuehrt zu
   Abwehrfehlern, frische Beine auf der Bank koennen umgekehrt spaete Tore
   begnstigen. Die Abweichung vom Liga-Durchschnitt dieser Spaetphasen-Quote
   fliesst als zusaetzlicher Faktor in Angriffs-/Abwehrstaerke ein.

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
FORM_ANZAHL_SPIELE = 5  # "the trend is your friend": ueber wie viele juengste Spiele die Form laeuft
FORM_GEWICHT = 0.2  # wie stark die juengste Form vom langfristigen Saison-Schnitt abweichen darf
SPERRFRIST_MINUTEN = 30  # ab wann vor Anstoss keine neue Vorhersage mehr berechnet wird
TRAINERWECHSEL_GEWICHT = 0.08  # kurzzeitiger Bonus im "neuer Besen"-Fenster
TRAINERWECHSEL_FENSTER = (3, 10)  # Spiele seit Wechsel, in denen der Bonus greift (1-2 davor: neutral)
TRAINER_QUALITAET_GEWICHT = 0.05  # bewusst klein -- nur Randnotiz, Trainer schiessen keine Tore
TRAINER_QUALITAET_MIN_SPIELE = 10  # ohne genug Spiele unter diesem Trainer keine Aussage moeglich
TRAINER_QUALITAET_JAHRE = 5  # Deckelung des Betrachtungszeitraums
SCHIEDSRICHTER_GEWICHT = 0.1  # bewusst klein gehalten, generelle Tendenz nicht Team-spezifisch
SCHIEDSRICHTER_MIN_SPIELE = 15  # ohne genug eigene Spiele keine verlaessliche Aussage
LIGA_HEIMSIEG_QUOTE = 0.45  # grober Bundesliga-Erfahrungswert als Vergleichsbasis
XI = 0.0065 / 3.5  # Dixon-Coles Zeitgewichtung, umgerechnet auf Tage (Original: pro Halbwoche)
RHO = -0.13  # Dixon-Coles Tau-Korrektur fuer knappe Ergebnisse (Literaturwert)
STAERKE_JAHRE = 3  # wie weit zurueck ueberhaupt Spiele geladen werden, bevor XI sie ausblendet
SPAETPHASE_MINUTE = 75  # ab dieser Minute gilt ein Tor als "spaet" (Konzentration/Fitness-Signal)
KONZENTRATION_GEWICHT = 1.0  # Einfluss der Spaetphasen-Schwaeche auf Angriff/Abwehr
MODELL_VERSION = "poisson_v10_schiedsrichter"

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def fetch_all_paginiert(query_builder):
    """
    Supabase/PostgREST deckelt Anfragen serverseitig auf 1000 Zeilen, auch
    bei explizit gesetztem hoeherem limit. Ohne Pagination wuerden groessere
    Zeitfenster (z.B. liga_kennzahlen_zeitgewichtet, liga_konzentration)
    still und leise abgeschnitten. query_builder ist eine Funktion, die bei
    jedem Aufruf eine frische Query liefert (Supabase-Query-Objekte sind
    nur einmal nutzbar), auf die .range() angewendet wird.
    """
    alle = []
    offset = 0
    seiten_groesse = 1000
    while True:
        seite = query_builder().range(offset, offset + seiten_groesse - 1).execute().data
        alle.extend(seite)
        if len(seite) < seiten_groesse:
            break
        offset += seiten_groesse
    return alle


def liga_kennzahlen_zeitgewichtet(bis_datum: datetime):
    """
    Ersetzt die alte starre "2-Saison-Schnitt vs. aktuelle Saison"-Umschaltung
    durch eine durchgehende Dixon-Coles-Zeitgewichtung: jedes Spiel zaehlt mit
    exp(-XI * Tage_seit_Anpfiff), also verblasst der Einfluss eines Spiels
    kontinuierlich statt abrupt an einer Saison-Grenze zu kippen.
    """
    ab_datum = bis_datum - timedelta(days=365 * STAERKE_JAHRE)
    spiele = fetch_all_paginiert(lambda: (
        sb.table("spiele")
        .select("heim_team_id, gast_team_id, tore_heim, tore_gast, xg_heim, xg_gast, anstoss")
        .eq("liga", "bl1")
        .gte("anstoss", ab_datum.isoformat())
        .lt("anstoss", bis_datum.isoformat())
        .not_.is_("tore_heim", "null")
    ))

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

    # "The trend is your friend": kurzfristige Form ueber die letzten 5 Spiele
    # je Team, unabhaengig vom Gegner. Separat von der langfristigen Staerke
    # (die mit ~1 Jahr Halbwertszeit eher den Saisonschnitt glaettet als eine
    # Hoch-/Tief-Phase abzubilden).
    spiele_je_team = {}
    for s in spiele:
        anstoss = datetime.fromisoformat(s["anstoss"].replace("Z", "+00:00"))
        eff_heim = s["tore_heim"] if s.get("xg_heim") is None else 0.5 * s["tore_heim"] + 0.5 * s["xg_heim"]
        eff_gast = s["tore_gast"] if s.get("xg_gast") is None else 0.5 * s["tore_gast"] + 0.5 * s["xg_gast"]
        for team_id, geschossen, kassiert in (
            (s["heim_team_id"], eff_heim, eff_gast),
            (s["gast_team_id"], eff_gast, eff_heim),
        ):
            spiele_je_team.setdefault(team_id, []).append((anstoss, geschossen, kassiert))

    form = {}
    for team_id, liste in spiele_je_team.items():
        liste.sort(key=lambda x: x[0], reverse=True)
        letzte5 = liste[:FORM_ANZAHL_SPIELE]
        if not letzte5:
            continue
        form[team_id] = {
            "angriff": sum(g for _, g, _ in letzte5) / len(letzte5) / liga_avg,
            "abwehr": sum(k for _, _, k in letzte5) / len(letzte5) / liga_avg,
        }

    return staerken, liga_avg, form


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


def formkurve_faktoren(team_id: int, staerken: dict, form: dict) -> tuple[float, float]:
    """
    "The trend is your friend": vergleicht die Form der letzten
    FORM_ANZAHL_SPIELE Partien mit der langfristigen Staerke des Teams.
    Schiesst/kassiert ein Team zuletzt spuerbar mehr oder weniger als sein
    Saison-Schnitt, verschiebt das Angriffs- bzw. Abwehr-Staerke leicht in
    diese Richtung -- gedeckelt durch FORM_GEWICHT, damit eine kurze Serie
    das Bild nicht komplett kippt.
    """
    basis = staerken.get(team_id)
    aktuell = form.get(team_id)
    if not basis or not aktuell or basis["angriff"] <= 0 or basis["abwehr"] <= 0:
        return 1.0, 1.0
    delta_angriff = FORM_GEWICHT * (aktuell["angriff"] / basis["angriff"] - 1)
    delta_abwehr = FORM_GEWICHT * (aktuell["abwehr"] / basis["abwehr"] - 1)
    return 1 + delta_angriff, 1 + delta_abwehr


def trainerwechsel_faktor(team_id: int, bis_datum: datetime) -> tuple[float, float]:
    """
    "Neue Besen kehren gut" -- aber verzoegert: die ersten 1-2 Spiele nach
    einem Trainerwechsel braucht das neue Konzept Zeit, deshalb kein
    Sofort-Bonus. Ab Spiel TRAINERWECHSEL_FENSTER[0] bis [1] seit Wechsel
    gibt es einen kleinen Angriffs-/Abwehr-Bonus, danach ist der Trainer
    reguraer und der Effekt steckt schon in Formkurve/Staerke.
    """
    res = sb.table("teams").select("trainer_seit").eq("id", team_id).single().execute().data
    trainer_seit = res.get("trainer_seit") if res else None
    if not trainer_seit:
        return 1.0, 1.0

    seit = datetime.fromisoformat(trainer_seit).replace(tzinfo=timezone.utc)
    heim = sb.table("spiele").select("id", count="exact").eq("liga", "bl1") \
        .eq("heim_team_id", team_id).gte("anstoss", seit.isoformat()) \
        .lt("anstoss", bis_datum.isoformat()).not_.is_("tore_heim", "null").execute()
    gast = sb.table("spiele").select("id", count="exact").eq("liga", "bl1") \
        .eq("gast_team_id", team_id).gte("anstoss", seit.isoformat()) \
        .lt("anstoss", bis_datum.isoformat()).not_.is_("tore_heim", "null").execute()
    anzahl = (heim.count or 0) + (gast.count or 0)

    if TRAINERWECHSEL_FENSTER[0] <= anzahl <= TRAINERWECHSEL_FENSTER[1]:
        return 1 + TRAINERWECHSEL_GEWICHT, 1 - TRAINERWECHSEL_GEWICHT
    return 1.0, 1.0


def trainer_qualitaet_faktor(team_id: int, bis_datum: datetime) -> tuple[float, float]:
    """
    Grundsaetzliche Trainer-Qualitaet, unabhaengig davon ob er neu ist oder
    schon lange da: Punkte pro Spiel seit Amtsantritt bei DIESEM Verein,
    gedeckelt auf die letzten TRAINER_QUALITAET_JAHRE Jahre. Vereinfachung
    gegenueber einer echten Karriere-Bilanz ueber mehrere Stationen (dafuer
    fehlt uns eine Trainer-Wechsel-Datenquelle mit Vereinshistorie) -- bei
    Trainern, die schon laenger an ihrem aktuellen Klub sind, kommt es aufs
    selbe raus. Ohne genug Spiele (z.B. gerade erst uebernommen) neutral.
    """
    res = sb.table("teams").select("trainer_seit").eq("id", team_id).single().execute().data
    trainer_seit = res.get("trainer_seit") if res else None
    if not trainer_seit:
        return 1.0, 1.0

    seit = datetime.fromisoformat(trainer_seit).replace(tzinfo=timezone.utc)
    ab_datum = max(seit, bis_datum - timedelta(days=365 * TRAINER_QUALITAET_JAHRE))

    heim = sb.table("spiele").select("tore_heim,tore_gast").eq("liga", "bl1").eq("heim_team_id", team_id) \
        .gte("anstoss", ab_datum.isoformat()).lt("anstoss", bis_datum.isoformat()) \
        .not_.is_("tore_heim", "null").execute().data
    gast = sb.table("spiele").select("tore_heim,tore_gast").eq("liga", "bl1").eq("gast_team_id", team_id) \
        .gte("anstoss", ab_datum.isoformat()).lt("anstoss", bis_datum.isoformat()) \
        .not_.is_("tore_gast", "null").execute().data

    n = len(heim) + len(gast)
    if n < TRAINER_QUALITAET_MIN_SPIELE:
        return 1.0, 1.0

    punkte = 0
    for s in heim:
        punkte += 3 if s["tore_heim"] > s["tore_gast"] else 1 if s["tore_heim"] == s["tore_gast"] else 0
    for s in gast:
        punkte += 3 if s["tore_gast"] > s["tore_heim"] else 1 if s["tore_heim"] == s["tore_gast"] else 0

    ppg = punkte / n
    delta = TRAINER_QUALITAET_GEWICHT * math.tanh((ppg - 1.5) / 1.5)
    return 1 + delta, 1 - delta


def schiedsrichter_heimvorteil_faktor(schiedsrichter: str | None) -> float:
    """
    Manche Schiedsrichter pfeifen im Schnitt heim-freundlicher oder
    heim-feindlicher als der Durchschnitt (Elfmeter, Karten, Nachspielzeit).
    Bewusst NICHT "Schiri X gegen Verein Y" (Stichprobe pro Paarung viel zu
    klein, reines Zufallsrauschen), sondern die generelle Heimsieg-Quote
    ueber ALLE von ihm geleiteten Bundesliga-Spiele. Schiedsrichter-Namen
    werden aktuell manuell nachgetragen (spiele.schiedsrichter), da es keine
    kostenlose automatisierte Ansetzungs-Quelle gibt. Ohne Namen oder ohne
    genug Spiele: neutral.
    """
    if not schiedsrichter:
        return 1.0
    spiele = sb.table("spiele").select("tore_heim,tore_gast").eq("liga", "bl1") \
        .eq("schiedsrichter", schiedsrichter).not_.is_("tore_heim", "null").execute().data
    if len(spiele) < SCHIEDSRICHTER_MIN_SPIELE:
        return 1.0
    heimsiege = sum(1 for s in spiele if s["tore_heim"] > s["tore_gast"])
    quote = heimsiege / len(spiele)
    delta = SCHIEDSRICHTER_GEWICHT * math.tanh((quote - LIGA_HEIMSIEG_QUOTE) / LIGA_HEIMSIEG_QUOTE)
    return 1 + delta


def liga_konzentration(bis_datum: datetime):
    """
    Analysiert fuer jedes Team, wie viele seiner Tore/Gegentore in der
    Spaetphase (ab SPAETPHASE_MINUTE) fallen -- ein Hinweis auf Fitness und
    Konzentration in den Schlussminuten. Vergleich gegen den Liga-Schnitt
    ergibt pro Team einen Angriffs- und einen Abwehr-Korrekturfaktor.
    Nutzt dasselbe Zeitfenster wie liga_kennzahlen_zeitgewichtet.
    """
    ab_datum = bis_datum - timedelta(days=365 * STAERKE_JAHRE)
    tore = fetch_all_paginiert(lambda: (
        sb.table("tore")
        .select("team_id, minute, spiele!inner(heim_team_id, gast_team_id, anstoss, liga)")
        .gte("spiele.anstoss", ab_datum.isoformat())
        .lt("spiele.anstoss", bis_datum.isoformat())
        .eq("spiele.liga", "bl1")
    ))

    stats = {}

    def eintrag(team_id):
        return stats.setdefault(team_id, {"tore": 0, "tore_spaet": 0, "gegentore": 0, "gegentore_spaet": 0})

    for t in tore:
        spiel = t["spiele"]
        heim, gast = spiel["heim_team_id"], spiel["gast_team_id"]
        schuetze = t["team_id"]
        gegner = gast if schuetze == heim else heim
        spaet = t["minute"] is not None and t["minute"] >= SPAETPHASE_MINUTE

        s = eintrag(schuetze)
        s["tore"] += 1
        if spaet:
            s["tore_spaet"] += 1

        g = eintrag(gegner)
        g["gegentore"] += 1
        if spaet:
            g["gegentore_spaet"] += 1

    # Liga-Durchschnitt der Spaetphasen-Quoten (mindestens etwas Datenbasis pro Team gefordert)
    tore_quoten = [s["tore_spaet"] / s["tore"] for s in stats.values() if s["tore"] >= 15]
    gegentore_quoten = [s["gegentore_spaet"] / s["gegentore"] for s in stats.values() if s["gegentore"] >= 15]
    liga_tore_quote = sum(tore_quoten) / len(tore_quoten) if tore_quoten else None
    liga_gegentore_quote = sum(gegentore_quoten) / len(gegentore_quoten) if gegentore_quoten else None

    faktoren = {}
    for team_id, s in stats.items():
        angriff_faktor = 1.0
        abwehr_faktor = 1.0
        if liga_tore_quote is not None and s["tore"] >= 15:
            quote = s["tore_spaet"] / s["tore"]
            angriff_faktor = 1 + KONZENTRATION_GEWICHT * (quote - liga_tore_quote)
        if liga_gegentore_quote is not None and s["gegentore"] >= 15:
            quote = s["gegentore_spaet"] / s["gegentore"]
            abwehr_faktor = 1 + KONZENTRATION_GEWICHT * (quote - liga_gegentore_quote)
        faktoren[team_id] = (angriff_faktor, abwehr_faktor)
    return faktoren


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
    sperrgrenze = jetzt + timedelta(minutes=SPERRFRIST_MINUTEN)
    bis = jetzt + timedelta(days=tage_voraus)

    staerken, liga_avg, form = liga_kennzahlen_zeitgewichtet(jetzt)
    konzentration = liga_konzentration(jetzt)

    spiele = (
        sb.table("spiele")
        .select("id, heim_team_id, gast_team_id, anstoss, schiedsrichter")
        .gte("anstoss", sperrgrenze.isoformat())
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

        fk_angriff_heim, fk_abwehr_heim = formkurve_faktoren(heim, staerken, form)
        fk_angriff_gast, fk_abwehr_gast = formkurve_faktoren(gast, staerken, form)
        angriff_heim *= fk_angriff_heim
        angriff_gast *= fk_angriff_gast
        abwehr_heim *= fk_abwehr_heim
        abwehr_gast *= fk_abwehr_gast

        tw_angriff_heim, tw_abwehr_heim = trainerwechsel_faktor(heim, jetzt)
        tw_angriff_gast, tw_abwehr_gast = trainerwechsel_faktor(gast, jetzt)
        angriff_heim *= tw_angriff_heim
        angriff_gast *= tw_angriff_gast
        abwehr_heim *= tw_abwehr_heim
        abwehr_gast *= tw_abwehr_gast

        tq_angriff_heim, tq_abwehr_heim = trainer_qualitaet_faktor(heim, jetzt)
        tq_angriff_gast, tq_abwehr_gast = trainer_qualitaet_faktor(gast, jetzt)
        angriff_heim *= tq_angriff_heim
        angriff_gast *= tq_angriff_gast
        abwehr_heim *= tq_abwehr_heim
        abwehr_gast *= tq_abwehr_gast

        kz_angriff_heim, kz_abwehr_heim = konzentration.get(heim, (1.0, 1.0))
        kz_angriff_gast, kz_abwehr_gast = konzentration.get(gast, (1.0, 1.0))
        angriff_heim *= kz_angriff_heim
        angriff_gast *= kz_angriff_gast
        abwehr_heim *= kz_abwehr_heim
        abwehr_gast *= kz_abwehr_gast

        erw_heim = avg * angriff_heim * abwehr_gast * HEIMVORTEIL * schiedsrichter_heimvorteil_faktor(spiel.get("schiedsrichter"))
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
