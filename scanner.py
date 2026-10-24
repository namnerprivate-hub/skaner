"""Skaner nowych tokenów Solana -> alerty na Telegram.
Dane: DexScreener (API publiczne) + RugCheck (kontrola ryzyka).
To NIE jest rekomendacja zakupu. Alert = token przeszedł filtry, resztę sprawdzasz sam.
"""
import json
import os
import time
import urllib.request

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Filtry (można zmienić w pliku scan.yml, sekcja env)
MIN_LIQ = float(os.getenv("MIN_LIQ", "10000"))        # min płynność w USD
MIN_VOL_H1 = float(os.getenv("MIN_VOL_H1", "5000"))   # min wolumen z 1h w USD
MIN_BUYS_H1 = int(os.getenv("MIN_BUYS_H1", "30"))     # min liczba kupnów w 1h
MAX_AGE_H = float(os.getenv("MAX_AGE_H", "24"))       # max wiek pary w godzinach
MIN_AGE_MIN = float(os.getenv("MIN_AGE_MIN", "15"))   # min wiek (omija pierwsze minuty snajperów)

SEEN_FILE = "seen.json"
UA = {"User-Agent": "Mozilla/5.0"}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def send(text):
    data = json.dumps({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=25).read()


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen):
    cutoff = time.time() - 3 * 86400
    seen = {k: v for k, v in seen.items() if v > cutoff}
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f)


def rugcheck(mint):
    """Zwraca (ok, ostrzeżenia). ok=False jeśli RugCheck widzi poważne ryzyko."""
    try:
        d = get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")
    except Exception:
        return True, ["RugCheck: brak danych, sprawdź ręcznie!"]
    risks = d.get("risks") or []
    danger = [r.get("name", "?") for r in risks if r.get("level") == "danger"]
    warn = [r.get("name", "?") for r in risks if r.get("level") == "warn"]
    if danger:
        return False, danger
    return True, warn


def candidates():
    """Najnowsze profile tokenów z Solany."""
    profiles = get("https://api.dexscreener.com/token-profiles/latest/v1")
    boosts = []
    try:
        boosts = get("https://api.dexscreener.com/token-boosts/latest/v1")
    except Exception:
        pass
    mints = []
    for p in list(profiles) + list(boosts):
        if p.get("chainId") == "solana" and p.get("tokenAddress") not in mints:
            mints.append(p["tokenAddress"])
    return mints


def pairs_for(mints):
    out = []
    for i in range(0, len(mints), 30):
        chunk = ",".join(mints[i:i + 30])
        try:
            out += get(f"https://api.dexscreener.com/tokens/v1/solana/{chunk}")
        except Exception as e:
            print("DexScreener błąd:", e)
    return out


def fmt(n):
    n = float(n or 0)
    if n >= 1e6:
        return f"{n/1e6:.2f}M"
    if n >= 1e3:
        return f"{n/1e3:.1f}k"
    return f"{n:.0f}"


def main():
    seen = load_seen()
    mints = [m for m in candidates() if m not in seen]
    print("Nowe kandydaty:", len(mints))
    best = {}
    for p in pairs_for(mints):
        mint = p["baseToken"]["address"]
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if mint not in best or liq > ((best[mint].get("liquidity") or {}).get("usd") or 0):
            best[mint] = p

    now = time.time()
    sent = 0
    for mint, p in best.items():
        created = (p.get("pairCreatedAt") or 0) / 1000
        age_min = (now - created) / 60 if created else 0
        liq = (p.get("liquidity") or {}).get("usd") or 0
        vol1 = (p.get("volume") or {}).get("h1") or 0
        buys1 = ((p.get("txns") or {}).get("h1") or {}).get("buys") or 0

        if not (MIN_AGE_MIN <= age_min <= MAX_AGE_H * 60):
            continue
        if liq < MIN_LIQ or vol1 < MIN_VOL_H1 or buys1 < MIN_BUYS_H1:
            continue

        ok, notes = rugcheck(mint)
        seen[mint] = now  # nie wysyłaj dwa razy, nawet jeśli odrzucony
        if not ok:
            print("Odrzucony:", mint, notes)
            continue

        ch = p.get("priceChange") or {}
        sym = p["baseToken"].get("symbol", "?")
        msg = (
            f"🔔 <b>{sym}</b> przeszedł filtry\n"
            f"Wiek: {age_min/60:.1f} h\n"
            f"Płynność: ${fmt(liq)} | Wolumen 1h: ${fmt(vol1)}\n"
            f"Kupna 1h: {buys1} | Zmiana ceny 5m/1h: {ch.get('m5','?')}% / {ch.get('h1','?')}%\n"
        )
        if notes:
            msg += "⚠️ Uwagi: " + ", ".join(notes[:4]) + "\n"
        msg += (
            f"\n<a href=\"https://axiom.trade/meme/{p.get('pairAddress')}\">Axiom</a> | "
            f"<a href=\"{p.get('url')}\">DexScreener</a> | "
            f"<a href=\"https://rugcheck.xyz/tokens/{mint}\">RugCheck</a>\n"
            f"<code>{mint}</code>\n\n"
            "To nie jest rekomendacja. Sprawdź holderów i dewelopera, ustaw stop-loss."
        )
        send(msg)
        sent += 1
        if sent >= 5:  # limit alertów na jedno uruchomienie
            break

    # zapamiętaj też resztę, żeby nie sprawdzać w kółko
    for m in mints:
        seen.setdefault(m, now)
    save_seen(seen)
    print("Wysłano alertów:", sent)


if __name__ == "__main__":
    main()
