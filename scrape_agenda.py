#!/usr/bin/env python3
"""
Agenda SP Mais Cultura -> JSON com os eventos dos próximos N dias.

Uso:
  python scrape_agenda.py                      # coleta ao vivo (Playwright) e gera agenda_7dias.json
  python scrape_agenda.py --days 7 --out agenda_7dias.json
  python scrape_agenda.py --from-file todos-eventos   # teste offline com o HTML salvo (só os 10 primeiros eventos)
  python scrape_agenda.py --flat               # saída simples, sem o envelope da Globo
  python scrape_agenda.py --probe              # lista as URLs XHR/fetch que o site chama (para achar a API)
  python scrape_agenda.py --now 2026-11-12T08:00   # simula "agora" (útil p/ testes)

Dependências: pip install playwright python-dateutil requests && playwright install chromium
"""
import argparse
import html
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PAGE_URL = "https://spmaiscultura.prefeitura.sp.gov.br/todos-eventos"
TZ = ZoneInfo("America/Sao_Paulo")

# Se você descobrir o endpoint de detalhe (o que o modal chama ao clicar num card),
# preencha aqui para trazer a descrição completa. Ex.: ".../cultural-events/{id}"
DETAIL_URL_TEMPLATE = None


# --------------------------------------------------------------------------- #
# 1) Extração dos dados
# --------------------------------------------------------------------------- #
def extract_initial(page_html: str):
    """O HTML (SSR do Next.js) já traz os primeiros eventos em JSON, no payload __next_f."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', page_html, re.S)
    flight = "".join(json.loads('"' + c + '"') for c in chunks)
    i = flight.find('{"initialEvents"')
    if i < 0:
        return [], None
    obj, _ = json.JSONDecoder().raw_decode(flight[i:])
    return obj.get("initialEvents", []), obj.get("totalEvents")


def find_events(data):
    """Procura recursivamente, em qualquer JSON, listas de eventos (dicts com 'schedules' e 'name')."""
    found = []
    if isinstance(data, list):
        if data and all(isinstance(x, dict) for x in data) and any("schedules" in x and "name" in x for x in data):
            found.extend(x for x in data if "schedules" in x)
        else:
            for x in data:
                found.extend(find_events(x))
    elif isinstance(data, dict):
        if "schedules" in data and "name" in data:
            found.append(data)
        else:
            for v in data.values():
                found.extend(find_events(v))
    return found


def collect_live(window_end: datetime, full: bool = False):
    """Abre o site, rola a página (scroll infinito) e captura os JSONs que o site mesmo baixa."""
    from playwright.sync_api import sync_playwright

    events = {}
    seen_urls = set()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="pt-BR", timezone_id="America/Sao_Paulo")

        def on_response(resp):
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            try:
                evs = find_events(resp.json())
            except Exception:
                return
            if evs:
                seen_urls.add(resp.url)
                for e in evs:
                    events[e["id"]] = e

        page.on("response", on_response)
        page.goto(PAGE_URL, wait_until="networkidle")

        initial, total = extract_initial(page.content())
        for e in initial:
            events[e["id"]] = e

        stale = 0
        while True:
            before = len(events)
            page.locator("#sentinel").scroll_into_view_if_needed()
            page.wait_for_timeout(1500)
            grew = len(events) > before
            stale = 0 if grew else stale + 1
            if stale >= 3 or (total and len(events) >= total):
                break
            # Lista vem ordenada pela próxima apresentação: se tudo que carregou por último
            # já está depois da janela, não precisa carregar mais (--full desativa isso).
            if not full and grew:
                last = sorted(events.values(), key=lambda e: e.get("nextPresentationDate") or "")[-10:]
                if all((e.get("nextPresentationDate") or "") > window_end.astimezone(ZoneInfo("UTC")).isoformat()
                       for e in last):
                    break
        browser.close()

    print("Endpoints que entregaram eventos:", *sorted(seen_urls), sep="\n  ", file=sys.stderr)
    return list(events.values()), total


def probe():
    """Lista todas as chamadas XHR/fetch do site (scroll + clique no 1º card) para descobrir a API."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="pt-BR")
        page.on("request", lambda r: r.resource_type in ("xhr", "fetch") and print(r.method, r.url))
        page.goto(PAGE_URL, wait_until="networkidle")
        print("--- scroll ---")
        page.locator("#sentinel").scroll_into_view_if_needed()
        page.wait_for_timeout(3000)
        print("--- clique no 1º card ---")
        page.locator("button[culturalevent]").first.click()
        page.wait_for_timeout(3000)
        browser.close()


# --------------------------------------------------------------------------- #
# 2) Normalização
# --------------------------------------------------------------------------- #
def clean(text):
    text = text or ""
    text = re.sub(r"&#?\w*$", "", text)  # entidade cortada no fim (ex.: "Mário&nb")
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def age_code(label):
    if not label:
        return None
    if "LIVRE" in label.upper():
        return "L"
    m = re.search(r"(\d{2})", label)
    return m.group(1) if m else label


def parse_dt(iso):
    return datetime.fromisoformat(iso).astimezone(TZ)


