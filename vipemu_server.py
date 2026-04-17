import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import html
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse
import urllib3
import ipaddress

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from stb_client import STBClient
from stream_hub import (
    MAGSessionLoop,
    MAG_SYNC_TIMEOUT,
    STREAM_HUBS,
    HUBS_LOCK,
    get_or_create_hub,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "vipemu.log")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DEFAULT_PORT = 5000

_LOG_BUFFER: deque = deque(maxlen=200)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        _LOG_BUFFER.append(self.format(record))


def _setup_logging():
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s — %(message)s", datefmt="%H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    bh = _BufferHandler()
    bh.setFormatter(fmt)
    root.addHandler(bh)


_setup_logging()
log = logging.getLogger("vipemu_server")

CONFIG_LOCK = threading.Lock()


def load_config() -> dict:
    with CONFIG_LOCK:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                log.info("Config yüklendi: %d portal.", len(cfg.get("portals", [])))
                return cfg
            except Exception as exc:
                log.error("Config okuma hatası: %s", exc)
        log.warning("Config bulunamadı, varsayılan ayarlar kullanılıyor.")
        return {"port": DEFAULT_PORT, "portals": []}


def save_config(cfg: dict):
    with CONFIG_LOCK:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
        except Exception as exc:
            log.error("Config kayıt hatası: %s", exc)


PORTALS: dict = {}
MAG_LOOPS: dict = {}


def init_portal(p_id: str, url: str, mac: str, types=None, filter_cats=None) -> bool:
    if types is None:
        types = ["itv"]
    for pid, p in PORTALS.items():
        if p.portal == url.rstrip("/") and p.mac == mac.upper():
            if p.is_syncing:
                log.info("[!] %s zaten senkronize ediliyor, init iptal.", p.portal)
                return True
            log.info("[*] %s zaten ekli, sync tetikleniyor...", p.portal)
            threading.Thread(
                target=p.sync_channels, args=(types, filter_cats), daemon=True
            ).start()
            return True
    client = STBClient(url, mac)
    if client.handshake():
        PORTALS[p_id] = client
        threading.Thread(
            target=client.sync_channels, args=(types, filter_cats), daemon=True
        ).start()
        loop = MAGSessionLoop(client)
        loop.start()
        MAG_LOOPS[p_id] = loop
        client._session_loop = loop
        return True
    log.error("[!] Portal handshake BAŞARISIZ: %s", url)
    return False


_CHECK_HISTORY: dict = {}
_CHECK_LOCK = threading.Lock()
CHECK_COOLDOWN = 5


def _rate_limit_check(ip: str) -> bool:
    now = time.time()
    with _CHECK_LOCK:
        last = _CHECK_HISTORY.get(ip, 0)
        if now - last < CHECK_COOLDOWN:
            return False
        _CHECK_HISTORY[ip] = now
        return True


def _is_safe_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.hostname:
            try:
                ip = ipaddress.ip_address(parsed.hostname)
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    return False
            except ValueError:
                # Hostname bir domain adı (örn: backup.xp1.tv), IP değil.
                # Domain isimlerine her zaman izin ver.
                pass
        return True
    except Exception:
        return False


