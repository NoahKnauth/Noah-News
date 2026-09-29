import calendar, html, json, os, re, sys, time, datetime as dt
import feedparser, requests

KEY = os.environ["GEMINI_API_KEY"]
MODELS = [m.strip() for m in os.environ.get("GEMINI_MODELS", "gemini-3.8-flash").split(",") if m.strip()]
MAX_AGE_H, PER_FEED, MAX_IN = 36, 12, 35

PROMPT = """Du bist Redakteur eines täglichen Nachrichtenbriefings für einen Studenten (Logistik/Wirtschaft) in Deutschland.
Themenfeld: {thema}. Unten stehen nummerierte Meldungen aus verschiedenen Quellen (Titel und Anreißertext).

Regeln:
- Wähle nur Meldungen mit echtem Nachrichtenwert. Lass Boulevard, Gewinnspiele, Ratgeber, reine Meinung ohne Faktenkern und Wiederholungen weg.
- Fasse Meldungen zum selben Ereignis zu einer zusammen.
- Übernimm Wesentliches exakt und unvereinfacht: Zahlen, Namen, Daten, Ursachen, Folgen, Fachbegriffe.
- Schreibe NUR, was in den gegebenen Texten steht. Ergänze nichts aus eigenem Wissen. Wenn ein Anreißertext nur wenig hergibt, schreibe entsprechend kurz.
- Widersprechen sich Quellen, benenne den Widerspruch.
- Jede Meldung: 2 bis 5 Sätze, sachlich, auf Deutsch (englische Quellen übersetzen). Höchstens 8 Meldungen, wichtigste zuerst.
- Antworte ausschließlich als JSON: {{"meldungen":[{{"titel":"...","text":"...","quellen":[Nummern]}}]}}

Meldungen:
{items}"""

def collect(feeds):
    cutoff, seen, out = time.time() - MAX_AGE_H * 3600, set(), []
    for f in feeds:
        d = feedparser.parse(f["url"], agent="Mozilla/5.0 (news-briefing)")
        if not d.entries:
            print("KEINE EINTRÄGE:", f["name"], f["url"]); continue
        n = 0
        for e in d.entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            link = e.get("link")
            if not link or link in seen or (t and calendar.timegm(t) < cutoff): continue
            seen.add(link)
            text = html.unescape(re.sub(r"<[^>]+>", " ", e.get("summary", "")))
            out.append({"quelle": f["name"], "titel": html.unescape(e.get("title", "")).strip(),
                        "text": " ".join(text.split())[:600], "url": link})
            n += 1
            if n >= PER_FEED: break
        print(f'{f["name"]}: {n} Meldungen')
    return out[:MAX_IN]

def ask(thema, items):
    listing = "\n\n".join(f'[{i}] ({x["quelle"]}) {x["titel"]}\n{x["text"]}' for i, x in enumerate(items))
    body = {"contents": [{"parts": [{"text": PROMPT.format(thema=thema, items=listing)}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        r = requests.post(url, headers={"x-goog-api-key": KEY}, json=body, timeout=120)
        if r.status_code == 200:
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip())["meldungen"]
        print(f"{model}: HTTP {r.status_code} {r.text[:200]}")
    raise RuntimeError("kein Modell hat geantwortet")

def main():
    cfg = json.load(open("feeds.json", encoding="utf-8"))
    themen, ok = [], 0
    for thema, feeds in cfg.items():
        entry = {"name": thema, "meldungen": []}
        try:
            items = collect(feeds)
            if items:
                for m in ask(thema, items):
                    q = [{"name": items[i]["quelle"], "url": items[i]["url"]}
                         for i in m.get("quellen", []) if isinstance(i, int) and 0 <= i < len(items)]
                    entry["meldungen"].append({"titel": m["titel"], "text": m["text"], "quellen": q})
                ok += 1
            else:
                entry["fehler"] = "Keine aktuellen Meldungen gefunden."
        except Exception as ex:
            print("FEHLER", thema, ex); entry["fehler"] = "Zusammenfassung fehlgeschlagen."
        themen.append(entry)
        time.sleep(15)
    if not ok:
        sys.exit("Alle Themenfelder fehlgeschlagen, data.json bleibt unverändert.")
    os.makedirs("docs", exist_ok=True)
    json.dump({"erstellt": dt.datetime.now(dt.timezone.utc).isoformat(), "themen": themen},
              open("docs/data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

main()
