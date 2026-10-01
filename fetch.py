import calendar, html, json, os, re, sys, time, datetime as dt
import feedparser, requests

KEY = os.environ["GEMINI_API_KEY"]
MODELS = [m.strip() for m in os.environ.get("GEMINI_MODELS", "gemini-2.5-flash,gemini-3-flash-preview").split(",") if m.strip()]
MAX_AGE_H, PER_FEED, MAX_IN = 36, 12, 35

PROMPT = """Du bist Redakteur eines täglichen Nachrichtenbriefings für einen Studenten (Logistik/Wirtschaft) in Deutschland.

Unten stehen mehrere Themenfelder, jeweils mit nummerierten Meldungen aus verschiedenen Quellen (Titel und Anreißertext).

Regeln für ALLE Themenfelder:
- Wähle nur Meldungen mit echtem Nachrichtenwert. Lass Boulevard, Gewinnspiele, Ratgeber, reine Meinung ohne Faktenkern und Wiederholungen weg.
- Fasse Meldungen zum selben Ereignis zu einer zusammen.
- Übernimm Wesentliches exakt und unvereinfacht: Zahlen, Namen, Daten, Ursachen, Folgen, Fachbegriffe, Kontext.
- Schreibe NUR, was in den gegebenen Texten steht. Ergänze nichts aus eigenem Wissen.
- Für jede Meldung: Erkläre das "Warum" und die "Folgen" basierend auf den verfügbaren Informationen.
- Widersprechen sich Quellen, benenne den Widerspruch.
- Pro Themenfeld: maximal 8 Meldungen, jede 3 bis 7 Sätze (ausführlicher für wichtige Stories), sachlich, auf Deutsch (englische Quellen übersetzen). Wichtigste zuerst.

Antworte ausschließlich als JSON mit dieser Struktur:
{{"themen":[{{"name":"Themaname","meldungen":[{{"titel":"...","text":"...","quellen":[Nummern]}}]}}]}}

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

def ask_all_themas(all_themas_data):
    """Sendet EINE Anfrage an Gemini mit ALLEN Themenfeldern"""
    # Baue die komplette Nachricht mit allen Themenfeldern auf
    items_text = ""
    for thema, items in all_themas_data.items():
        if not items:
            continue
        items_text += f"\n=== {thema} ===\n"
        for i, x in enumerate(items):
            items_text += f'[{i}] ({x["quelle"]}) {x["titel"]}\n{x["text"]}\n\n'
    
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(items=items_text)}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }

    retryable_status = {429, 500, 502, 503, 504}

    for model in MODELS:
        for attempt in range(3):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            try:
                print(f"Sende Anfrage an {model} (Versuch {attempt + 1}/3)...")
                r = requests.post(url, headers={"x-goog-api-key": KEY}, json=body, timeout=120)
                
                if r.status_code == 200:
                    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    response = json.loads(re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip())
                    print(f"✓ {model} erfolgreich")
                    return response.get("themen", [])

                print(f"{model}: HTTP {r.status_code}")
                if r.status_code in retryable_status and attempt < 2:
                    delay = 60 if r.status_code in {429, 503} else 10
                    print(f"Warte {delay}s vor Retry...")
                    time.sleep(delay)
                    continue

                break

            except Exception as ex:
                print(f"{model}: Fehler auf Versuch {attempt + 1}: {ex}")
                if attempt < 2:
                    time.sleep(10)
                    continue
                break

    raise RuntimeError("Kein Modell konnte die Anfrage verarbeiten")

def main():
    cfg = json.load(open("feeds.json", encoding="utf-8"))
    
    print("Sammle Nachrichten von allen RSS-Feeds...")
    all_themas_data = {}
    themen_list = []
    
    for thema, feeds in cfg.items():
        print(f"\n--- {thema} ---")
        items = collect(feeds)
        all_themas_data[thema] = items
        themen_list.append({"name": thema, "meldungen": [], "fehler": None if items else "Keine Meldungen"})
    
    # Nur eine Anfrage an Gemini
    print("\n" + "="*50)
    print("Sende EINE Anfrage an Gemini mit ALLEN Themenfeldern...")
    print("="*50 + "\n")
    
    try:
        gemini_response = ask_all_themas(all_themas_data)
        
        # Verarbeite die Gemini-Antwort und baue die finale Struktur auf
        for gemini_thema in gemini_response:
            thema_name = gemini_thema.get("name", "")
            # Finde das matching Themenfeld
            for entry in themen_list:
                if entry["name"].lower() == thema_name.lower():
                    entry["fehler"] = None
                    # Finde die Items für dieses Thema
                    if thema_name in all_themas_data:
                        items = all_themas_data[thema_name]
                        for m in gemini_thema.get("meldungen", []):
                            q = [{"name": items[i]["quelle"], "url": items[i]["url"]}
                                 for i in m.get("quellen", []) if isinstance(i, int) and 0 <= i < len(items)]
                            entry["meldungen"].append({"titel": m["titel"], "text": m["text"], "quellen": q})
                    break
        
        # Speichere die Daten
        os.makedirs("docs", exist_ok=True)
        json.dump({"erstellt": dt.datetime.now(dt.timezone.utc).isoformat(), "themen": themen_list},
                  open("docs/data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("\n✓ data.json erfolgreich erstellt!")
        
    except Exception as ex:
        print(f"\n✗ FEHLER: {ex}")
        print("data.json bleibt unverändert.")
        os.makedirs("docs", exist_ok=True)
        # Speichere wenigstens die Struktur mit Fehlerhinweis
        json.dump({"erstellt": dt.datetime.now(dt.timezone.utc).isoformat(), "themen": themen_list},
                  open("docs/data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

main()