CSS = """
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap');
:root { --p: #00f2ff; --s: #bc13fe; --bg: #05060a; --glass: rgba(255,255,255,0.03); --border: rgba(255,255,255,0.1); --success: #00c853; --warn: #ffc107; --danger: #ff4b2b; }
* { box-sizing: border-box; transition: all 0.25s ease; }
body { background: var(--bg); color: #fff; font-family: 'Outfit', sans-serif; margin: 0; overflow-x: hidden; background-image: radial-gradient(circle at 10% 20%, rgba(188,19,254,0.1) 0%, transparent 40%), radial-gradient(circle at 90% 80%, rgba(0,242,255,0.1) 0%, transparent 40%); background-attachment: fixed; }
.dash { max-width: 1160px; margin: 50px auto; padding: 20px; animation: fadeIn 0.7s ease-out; }
@keyframes fadeIn { from { opacity:0; transform:translateY(18px); } to { opacity:1; transform:none; } }
.card { background: var(--glass); backdrop-filter: blur(20px); border: 1px solid var(--border); border-radius: 22px; padding: 28px; margin-bottom: 28px; box-shadow: 0 20px 50px rgba(0,0,0,0.5); position: relative; overflow: hidden; }
h1 { font-size: 40px; font-weight: 800; margin: 0 0 36px; text-align: center; letter-spacing: -1px; background: linear-gradient(90deg, var(--p), var(--s)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.btn { background: linear-gradient(45deg, var(--p), var(--s)); border: none; color: #fff; padding: 13px 26px; border-radius: 12px; cursor: pointer; font-weight: 700; text-transform: uppercase; font-size: 13px; letter-spacing: 1px; box-shadow: 0 8px 18px rgba(188,19,254,0.3); display: inline-flex; align-items: center; gap: 8px; text-decoration: none; }
.btn:hover { transform: translateY(-3px) scale(1.04); box-shadow: 0 14px 28px rgba(188,19,254,0.5); }
.btn-secondary { background: rgba(255,255,255,0.08); border: 1px solid var(--border); box-shadow: none; }
.btn-secondary:hover { background: rgba(255,255,255,0.14); transform: none; box-shadow: none; }
.btn-green { background: linear-gradient(45deg, #00c853, #b2ff59); }
.btn-danger { background: linear-gradient(45deg, #ff4b2b, #ff416c); }
input[type=text], input:not([type=checkbox]):not([type=radio]) { background: rgba(0,0,0,0.45); border: 1px solid var(--border); color: #fff; padding: 13px 18px; border-radius: 12px; outline: none; }
input[type=text]:focus { border-color: var(--p); box-shadow: 0 0 12px rgba(0,242,255,0.2); }
.portal-row { display: flex; justify-content: space-between; align-items: center; padding: 18px 22px; border-radius: 16px; margin-bottom: 10px; background: rgba(255,255,255,0.02); gap: 12px; flex-wrap: wrap; }
.portal-row:hover { background: rgba(255,255,255,0.05); transform: translateX(8px); }
.badge { background: rgba(0,242,255,0.1); color: var(--p); padding: 3px 12px; border-radius: 20px; font-size: 12px; font-weight: 700; }
.badge-warn { background: rgba(255,193,7,0.15); color: var(--warn); }
.badge-green { background: rgba(0,200,83,0.15); color: var(--success); }
.spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(255,255,255,0.15); border-top-color: #fff; border-radius: 50%; animation: spin 0.8s linear infinite; vertical-align: middle; }
@keyframes spin { to { transform: rotate(360deg); } }
.cat-item { display: flex; align-items: center; gap: 10px; font-size: 13px; cursor: pointer; padding: 10px 12px; background: rgba(255,255,255,0.025); border-radius: 10px; border: 1px solid transparent; }
.cat-item.selected { border-color: var(--p); background: rgba(0,242,255,0.06); }
.alert { padding: 14px 18px; border-radius: 12px; margin-bottom: 20px; font-weight: 600; text-align: center; animation: fadeIn 0.4s; }
.alert-error { background: rgba(255,0,0,0.15); border: 1px solid var(--danger); color: var(--danger); }
.alert-success { background: rgba(0,200,83,0.12); border: 1px solid var(--success); color: #b2ff59; }
.manage-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 8px; }
.ch-box { display: flex; align-items: center; gap: 10px; padding: 10px 12px; background: rgba(255,255,255,0.03); border-radius: 10px; font-size: 13px; cursor: pointer; }
.ch-box:hover { background: rgba(255,255,255,0.06); }
#log-panel { background: #000; border-radius: 14px; padding: 16px; font-family: monospace; font-size: 12px; max-height: 400px; overflow-y: auto; color: #bbb; line-height: 1.6; }
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="tr"><head><meta charset="UTF-8"><title>VİPEMU HUNTER ELITE</title>{css}</head><body>
<div class="dash">
<h1>⚡ VİPEMU HUNTER ELITE</h1>
{alerts}
<div class="card">
<h3 style="margin-top:0; color:var(--p)">Elite Portal Yönetimi</h3>
<div style="display:flex; flex-direction:column; gap:14px;">
<div style="display:flex; gap:12px; flex-wrap:wrap;">
<input id="p_url" type="text" placeholder="Stalker Portal URL (http://...)" style="flex:1; min-width:260px;">
<input id="p_mac" type="text" placeholder="MAC Adresi (00:1A:79:...)" style="width:240px;">
<button class="btn" onclick="checkPortal(event)">KONTROL ET</button>
</div>
<div id="selection_area" style="display:none; animation:fadeIn 0.4s;">
<div id="cat_list" style="max-height:420px; overflow-y:auto; margin:16px 0; padding:18px; background:rgba(0,0,0,0.3); border-radius:14px; border:1px solid var(--border);"></div>
<div style="display:flex; align-items:center; gap:16px;">
<button class="btn btn-green" onclick="addPortal(event)">✓ SEÇİLENLERİ EKLE</button>
<button class="btn btn-secondary" onclick="resetForm()">İPTAL</button>
</div>
</div>
</div>
</div>
{global_m3u}
<div id="portals">{portal_rows}</div>
<div class="card" style="margin-top:20px;">
<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
<h3 style="margin:0; color:var(--p);">📋 Canlı Loglar</h3>
<button class="btn btn-secondary" onclick="refreshLogs()" style="padding:8px 16px;">YENİLE</button>
</div>
<div id="log-panel">Yükleniyor...</div>
</div>
</div>
<script>
async function checkPortal(e) {
    const btn = e.target;
    const url = document.getElementById('p_url').value.trim();
    const mac = document.getElementById('p_mac').value.trim();
    if (!url || !mac) return alert('URL ve MAC giriniz!');
    btn.innerHTML = '<span class="spinner"></span> KONTROL EDİLİYOR...';
    btn.disabled = true;
    try {
        const r = await fetch(`/check?url=${encodeURIComponent(url)}&mac=${encodeURIComponent(mac)}`);
        const d = await r.json();
        if (d.status === 'ok') {
            renderCategories(d.categories);
            document.getElementById('selection_area').style.display = 'block';
        } else {
            alert('Handshake hatası: ' + (d.error || 'Bilinmiyor'));
        }
    } catch(ex) {
        alert('Bağlantı hatası!' + ex);
    } finally {
        btn.innerHTML = 'KONTROL ET';
        btn.disabled = false;
    }
}
function renderCategories(cats) {
    const labels = {itv:'CANLI TV'};
    let html = '';
    let any = false;
    for (const type in cats) {
        if (!cats[type] || !cats[type].length) continue;
        any = true;
        html += `<div style="margin-bottom:20px; border:1px solid var(--border); border-radius:14px; padding:14px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; flex-wrap:wrap; gap:8px;">
                <h4 style="color:var(--p); margin:0;">${labels[type]||type} <span style="opacity:.5;font-size:12px;" id="cnt-${type}">(${cats[type].length})</span></h4>
                <div style="display:flex; gap:12px; align-items:center;">
                    <input type="text" onkeyup="filterCats('${type}',this.value)" placeholder="Kategori ara..." style="padding:7px 14px; border-radius:20px; font-size:13px; width:180px;">
                    <label style="font-size:12px; cursor:pointer; display:flex; align-items:center; gap:7px; background:rgba(0,242,255,0.05); padding:6px 12px; border-radius:30px; border:1px solid rgba(0,242,255,.2);">
                         <input type="checkbox" class="all-check" data-for="${type}" onchange="toggleAll('${type}',this.checked)"><b>TÜMÜNÜ SEÇ</b></label></div></div>
            <div class="manage-grid" id="grid-${type}">`;
        cats[type].forEach(c => {
            const t = c.title.toLowerCase().replace(/"/g,'&quot;');
            const isAll = (t==='all'||t==='tümü'||t==='tüm kategoriler');
            html += `<label class="cat-item" data-title="${t}" onclick="syncHeader('${type}')">
                <input type="checkbox" class="cc check-${type}" data-isall="${isAll}" data-type="${type}" data-id="${c.id}" onchange="onCatChange('${type}',this)" style="width:15px;height:15px;">
                <span title="${c.title}" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${c.title}</span></label>`;
        });
        html += `</div></div>`;
    }
    document.getElementById('cat_list').innerHTML = any ? html : '<div style="text-align:center;padding:40px;opacity:.5;">PORTALDA İÇERİK BULUNAMADI.</div>';
}
function filterCats(type, q) {
    q = q.toLowerCase().trim();
    let vis = 0;
    document.querySelectorAll(`#grid-${type} .cat-item`).forEach(el => {
        const show = el.dataset.title.includes(q);
        el.style.display = show ? '' : 'none';
        if (show) vis++;
    });
    const cnt = document.getElementById(`cnt-${type}`);
    if (cnt) cnt.textContent = `(${vis})`;
}
function toggleAll(type, checked) {
    document.querySelectorAll(`#grid-${type} .cc`).forEach(cb => {
        cb.checked = checked;
        cb.closest('.cat-item').classList.toggle('selected', checked);
    });
}
function onCatChange(type, cb) {
    if (cb.dataset.isall === 'true') { toggleAll(type, cb.checked); return; }
    cb.closest('.cat-item').classList.toggle('selected', cb.checked);
    syncHeader(type);
}
function syncHeader(type) {
    const cbs = [...document.querySelectorAll(`#grid-${type} .cc`)];
    const mc = document.querySelector(`.all-check[data-for="${type}"]`);
    if (mc) mc.checked = cbs.length > 0 && cbs.every(c => c.checked);
}
async function addPortal(e) {
    const btn = e.target;
    const url = document.getElementById('p_url').value.trim();
    const mac = document.getElementById('p_mac').value.trim();
    const sel = {itv:[], video:[], series:[]};
    document.querySelectorAll('.cc:checked').forEach(cb => sel[cb.dataset.type].push(cb.dataset.id));
    if (!url || !mac) return alert('HATA: URL ve MAC boş olamaz!');
    if (!sel.itv.length) return alert('HATA: En az bir CANLI TV kategorisi seçiniz!');
    btn.innerHTML = '<span class="spinner"></span> EKLENİYOR...';
    btn.disabled = true;
    try {
        const r = await fetch('/add_portal', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url, mac, selected: sel})});
        const res = await r.json();
        if (r.ok) { window.location.href = '/?success=1'; }
        else alert('HATA: ' + (res.error || 'Portal eklenemedi.'));
    } catch(ex) {
        alert('BAĞLANTI HATASI: Sunucuya ulaşılamıyor!');
    } finally {
        btn.innerHTML = '✓ SEÇİLENLERİ EKLE';
        btn.disabled = false;
    }
}
function resetForm() {
    document.getElementById('selection_area').style.display = 'none';
    document.getElementById('p_url').value = '';
    document.getElementById('p_mac').value = '';
}
async function refreshLogs() {
    try {
        const r = await fetch('/logs');
        const d = await r.json();
        const panel = document.getElementById('log-panel');
        panel.innerHTML = d.lines.map(l => {
            let color = '#bbb';
            if (l.includes('ERROR') || l.includes('BAŞARISIZ') || l.includes('hatası')) color = '#ff6b6b';
            else if (l.includes('INFO') && (l.includes('✓') || l.includes('TAMAMLANDI') || l.includes('OK'))) color = '#69db7c';
            else if (l.includes('WARNING') || l.includes('WARN')) color = '#fcc419';
            return `<div style="color:${color}">${escHtml(l)}</div>`;
        }).join('');
        panel.scrollTop = panel.scrollHeight;
    } catch(e) { document.getElementById('log-panel').textContent = 'Log alınamadı.'; }
}
function escHtml(t) { return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
document.addEventListener('DOMContentLoaded', () => { refreshLogs(); setInterval(refreshLogs, 8000); });
</script>
</body></html>
"""

