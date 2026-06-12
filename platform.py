#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TFSAnaliz Platform"""

import json, os, threading, urllib.request, urllib.parse, re, smtplib, ssl, secrets
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
        else:
            self.send_response(404); self.end_head