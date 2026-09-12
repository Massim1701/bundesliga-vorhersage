"""
Druckt die Trefferquote unserer Vorhersagen (Tendenz + exaktes Ergebnis).

Nutzung:
  python abgleich_report.py --saison 2026 --spieltag 5
  python abgleich_report.py --saison 2026          # ganze Saison
"""
import argparse
import os

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def report(saison: str, spieltag: int | None):
    q = sb.table("abgleich").select("*").eq("saison", saison).not_.is_(
        "tendenz_richtig", "null"
    )
    if spieltag:
        q = q.eq("spieltag", spieltag)
    rows = q.execute().data

    if not rows:
        print("Keine ausgewerteten Spiele gefunden.")
        return

    n = len(rows)
    tendenz_ok = sum(1 for r in rows if r["tendenz_richtig"])
    exakt_ok = sum(1 for r in rows if r["exaktes_ergebnis_richtig"])

    print(f"Saison {saison}" + (f", Spieltag {spieltag}" if spieltag else " (gesamt)"))
    print(f"Spiele ausgewertet:      {n}")
    print(f"Tendenz richtig:         {tendenz_ok}/{n} ({tendenz_ok / n:.1%})")
    print(f"Exaktes Ergebnis richtig:{exakt_ok}/{n} ({exakt_ok / n:.1%})")
    print()
    for r in rows:
        status = "✓" if r["tendenz_richtig"] else "✗"
        print(
            f"{status} ST{r['spieltag']:>2}  {r['heim_team']:<22} "
            f"{r['tore_heim']}:{r['tore_gast']}  "
            f"(Tipp {r['tipp_tore_heim']}:{r['tipp_tore_gast']})  {r['gast_team']}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--saison", required=True)
    parser.add_argument("--spieltag", type=int, default=None)
    args = parser.parse_args()
    report(args.saison, args.spieltag)