MANAGE_HTML = """
<!DOCTYPE html>
<html lang="tr"><head><meta charset="UTF-8"><title>Kanal Yönetimi</title>{css}</head><body>
<div class="dash">
<h1>📺 Kanal Yönetimi</h1>
<div class="alert alert-success">{portal}</div>
<button class="btn btn-green" onclick="saveChannels('{pid}')">💾 KAYDET</button>
<button class="btn btn-secondary" onclick="toggleAllCh(!document.getElementById('all-cb').checked)">Tümünü Göster/Gizle</button>
<a href="/" class="btn btn-secondary" style="margin-left:auto;">[← GERİ]</a>
<div class="manage-grid" style="margin-top:20px;">{ch_rows}</div>
</div>
<script>
function filterCh(q) {
    q = q.toLowerCase();
    document.querySelectorAll('.ch-box').forEach(el => {
        el.style.display = el.dataset.name.includes(q) ? '' : 'none';
    });
    syncAllCb();
}
function toggleAllCh(v) {
    document.querySelectorAll('.ch-sel').forEach(cb => cb.checked = v);
}
function syncAllCb() {
    const all = [...document.querySelectorAll('.ch-sel')];
    document.getElementById('all-cb').checked = all.length > 0 && all.every(c => c.checked);
}
async function saveChannels(pid) {
    const disabled = [...document.querySelectorAll('.ch-sel')].filter(c => !c.checked).map(c => c.dataset.id);
    const r = await fetch('/save_channels', {method:'POST', body: JSON.stringify({pid, disabled}), headers:{'Content-Type':'application/json'}});
    if (r.ok) alert('Kaydedildi!'); else alert('Kayıt hatası!');
}
document.addEventListener('DOMContentLoaded', syncAllCb);
</script>
</body></html>
"""


class IPTVHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpegurl")
        self.end_headers()

    def do_GET(self):
        url_obj = urlparse(self.path)
        path = url_obj.path
        qs = {k: v[0] for k, v in parse_qs(url_obj.query).items()}
        try:
            if path == "/":
                self._handle_dashboard(qs)
            elif path == "/check":
                self._handle_check(qs)
            elif path == "/add":
                self._redirect("/")
            elif path == "/del":
                self._handle_del(qs)
            elif path == "/m3u_plus":
                self._handle_m3u(qs)
            elif path == "/manage":
                self._handle_manage(qs)
            elif path == "/play":
                self._handle_play(qs)
            elif path == "/status":
                self._handle_status()
            elif path == "/logs":
                self._handle_logs()
            else:
                self.send_error(404)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass
        except OSError as exc:
            if exc.errno not in (10053, 10054, 32):
                log.error("Handler hatası [%s]: %s", path, exc)
        except Exception as exc:
            log.error("Handler hatası [%s]: %s", path, exc)
            try:
                self.send_error(500)
            except Exception:
                pass

    def do_POST(self):
        url_obj = urlparse(self.path)
        path = url_obj.path
        try:
            if path == "/add_portal":
                self._handle_add_portal()
            elif path == "/save_channels":
                self._handle_save_channels()
            else:
                self.send_error(404)
        except Exception as exc:
            log.error("POST handler hatası [%s]: %s", path, exc)
            try:
                self.send_error(500)
            except Exception:
                pass

    def _send_html(self, body: str, status: int = 200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj: dict, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _handle_dashboard(self, qs: dict):
        alerts = ""
        if qs.get("error"):
            alerts = '<div class="alert alert-error">⚠ Portal bağlantısı kurulamadı. URL ve MAC adresini kontrol edin.</div>'
        if qs.get("success"):
            alerts = '<div class="alert alert-success">✓ Portal başarıyla eklendi! Senkronizasyon arka planda devam ediyor.</div>'
        rows = ""
        active = 0
        host = self.headers.get("Host", "localhost")
        for pid, p in PORTALS.items():
            ch_cnt = len(p.channels)
            if p.is_syncing:
                elapsed = time.time() - p._sync_started_at
                rows += f'<div class="portal-row" style="opacity:.85;"><div><span class="badge badge-warn">SYNCING</span>&nbsp;<b style="font-size:17px">{html.escape(p.portal)}</b><br><small style="opacity:.55">{p.mac}</small></div><div style="display:flex; align-items:center; gap:14px; flex-wrap:wrap;"><span style="color:var(--warn); font-weight:600;"><span class="spinner"></span>{ch_cnt} kanal · {elapsed:.0f} sn</span><a href="/del?id={pid}" style="color:var(--danger); font-size:13px; font-weight:700;">SİL</a></div></div>'
            else:
                active += 1
                m3u = f"http://{host}/m3u_plus?id={pid}"
                rows += f'<div class="portal-row"><div><span class="badge">ACTIVE</span>&nbsp;<b style="font-size:17px">{html.escape(p.portal)}</b><br><small style="opacity:.55">{p.mac}</small></div><div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;"><span style="font-weight:600;">{ch_cnt} kanal</span><a href="/manage?id={pid}" class="btn btn-secondary" style="padding:8px 14px;">YÖNET</a><a href="{m3u}" class="btn" style="padding:8px 14px;">M3U+</a><a href="/del?id={pid}" style="color:var(--danger); font-size:13px; font-weight:700;">SİL</a></div></div>'
        global_m3u = ""
        if active > 0:
            gl = f"http://{host}/m3u_plus"
            global_m3u = f'<div style="text-align:center; margin:18px 0;"><a href="{gl}" class="btn btn-green" style="font-size:15px; padding:15px 30px;"> GLOBAL M3U PLUS (TÜM PORTALLARI BİRLEŞTİR)</a></div>'
        auto_reload = ""
        for pid2, p2 in PORTALS.items():
            if p2.is_syncing and (time.time() - p2._sync_started_at) < MAG_SYNC_TIMEOUT:
                auto_reload = "<script>setTimeout(()=>location.href='/',5000);</script>"
                break
        page_html = (
            DASHBOARD_HTML.replace("{css}", f"<style>{CSS}</style>")
            .replace("{alerts}", alerts)
            .replace("{global_m3u}", global_m3u)
            .replace("{portal_rows}", rows)
        ) + auto_reload
        self._send_html(page_html)

    def _handle_check(self, qs: dict):
        ip = self.client_address[0]
        if not _rate_limit_check(ip):
            self._send_json(
                {"status": "error", "error": "Çok sık istek. Lütfen bekleyin."}, 429
            )
            return
        url = qs.get("url", "")
        mac = qs.get("mac", "")
        if not url or not mac:
            self._send_json({"status": "error", "error": "url ve mac gerekli"}, 400)
            return
        if not _is_safe_url(url):
            self._send_json(
                {"status": "error", "error": "Geçersiz veya güvenli olmayan URL."}, 400
            )
            return
        client = STBClient(url, mac)
        if client.handshake():
            cats = {"itv": client.get_categories("itv")}
            self._send_json({"status": "ok", "categories": cats})
        else:
            self._send_json({"status": "error", "error": "Bağlantı kurulamadı"})

    def _handle_add_portal(self):
        try:
            cl = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(cl).decode())
            url, mac, sel = data.get("url"), data.get("mac"), data.get("selected", {})
            if not url or not mac:
                self._send_json({"error": "url ve mac boş olamaz"}, 400)
                return
            if not _is_safe_url(url):
                self._send_json({"error": "Geçersiz URL"}, 400)
                return
            p_id = str(int(time.time()))
            types = ["itv"]
            if init_portal(p_id, url, mac, types, sel):
                cfg = load_config()
                cfg["portals"].append(
                    {
                        "id": p_id,
                        "url": url,
                        "mac": mac,
                        "types": types,
                        "filter_cats": sel,
                    }
                )
                save_config(cfg)
                self._send_json({"status": "ok"})
            else:
                self._send_json(
                    {"error": "Portal handshake başarısız. MAC bu portalda aktif mi?"},
                    500,
                )
        except Exception as exc:
            log.error("add_portal hatası: %s", exc)
            self._send_json({"error": str(exc)}, 500)

    def _handle_del(self, qs: dict):
        p_id = qs.get("id", "")
        loop = MAG_LOOPS.pop(p_id, None)
        if loop:
            loop.stop()
        with HUBS_LOCK:
            dead = [k for k in STREAM_HUBS if k.startswith(f"{p_id}:")]
            for k in dead:
                hub = STREAM_HUBS.pop(k)
                hub.stop()
        PORTALS.pop(p_id, None)
        cfg = load_config()
        cfg["portals"] = [p for p in cfg["portals"] if p["id"] != p_id]
        save_config(cfg)
        log.info("[*] Portal silindi: %s", p_id)
        self._redirect("/")

    def _handle_m3u(self, qs: dict):
        host = self.headers.get("Host", "localhost")
        p_id = qs.get("id")
        if p_id:
            p = PORTALS.get(p_id)
            if not p:
                self.send_error(404)
                return
            portals_to_export = [(p_id, p)]
        else:
            portals_to_export = list(PORTALS.items())
        cfg = load_config()
        lines = ['#EXTM3U x-tvg-url=""']
        for pid, p in portals_to_export:
            portal_cfg = next((x for x in cfg["portals"] if x["id"] == pid), {})
            disabled = set(portal_cfg.get("disabled_channels", []))
            for ch_id, ch in p.channels.items():
                if ch_id in disabled:
                    continue
                name = str(ch.get("name", "")).replace('"', "")
                cat = str(ch.get("_cat", "")).replace('"', "")
                logo = str(ch.get("logo", ch.get("pic", ""))).replace('"', "")
                s_url = f"http://{host}/play?p={pid}&id={ch_id}"
                lines.append(
                    f'#EXTINF:-1 tvg-id="{name}" tvg-name="{name}" tvg-logo="{logo}" group-title="{cat}",{name}\n{s_url}'
                )
        content = "\n".join(lines).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpegurl")
        self.send_header("Content-Disposition", 'inline; filename="playlist.m3u"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _handle_manage(self, qs: dict):
        p_id = qs.get("id", "")
        p = PORTALS.get(p_id)
        if not p:
            self.send_error(404)
            return
        cfg = load_config()
        portal_cfg = next((x for x in cfg["portals"] if x["id"] == p_id), {})
        disabled = set(portal_cfg.get("disabled_channels", []))
        rows = ""
        for ch_id, ch in p.channels.items():
            checked = "checked" if ch_id not in disabled else ""
            name = html.escape(str(ch.get("name", "")))
            cat = html.escape(str(ch.get("_cat", "")))
            rows += f'<label class="ch-box" data-name="{name.lower()}"><input type="checkbox" class="ch-sel" data-id="{ch_id}" onchange="syncAllCb()" {checked}><span title="{name}" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{name}</span><small style="opacity:.45;margin-left:auto;">({cat})</small></label>'
        page_html = (
            MANAGE_HTML.replace("{css}", f"<style>{CSS}</style>")
            .replace("{pid}", p_id)
            .replace("{portal}", html.escape(p.portal))
            .replace("{ch_rows}", rows)
        )
        self._send_html(page_html)

    def _handle_save_channels(self):
        try:
            cl = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(cl).decode())
            pid, disabled = data.get("pid"), data.get("disabled", [])
            cfg = load_config()
            for p in cfg["portals"]:
                if p["id"] == pid:
                    p["disabled_channels"] = disabled
                    break
            save_config(cfg)
            self.send_response(200)
            self.end_headers()
        except Exception as exc:
            log.error("save_channels hatası: %s", exc)
            self.send_response(500)
            self.end_headers()

    def _handle_play(self, qs: dict):
        p_id = qs.get("p", "")
        ch_id = qs.get("id", "")
        p = PORTALS.get(p_id)
        if not p or not ch_id:
            self.send_error(404)
            return
        ch = p.channels.get(ch_id)
        if not ch:
            self.send_error(404)
            return
        hub = get_or_create_hub(p_id, ch_id, p, ch)
        cur_seq = hub.subscribe()
        log.info(
            "[HUB] İzleyici bağlandı: %s (%d toplam) ← %s",
            hub.key,
            hub.subs,
            self.client_address[0],
        )
        try:
            if not hub.wait_for_data(timeout=15.0):
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Icy-MetaData", "0")
            self.end_headers()
            pat_pmt = hub.get_pat_pmt()
            if pat_pmt:
                self.wfile.write(pat_pmt)
                self.wfile.flush()
            while hub.running:
                data, next_seq = hub.read_from(cur_seq, timeout=5.0)
                if data is None:
                    break
                if not data:
                    continue
                self.wfile.write(data)
                self.wfile.flush()
                cur_seq = next_seq
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except OSError as exc:
            if exc.errno not in (10053, 10054, 32):
                log.debug("Play handler OSError: %s", exc)
        except Exception as exc:
            log.debug("Play handler Hatası: %s", exc)
        finally:
            hub.unsubscribe()
            log.info("[HUB] İzleyici ayrıldı: %s (%d kalan)", hub.key, hub.subs)

    def _handle_status(self):
        rows = []
        with HUBS_LOCK:
            for key, hub in STREAM_HUBS.items():
                rows.append(
                    {
                        "key": key,
                        "running": hub.running,
                        "subscribers": hub.subs,
                        "total_mb": round(hub.total_bytes / 1_048_576, 2),
                        "uptime_sn": round(time.time() - hub.started_at),
                        "ring_chunks": len(hub.ring),
                        "live_seq": hub.seq,
                    }
                )
        self._send_json(
            {"portals": len(PORTALS), "active_hubs": len(rows), "hubs": rows}
        )

    def _handle_logs(self):
        self._send_json({"lines": list(_LOG_BUFFER)})


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    log.info("=" * 60)
    log.info("  VİPEMU HUNTER ELITE — Geliştirme Versiyonu")
    log.info("  Windows & Linux Uyumlu | SADECE LIVE TV")
    log.info("=" * 60)
    cfg = load_config()
    port = cfg.get("port", DEFAULT_PORT)
    log.info("[*] Portallar yükleniyor...")
    for p in cfg.get("portals", []):
        threading.Thread(
            target=init_portal,
            args=(
                p["id"],
                p["url"],
                p["mac"],
                p.get("types", ["itv"]),
                p.get("filter_cats"),
            ),
            daemon=True,
        ).start()
    server = ThreadedHTTPServer(("0.0.0.0", port), IPTVHandler)
    log.info("[+] Sunucu başlatıldı: http://0.0.0.0:%d", port)
    log.info("[+] Dashboard: http://localhost:%d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("[*] Sunucu kapatılıyor...")
        server.server_close()


if __name__ == "__main__":
    main()
