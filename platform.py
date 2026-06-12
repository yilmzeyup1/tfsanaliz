#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TFSAnaliz Platform"""

import json, os, threading, urllib.request, urllib.parse, re, smtplib, ssl, secrets, io
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

BASE       = os.path.dirname(os.path.abspath(__file__))
DATA_PATH  = os.path.join(BASE, "platform_data.json")
NEWS_CACHE = os.path.join(BASE, "news_cache.json")
SUBS_PATH  = os.path.join(BASE, "subscribers.json")
CFG_PATH   = os.path.join(BASE, "config.json")
TMPL_PATH  = os.path.join(BASE, "platform_template.html")


# --- Oturum Yonetimi ---
SESSIONS = {}
PLATFORM_PASSWORD = os.environ.get("PLATFORM_PASSWORD", "tfsanaliz2026")

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="tr">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TFSAnaliz — Giriş</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#060d18;display:flex;align-items:center;justify-content:center;min-height:100vh;font-family:'Segoe UI',sans-serif}
.card{background:#0d1a2d;border:1px solid #1a3356;border-radius:16px;padding:48px 40px;width:360px;text-align:center}
.logo{font-size:28px;font-weight:700;color:#f0f6ff;margin-bottom:8px}
.sub{font-size:13px;color:#6b8cae;margin-bottom:32px}
input{width:100%;padding:12px 16px;background:#060d18;border:1px solid #1a3356;border-radius:8px;color:#f0f6ff;font-size:14px;margin-bottom:16px;outline:none}
input:focus{border-color:#3b82f6}
button{width:100%;padding:13px;background:#3b82f6;color:#fff;border:none;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer}
button:hover{background:#2563eb}
.err{color:#f87171;font-size:13px;margin-bottom:12px}
</style></head>
<body><div class="card">
<div class="logo">TFSAnaliz</div>
<div class="sub">Tasarruf Finansman Sektör Analizi</div>
{error}
<form method="POST" action="/login">
<input type="password" name="password" placeholder="Şifre" autofocus required>
<button type="submit">Giriş Yap</button>
</form>
</div></body></html>"""

def create_session():
    token = secrets.token_hex(24)
    SESSIONS[token] = datetime.now()
    return token

def is_valid_session(cookie_header):
    if not cookie_header: return False
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("tfs_session="):
            token = part[12:]
            if token in SESSIONS:
                if datetime.now() - SESSIONS[token] < timedelta(hours=24):
                    SESSIONS[token] = datetime.now()
                    return True
                else:
                    del SESSIONS[token]
    return False

def load_data():
    with open(DATA_PATH, encoding="utf-8") as f: return json.load(f)

def load_cfg():
    try:
        with open(CFG_PATH, encoding="utf-8") as f: cfg = json.load(f)
    except: cfg = {}
    # Railway / production ortam değişkenlerinden oku (öncelikli)
    if os.environ.get("CLAUDE_API_KEY"): cfg["claude_api_key"] = os.environ["CLAUDE_API_KEY"]
    if os.environ.get("PLATFORM_DOMAIN"): cfg["platform_domain"] = os.environ["PLATFORM_DOMAIN"]
    if os.environ.get("EMAIL_SENDER"):
        cfg.setdefault("email", {})["sender"] = os.environ["EMAIL_SENDER"]
    if os.environ.get("EMAIL_APP_PASSWORD"):
        cfg.setdefault("email", {})["app_password"] = os.environ["EMAIL_APP_PASSWORD"]
    return cfg

def load_subscribers():
    if os.path.exists(SUBS_PATH):
        with open(SUBS_PATH, encoding="utf-8") as f: return json.load(f)
    return {"list": []}

def save_subscribers(d):
    with open(SUBS_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def load_news_cache():
    if os.path.exists(NEWS_CACHE):
        with open(NEWS_CACHE, encoding="utf-8") as f: return json.load(f)
    return {"items": [], "ai_analysis": "", "updated": ""}

def save_news_cache(d):
    with open(NEWS_CACHE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def load_template():
    with open(TMPL_PATH, encoding="utf-8") as f: return f.read()

class StripHTML(HTMLParser):
    def __init__(self): super().__init__(); self.parts = []
    def handle_data(self, d): self.parts.append(d)
    def get(self): return " ".join(self.parts).strip()

def strip_html(s):
    p = StripHTML(); p.feed(s or ""); return p.get()

def fetch_url(url, timeout=12):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "tr-TR,tr;q=0.9"
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except: return None

def gnews(q, n=6):
    url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q+' when:7d')}&hl=tr-TR&gl=TR&ceid=TR:tr"
    raw = fetch_url(url)
    if not raw: return []
    items = []
    try:
        root = ET.fromstring(raw)
        for item in root.findall(".//item"):
            t = item.find("title"); l = item.find("link")
            d = item.find("description"); p = item.find("pubDate")
            title = (t.text or "").strip() if t is not None else ""
            if not title: continue
            items.append({
                "title": title,
                "link":  (l.text or "#") if l is not None else "#",
                "desc":  strip_html(d.text or "")[:200] if d is not None else "",
                "date":  (p.text or "")[:16] if p is not None else "",
            })
    except: pass
    return items[:n]

def claude_call(prompt, max_tokens=1500):
    cfg = load_cfg()
    api_key = cfg.get("claude_api_key","")
    if not api_key or "YOUR_KEY" in api_key: return None
    try:
        body = json.dumps({"model":"claude-opus-4-6","max_tokens":max_tokens,"messages":[{"role":"user","content":prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
            headers={"x-api-key":api_key,"anthropic-version":"2023-06-01","content-type":"application/json"})
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read())["content"][0]["text"]
    except Exception as e:
        return f"Claude API hatasi: {e}"

news_lock  = threading.Lock()
news_state = {"running": False}

def refresh_news_task():
    with news_lock:
        if news_state["running"]: return
        news_state["running"] = True
    try:
        print(f"  [{datetime.now().strftime('%H:%M')}] Haberler guncelleniyor...")
        queries = [
            '"tasarruf finansman" BDDK',
            '"tasarruf finansman" yonetmelik duzenleme',
            '"tasarruf finansman sirketi" kampanya',
            'BDDK "konut finansman" denetim',
            'tasarruf finansman sirketi haber',
        ]
        seen, items = set(), []
        for q in queries:
            for i in gnews(q, 5):
                if i["title"] not in seen:
                    seen.add(i["title"]); items.append(i)
        ai_text = ""
        if items:
            news_summary = "\n".join(f"- {i['title']}: {i['desc'][:100]}" for i in items[:10])
            prompt = f"""Turkiye tasarruf finansman sektoru arastirma platformu icin asagidaki son haberleri analiz et.
Sektor profesyonelleri icin kisa, net Turkce analiz yaz.

Haberler:
{news_summary}

Su basliklar altinda yaz (her biri 2-3 cumle):
**Yone Cikan Gelisme**
**Sektorel Etki**
**Dikkat Edilmesi Gereken**

Toplam 150-200 kelime."""
            ai_text = claude_call(prompt, 600) or ""
        cache = {"items": items, "ai_analysis": ai_text, "updated": datetime.now().strftime("%d.%m.%Y %H:%M")}
        save_news_cache(cache)
        print(f"  [{datetime.now().strftime('%H:%M')}] {len(items)} haber, AI: {'OK' if ai_text else 'yok'}")
    except Exception as e:
        print(f"  [HATA] {e}")
    finally:
        news_state["running"] = False

def get_or_refresh_news():
    cache = load_news_cache()
    if cache.get("updated"):
        try:
            last = datetime.strptime(cache["updated"], "%d.%m.%Y %H:%M")
            if datetime.now() - last < timedelta(minutes=30):
                return cache
        except: pass
    if not news_state["running"]:
        threading.Thread(target=refresh_news_task, daemon=True).start()
    return cache

def send_smtp(to_list, subject, html_body):
    cfg = load_cfg(); ec = cfg.get("email", {})
    sender, pw = ec.get("sender",""), ec.get("app_password","")
    if not sender or not pw: return False, "E-posta ayarlari eksik"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject; msg["From"] = f"TFSAnaliz <{sender}>"; msg["To"] = ", ".join(to_list)
    msg.attach(MIMEText("HTML destekli istemci kullaniniz.", "plain","utf-8"))
    msg.attach(MIMEText(html_body, "html","utf-8"))
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
            s.login(sender, pw); s.sendmail(sender, to_list, msg.as_bytes())
        return True, f"{len(to_list)} kisiye gonderildi"
    except smtplib.SMTPAuthenticationError: return False, "Gmail App Password hatali"
    except Exception as e: return False, str(e)

def build_newsletter_html(news_items, ai_analysis):
    now   = datetime.now().strftime("%d.%m.%Y")
    count = len(news_items)
    cards = ""
    for n in news_items[:6]:
        cards += f'<div style="background:#0d1a2d;border:1px solid #1a3356;border-radius:8px;padding:14px;margin-bottom:8px"><a href="{n["link"]}" style="font-size:14px;font-weight:600;color:#60a5fa;text-decoration:none">{n["title"]}</a><p style="font-size:12px;color:#6b8cae;margin:6px 0 0">{n["desc"]}</p><span style="font-size:11px;color:#475569">{n.get("date","")}</span></div>'
    ai_section = ""
    if ai_analysis:
        ai_clean = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', ai_analysis).replace("\n","<br>")
        ai_section = f'<div style="background:#0a1f0a;border:1px solid #166534;border-radius:10px;padding:18px;margin-bottom:20px"><div style="font-size:14px;font-weight:700;color:#4ade80;margin-bottom:10px">Claude AI Analizi</div><div style="font-size:13px;color:#d1fae5;line-height:1.8">{ai_clean}</div></div>'
    return f'<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8"></head><body style="margin:0;padding:0;background:#060d18;font-family:sans-serif"><table width="100%" cellpadding="0" cellspacing="0" style="background:#060d18;padding:24px 0"><tr><td align="center"><table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%"><tr><td style="background:#0d1a2d;border-radius:12px 12px 0 0;padding:24px 28px"><div style="font-size:20px;font-weight:700;color:#f0f6ff">TFSAnaliz</div><div style="font-size:12px;color:#6b8cae">Haftalik Bulten - {now} - {count} haber</div></td></tr><tr><td style="background:#060d18;padding:20px 28px">{ai_section}{cards}<div style="text-align:center;margin-top:20px"><a href="PLATFORM_URL" style="background:#3b82f6;color:#fff;border-radius:8px;padding:11px 24px;font-size:14px;font-weight:600;text-decoration:none">Platforma Git</a></div></td></tr><tr><td style="background:#0d1a2d;border-radius:0 0 12px 12px;padding:14px 28px;text-align:center"><p style="font-size:11px;color:#475569;margin:0">TFSAnaliz - <a href="UNSUBSCRIBE_URL" style="color:#6b8cae">Aboneligi iptal et</a></p></td></tr></table></td></tr></table></body></html>'

# ──────────────────────────────────────────────────────────────────────────────
# PDF Yükleme & Finansal Veri Çıkarma
# ──────────────────────────────────────────────────────────────────────────────

def parse_multipart_file(raw_body, content_type):
    """Multipart/form-data body'sinden PDF dosyasını çıkar."""
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[9:].strip('"').encode()
            break
    if not boundary:
        return None, None, "Boundary bulunamadi"

    delimiter = b"--" + boundary
    parts = raw_body.split(delimiter)
    for part in parts[1:]:
        if part.strip() in (b"--", b"--\r\n", b""):
            continue
        if b"\r\n\r\n" in part:
            hdr, body = part.split(b"\r\n\r\n", 1)
        elif b"\n\n" in part:
            hdr, body = part.split(b"\n\n", 1)
        else:
            continue
        if body.endswith(b"\r\n"):
            body = body[:-2]
        headers = {}
        for line in hdr.split(b"\r\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower().decode("utf-8", errors="replace")] = v.strip().decode("utf-8", errors="replace")
        disp = headers.get("content-disposition", "")
        fm = re.search(r'filename="([^"]+)"', disp)
        if fm:
            return body, fm.group(1), None
    return None, None, "PDF dosyasi form verisi icinde bulunamadi"


def extract_pdf_text(pdf_bytes, page_range=None):
    """pdfplumber ile PDF'den metin çıkar."""
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            total = len(pdf.pages)
            pr = page_range if page_range is not None else range(total)
            for i in pr:
                if i < total:
                    t = pdf.pages[i].extract_text() or ""
                    if t.strip():
                        parts.append(f"[Sayfa {i+1}]\n{t}")
        return "\n\n".join(parts), None
    except ImportError:
        return None, "pdfplumber kutuphanesi yuklu degil (requirements.txt kontrol edin)"
    except Exception as e:
        return None, f"PDF okuma hatasi: {e}"


def extract_financials_with_claude(pdf_text, filename):
    """Claude API'ye PDF metnini gonderir, finansal verileri JSON olarak alir."""
    truncated = pdf_text[:16000] if len(pdf_text) > 16000 else pdf_text
    prompt = f"""Asagidaki metin bir BDDK bagimsiz denetim raporundan alinmistir.
Dosya adi: {filename}

PDF metni:
{truncated}

Bu metinden finansal verileri cikar. SADECE asagidaki JSON formatinda don, baska hicbir aciklama ekleme:

{{
  "sirket_adi": "...",
  "donem": "...",
  "toplam_varlik_mn_tl": ...,
  "tf_alacaklari_mn_tl": ...,
  "tf_borclar_mn_tl": ...,
  "ozkaynaklar_mn_tl": ...,
  "donem_kari_mn_tl": ...
}}

Kurallar:
- sirket_adi: raporun basligindaki tam sirket adi (Tasarruf Finansman A.S. ile birlikte)
- donem: raporun bitis tarihi, ornegin "31.12.2025"
- Tutarlar milyon TL cinsinden sayisal deger. Rapor "Bin TL" birimindeyse 1000'e bol; "TL" birimindeyse 1.000.000'a bol.
- Bulunamazsa null kullan.
- Yanit sadece JSON olmali."""

    result = claude_call(prompt, 600)
    if not result:
        return None, "Claude API yanit vermedi"
    try:
        m = re.search(r"\{[\s\S]*\}", result)
        if m:
            return json.loads(m.group()), None
        return None, f"JSON parse edilemedi. Claude yaniti: {result[:300]}"
    except Exception as e:
        return None, f"JSON hatasi: {e} — Yanit: {result[:300]}"


def update_company_financials(extracted):
    """platform_data.json'daki ilgili sirketin finansal verilerini guncelle."""
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)

        sirket_adi_lower = (extracted.get("sirket_adi") or "").lower()
        matched = None

        # Basit kelime eslestirmesi
        for c in data.get("sirketler", []):
            candidates = [
                c.get("id", ""),
                c.get("marka", "").lower(),
                c.get("tam_ad", "").lower().split()[0],   # ilk kelime
                c.get("ad", "").lower().replace(" tf", "").strip(),
            ]
            if any(kw and kw in sirket_adi_lower for kw in candidates):
                matched = c
                break

        if not matched:
            # Ters arama: tam_ad'in ilk iki kelimesinin extracted isimde olup olmadigi
            for c in data.get("sirketler", []):
                words = c.get("tam_ad", "").lower().split()[:2]
                if all(w in sirket_adi_lower for w in words if w not in ("a.ş.", "a.s.", "tasarruf", "finansman")):
                    matched = c
                    break

        if not matched:
            return False, f"Sirket eslestirilemedi: '{extracted.get('sirket_adi')}'. platform_data.json'daki tam_ad degerlerini kontrol edin."

        for field in ["donem", "toplam_varlik_mn_tl", "tf_alacaklari_mn_tl",
                      "tf_borclar_mn_tl", "ozkaynaklar_mn_tl", "donem_kari_mn_tl"]:
            if extracted.get(field) is not None:
                matched[field] = extracted[field]
        matched["finansal_guncelleme"] = datetime.now().strftime("%d.%m.%Y")
        data["_meta"]["guncelleme"] = datetime.now().strftime("%Y-%m-%d")

        with open(DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return True, matched.get("tam_ad", matched.get("ad", "?"))
    except Exception as e:
        return False, str(e)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        cookie = self.headers.get("Cookie","")
        if self.path == "/login":
            self.send_html(LOGIN_PAGE.replace("{error}",""))
            return
        if self.path == "/logout":
            self.send_response(302)
            self.send_header("Location","/login")
            self.send_header("Set-Cookie","tfs_session=; Max-Age=0; Path=/")
            self.end_headers(); return
        if not is_valid_session(cookie):
            self.send_response(302)
            self.send_header("Location","/login")
            self.end_headers(); return
        if self.path == "/":
            self.send_html(load_template())
        elif self.path == "/api/data":
            self.send_json(load_data())
        elif self.path == "/api/news":
            self.send_json(get_or_refresh_news())
        elif self.path.startswith("/api/unsubscribe"):
            token = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("token",[""])[0]
            subs = load_subscribers()
            subs["list"] = [s for s in subs["list"] if s.get("token") != token]
            save_subscribers(subs)
            self.send_html("<html><body style='font-family:sans-serif;text-align:center;padding:60px;background:#060d18;color:#c8d8eb'><h2>Aboneliginiz iptal edildi.</h2><p><a href='/' style='color:#60a5fa'>Ana sayfaya don</a></p></body></html>")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length",0))
        body   = self.rfile.read(length) if length else b""

        if self.path == "/login":
            try:
                params = urllib.parse.parse_qs(body.decode())
                pw = params.get("password",[""])[0]
                if pw == PLATFORM_PASSWORD:
                    token = create_session()
                    self.send_response(302)
                    self.send_header("Location","/")
                    self.send_header("Set-Cookie",f"tfs_session={token}; Path=/; HttpOnly; Max-Age=86400")
                    self.end_headers()
                else:
                    self.send_html(LOGIN_PAGE.replace("{error}",'<div class="err">Hatalı şifre</div>'))
            except Exception as e:
                self.send_html(LOGIN_PAGE.replace("{error}",f'<div class="err">{e}</div>'))
            return

        if not is_valid_session(self.headers.get("Cookie","")):
            self.send_response(302)
            self.send_header("Location","/login")
            self.end_headers(); return

        if self.path == "/api/subscribe":
            try:
                data  = json.loads(body)
                email = data.get("email","").strip().lower()
                if not email or "@" not in email:
                    self.send_json({"ok":False,"msg":"Gecersiz e-posta adresi"}); return
                subs = load_subscribers()
                if any(s["email"] == email for s in subs["list"]):
                    self.send_json({"ok":False,"msg":"Bu adres zaten kayitli."}); return
                token = secrets.token_hex(16)
                subs["list"].append({"email":email,"token":token,"date":datetime.now().strftime("%d.%m.%Y")})
                save_subscribers(subs)
                cfg = load_cfg()
                domain = cfg.get("platform_domain","http://localhost:5001")
                unsub_url = f"{domain}/api/unsubscribe?token={token}"
                welcome = f'<html><body style="font-family:sans-serif;background:#060d18;color:#c8d8eb;padding:40px;max-width:500px;margin:0 auto"><h2 style="color:#60a5fa">TFSAnaliz Bulteni - Hos Geldiniz!</h2><p>Haftalik sektor analizi artik dogrudan e-postanizda.</p><p style="font-size:12px;color:#475569;margin-top:32px">Iptal: <a href="{unsub_url}" style="color:#6b8cae">{unsub_url}</a></p></body></html>'
                send_smtp([email], "TFSAnaliz Bulteni - Hos Geldiniz!", welcome)
                self.send_json({"ok":True,"msg":"Kaydiniz alindi! Hos geldin maili gonderildi."})
            except Exception as e:
                self.send_json({"ok":False,"msg":str(e)})

        elif self.path == "/api/send_newsletter":
            try:
                subs = load_subscribers()
                if not subs["list"]:
                    self.send_json({"ok":False,"msg":"Abone listesi bos"}); return
                news = load_news_cache()
                cfg  = load_cfg()
                domain = cfg.get("platform_domain","http://localhost:5001")
                ok_count = 0
                for sub in subs["list"]:
                    unsub_url = f"{domain}/api/unsubscribe?token={sub['token']}"
                    html = build_newsletter_html(news.get("items",[]), news.get("ai_analysis",""))
                    html = html.replace("PLATFORM_URL", domain).replace("UNSUBSCRIBE_URL", unsub_url)
                    ok, _ = send_smtp([sub["email"]], f"TFSAnaliz Haftalik Bulten - {datetime.now().strftime('%d.%m.%Y')}", html)
                    if ok: ok_count += 1
                self.send_json({"ok":True,"msg":f"Bulten {ok_count}/{len(subs['list'])} kisiye gonderildi"})
            except Exception as e:
                self.send_json({"ok":False,"msg":str(e)})

        elif self.path == "/api/refresh_news":
            threading.Thread(target=refresh_news_task, daemon=True).start()
            self.send_json({"ok":True})

        elif self.path == "/api/upload_pdf":
            try:
                ct = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in ct:
                    self.send_json({"ok": False, "msg": "multipart/form-data bekleniyor"}); return

                pdf_bytes, filename, err = parse_multipart_file(body, ct)
                if err:
                    self.send_json({"ok": False, "msg": err}); return
                if not filename or not filename.lower().endswith(".pdf"):
                    self.send_json({"ok": False, "msg": "Lutfen .pdf uzantili dosya yukleyin"}); return

                # Finansal sayfalar genellikle 5-20 arasindadir; once orada dene
                pdf_text, err = extract_pdf_text(pdf_bytes, range(4, 20))
                if err:
                    self.send_json({"ok": False, "msg": err}); return
                if not pdf_text.strip():
                    # Tum sayfaları tara
                    pdf_text, err = extract_pdf_text(pdf_bytes)
                    if err or not (pdf_text or "").strip():
                        self.send_json({"ok": False, "msg": "PDF metnine erisilemedi (taranmis goruntu PDF olabilir)"}); return

                extracted, err = extract_financials_with_claude(pdf_text, filename)
                if err:
                    self.send_json({"ok": False, "msg": err}); return

                ok, info = update_company_financials(extracted)
                if not ok:
                    self.send_json({"ok": False, "msg": info, "extracted": extracted}); return

                self.send_json({
                    "ok": True,
                    "msg": f"✓ {info} finansal verileri basariyla guncellendi.",
                    "extracted": extracted
                })
            except Exception as e:
                self.send_json({"ok": False, "msg": f"Sunucu hatasi: {e}"})

        else:
            self.send_response(404); self.end_headers()

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 8080))
    print("=" * 52, flush=True)
    print("  TFSAnaliz - Arastirma Platformu", flush=True)
    print(f"  http://0.0.0.0:{PORT}", flush=True)
    print("=" * 52, flush=True)
    threading.Thread(target=refresh_news_task, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Durduruldu.", flush=True)