def normalize(raw_events, now: datetime, days: int):
    """Uma linha por OCORRÊNCIA (schedule), não por evento: um evento com 9 datas vira 9 linhas
    e só entram as que caem na janela [agora, fim do dia now+days]."""
    window_end = (now + timedelta(days=days)).replace(hour=23, minute=59, second=59, microsecond=0)
    out = []
    for ev in raw_events:
        raw_desc = (ev.get("description") or "").rstrip()
        truncated = raw_desc.endswith("...")
        desc = clean(raw_desc[:-3] if truncated else raw_desc)
        for s in ev.get("schedules", []):
            start, end = parse_dt(s["startDate"]), parse_dt(s["endDate"])
            if end < now or start > window_end:
                continue
            out.append({
                "id": s["id"],
                "event_id": ev["id"],
                "title": clean(ev.get("name")),
                "description": desc,
                "description_truncated": truncated,
                "type": clean(ev.get("eventTypeName")),
                "age_rating": age_code(ev.get("ageRanges")),
                "age_rating_text": ev.get("ageRanges"),
                "date": start.date().isoformat(),
                "start_time": start.strftime("%H:%M"),
                "end_time": end.strftime("%H:%M"),
                "start_datetime": start.isoformat(),
                "end_datetime": end.isoformat(),
                "is_all_day": bool(s.get("isAllDayEvent")),
                "place_name": s.get("placeName"),
                "address": s.get("fullAddress"),
                "artist": s.get("artistName"),
                "observation": s.get("observation") or None,
                "image_url": ev.get("imageUrl"),
            })
    out.sort(key=lambda o: (o["start_datetime"], o["title"]))
    return out, window_end


def build_feed(items, now: datetime, window_end: datetime, days: int, source: str, total_on_site):
    """Empacota as ocorrências no mesmo envelope do feed da Globo (config/id/items/itemCount/limit/nextPage/source)."""
    stamp = now.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    feed_items = []
    for it in items:
        feed_items.append({
            "id": it["id"],
            "feedId": "spmaiscultura-agenda",
            "type": "event-base-spmaiscultura",
            "lastPublication": stamp,
            "publication": stamp,
            "content": {
                "title": it["title"],
                "summary": it["description"],
                "summaryTruncated": it["description_truncated"],
                "editorial": it["type"],
                "image": {"url": it["image_url"]} if it["image_url"] else None,
                "url": None,
                "product": "SP Mais Cultura",
                "tenantId": "spmaiscultura",
                "theme": "cultura",
                "type": "evento",
                "eventId": it["event_id"],
                "ageRating": it["age_rating"],
                "ageRatingText": it["age_rating_text"],
                "date": it["date"],
                "startTime": it["start_time"],
                "endTime": it["end_time"],
                "startDateTime": it["start_datetime"],
                "endDateTime": it["end_datetime"],
                "isAllDay": it["is_all_day"],
                "placeName": it["place_name"],
                "address": it["address"],
                "artist": it["artist"],
                "observation": it["observation"],
            },
            "draft": False,
            "area": it["date"],  # agrupa por dia (equivalente ao "area" do feed da Globo)
            "parentId": None,
            "children": [],
            "isPublishing": True,
            "created": stamp,
            "modified": stamp,
            "tenantId": "spmaiscultura",
            "kind": "event",
        })
    return {
        "config": {
            "feedType": "EVENTS",
            "timezone": "America/Sao_Paulo",
            "windowFrom": now.date().isoformat(),
            "windowTo": window_end.date().isoformat(),
            "windowDays": days,
            "eventsTotalOnSite": total_on_site,
        },
        "id": "spmaiscultura-agenda-proximos-dias",
        "items": feed_items,
        "itemCount": len(feed_items),
        "limit": len(feed_items),
        "perPage": len(feed_items),
        "nextPage": None,
        "source": source,
    }


def enrich_descriptions(items):
    """Opcional: troca a descrição truncada pela completa, se DETAIL_URL_TEMPLATE estiver preenchido."""
    if not DETAIL_URL_TEMPLATE:
        return
    import requests
    cache = {}
    for it in items:
        if not it["description_truncated"]:
            continue
        eid = it["event_id"]
        if eid not in cache:
            try:
                r = requests.get(DETAIL_URL_TEMPLATE.format(id=eid), timeout=20)
                r.raise_for_status()
                cache[eid] = clean(r.json().get("description"))
            except Exception as exc:
                print(f"detalhe falhou p/ {eid}: {exc}", file=sys.stderr)
                cache[eid] = None
        if cache[eid]:
            it["description"], it["description_truncated"] = cache[eid], False


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="agenda_7dias.json")
    ap.add_argument("--from-file", help="HTML salvo da página (teste offline)")
    ap.add_argument("--now", help="ISO, ex. 2026-11-12T08:00 (horário de Brasília)")
    ap.add_argument("--full", action="store_true", help="carrega todos os eventos, sem parada antecipada")
    ap.add_argument("--flat", action="store_true", help="saída simples (sem o envelope no formato Globo)")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()

    if a.probe:
        return probe()

    now = datetime.fromisoformat(a.now).replace(tzinfo=TZ) if a.now else datetime.now(TZ)
    window_end = (now + timedelta(days=a.days)).replace(hour=23, minute=59, second=59)

    if a.from_file:
        raw, total = extract_initial(Path(a.from_file).read_text(encoding="utf-8"))
        source = f"arquivo:{a.from_file}"
    else:
        raw, total = collect_live(window_end, a.full)
        source = PAGE_URL

    items, window_end = normalize(raw, now, a.days)
    enrich_descriptions(items)

    if not a.flat:
        feed = build_feed(items, now, window_end, a.days, source, total)
        Path(a.out).write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{len(items)} itens no feed ({len(raw)} eventos lidos de {total}) -> {a.out}")
        return

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "timezone": "America/Sao_Paulo",
        "source": source,
        "window": {"from": now.date().isoformat(), "to": window_end.date().isoformat(), "days": a.days},
        "events_scanned": len(raw),
        "events_total_on_site": total,
        "total_occurrences": len(items),
        "events": items,
    }
    Path(a.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(items)} ocorrências na janela ({len(raw)} eventos lidos de {total}) -> {a.out}")


if __name__ == "__main__":
    main()
