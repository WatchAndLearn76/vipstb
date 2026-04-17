"""
VİPEMUHUNTER — Birleşik STB İstemcisi (Elite Düzeltme)
MAG250/254 protokolünü tam olarak emüle eder.
Özel Düzeltme: backup.xp1.tv gibi katı portallar için HTTP 462 çözümü ve akıllı token yenileme.
"""

import hashlib
import json
import logging
import re
import threading
import time
from urllib.parse import urlparse
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger("stb_client")


class STBClient:
    """
    Gerçek MAG250/254 cihazının portal iletişimini tam olarak taklit eder.
    Protokol Sırası:
        Başlangıç : handshake → get_profile → get_servertime
        Döngü     : watchdog + get_events (her 25 sn)
        Yenileme  : handshake (her 8 dk)
    """

    UA = (
        "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
        "(KHTML, like Gecko) MAG200 stbapp ver: 4 rev: 1812 Mobile Safari/533.3"
    )
    STB_TYPE = "MAG254"
    IMAGE_VERSION = "218"

    # Handshake için denenecek URL kalıpları
    _URL_PATTERNS = [
        "{portal}",
        "{base}/c/",
        "{base}/portal.php",
        "{base}/stalker_portal/c/",
        "{base}/stalker_portal/server/load.php",
        "{base}/mag/c/",
    ]

    def __init__(self, portal_url: str, mac_address: str):
        self.portal = portal_url.rstrip("/")
        self.mac = mac_address.upper()
        parsed = urlparse(self.portal)
        self.base = f"{parsed.scheme}://{parsed.netloc}"

        # MAG kimlik hesaplama (resmi algoritma)
        self.sn, self.device_id, self.signature = self._calculate_identity()

        self.token = ""
        self.active_url = self.portal

        self.session = requests.Session()
        self.session.verify = False

        # Temel Header'lar
        self._headers = {
            "User-Agent": self.UA,
            "X-User-Agent": f"Model: {self.STB_TYPE}; Link: Ethernet",
            "Cookie": f"mac={self.mac}; stb_lang=en; timezone=Europe/Amsterdam",
        }

        # Thread güvenliği
        self._lock = threading.Lock()

        # Kanal deposu
        self.channels: dict = {}
        self.last_sync: float = 0.0
        self.is_syncing: bool = False
        self._sync_started_at: float = 0.0
        self.filter_cats = None
        self._hb_running = False

    def _calculate_identity(self):
        """Resmi MAG algoritmasına göre SN / device_id / signature üret."""
        sn_raw = hashlib.md5(self.mac.encode()).hexdigest().upper()
        sn = sn_raw[:13]
        device_id = hashlib.sha256(self.mac.encode()).hexdigest().upper()[:32]
        signature = hashlib.sha256((sn + self.mac).encode()).hexdigest().upper()
        return sn, device_id, signature

    def _parse(self, text: str) -> dict:
        """JSON yanıtı güvenli şekilde çözümle."""
        try:
            if "/*-secure-" in text:
                text = text.replace("/*-secure-", "").replace("*/", "")
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                return json.loads(m.group(0))
        except Exception:
            pass
        return {}

    def _clean_url(self, url, fallback_cmd="") -> str:
        """URL'yi temizle: ffmpeg/ffrt prefix sil, localhost→domain, mac düzelt."""
        if not url or not isinstance(url, str):
            for part in str(fallback_cmd).split():
                if "http" in part:
                    return part
            return str(fallback_cmd)

        # Eğer URL zaten http ile başlıyorsa, sadece MAC güncellemesi yap ve dön
        # backup.xp1.tv gibi portallar için cmd bazen direkt URL'dir, bozmamak lazım!
        if url.startswith("http"):
            if "mac=" in url:
                url = re.sub(r"mac=[^&]+", f"mac={self.mac}", url)
            return url

        # Prefixleri temizle
        if url.startswith("ffmpeg "):
            url = url[7:].strip()
        if url.startswith("ffrt "):
            url = url[5:].strip()

        # Localhost veya IP sorunlarını çöz
        if "localhost" in url:
            url = url.replace("http://localhost", self.base)
        elif "127.0.0.1" in url:
            url = url.replace("http://127.0.0.1", self.base)

        # MAC adresini güncelle
        if "mac=" in url:
            url = re.sub(r"mac=[^&]+", f"mac={self.mac}", url)

        return url

    def _safe_get(
        self, url: str, params: dict = None, timeout: int = 10, headers: dict = None
    ):
        """Thread-safe ve Keep-Alive kopmalarına karşı dirençli GET isteği."""
        req_headers = headers if headers is not None else self._headers
        resp = None
        try:
            resp = self.session.get(
                url, params=params, headers=req_headers, timeout=timeout
            )
            return resp
        except Exception as exc:
            if resp is not None:
                return resp
            err_str = str(exc).lower()
            if (
                "remotedisconnected" in err_str
                or "aborted" in err_str
                or "reset" in err_str
            ):
                log.debug(
                    "[MAG] Keep-Alive koptu, yeni oturumla tekrar deneniyor: %s", url
                )
                self.session.close()
                self.session = requests.Session()
                self.session.verify = False
                return self.session.get(
                    url, params=params, headers=req_headers, timeout=timeout
                )
            raise

    def _req(self, params: dict, timeout: int = 10, headers: dict = None) -> dict:
        """Thread-safe portal API isteği."""
        with self._lock:
            if not self.active_url:
                return {}
            try:
                r = self._safe_get(
                    self.active_url, params=params, timeout=timeout, headers=headers
                )
                return self._parse(r.text)
            except Exception as exc:
                log.debug(
                    "Portal isteği hatası [%s]: %s", params.get("action", "?"), exc
                )
                return {}

    def handshake(self) -> bool:
        """Token al. Double-handshake ile her portal tipini destekler."""
        candidates = [
            p.format(portal=self.portal, base=self.base) for p in self._URL_PATTERNS
        ]

        with self._lock:
            for url in candidates:
                try:
                    # 1. Handshake isteği gönder
                    r1 = self._safe_get(
                        url,
                        params={
                            "type": "stb",
                            "action": "handshake",
                            "token": "",
                            "JsHttpRequest": "1-xml",
                        },
                        timeout=10,
                    )
                    try:
                        text1 = r1.text
                    except Exception:
                        text1 = (r1.content or b"").decode("utf-8", errors="replace")

                    data1 = self._parse(text1)
                    token1 = data1.get("js", {}).get("token")

                    if not token1:
                        continue

                    self.token = token1
                    self.active_url = url
                    self._headers["Authorization"] = f"Bearer {token1}"

                    # 2. Double-handshake (token onayla)
                    try:
                        r2 = self._safe_get(
                            url,
                            params={
                                "type": "stb",
                                "action": "handshake",
                                "token": "",
                                "JsHttpRequest": "1-xml",
                            },
                            timeout=10,
                        )
                        try:
                            text2 = r2.text
                        except Exception:
                            text2 = (r2.content or b"").decode(
                                "utf-8", errors="replace"
                            )

                        data2 = self._parse(text2)
                        token2 = data2.get("js", {}).get("token")

                        if token2:
                            self.token = token2
                            self._headers["Authorization"] = f"Bearer {token2}"
                            log.info(
                                "[MAG] Double Handshake OK (%s): %s...",
                                url,
                                token2[:10],
                            )
                        else:
                            log.info(
                                "[MAG] Single Handshake OK (%s): %s...",
                                url,
                                token1[:10],
                            )
                    except Exception:
                        log.info(
                            "[MAG] Single Handshake OK (%s): %s...", url, token1[:10]
                        )

                    self._get_profile_locked(url)
                    return True
                except Exception as exc:
                    log.debug("[MAG] Handshake denemesi başarısız [%s]: %s", url, exc)

        log.warning("[MAG] Handshake BAŞARISIZ: %s", self.portal)
        return False

    def _get_profile_locked(self, url: str):
        """Handshake sırasında çağrılır — kilit zaten açık olduğu için ayrı metot."""
        try:
            ver_str = (
                f"ImageDescription: 0.2.18-r14-pub-254; ImageDate: Fri Jan 15 2021 00:00:00 GMT+0200; "
                f"PORTAL version: 5.6.1; API Version: JS API version: 343; STB API version: 146; Player Engine version: 0x58c"
            )
            self.session.get(
                url,
                params={
                    "type": "stb",
                    "action": "get_profile",
                    "hd": "1",
                    "ver": ver_str,
                    "num_banks": "2",
                    "sn": self.sn,
                    "stb_type": self.STB_TYPE,
                    "image_version": self.IMAGE_VERSION,
                    "hw_version": "1.7-BD-00",
                    "device_id": self.device_id,
                    "device_id2": self.device_id,
                    "signature": self.signature,
                    "JsHttpRequest": "1-xml",
                },
                headers=self._headers,
                timeout=10,
            )
        except Exception:
            pass

    def get_profile(self) -> dict:
        return self._req(
            {
                "type": "stb",
                "action": "get_profile",
                "hd": "1",
                "sn": self.sn,
                "stb_type": self.STB_TYPE,
                "image_version": self.IMAGE_VERSION,
                "hw_version": "1.7-BD-00",
                "device_id": self.device_id,
                "device_id2": self.device_id,
                "signature": self.signature,
                "JsHttpRequest": "1-xml",
            },
            timeout=15,
        )

    def get_servertime(self) -> dict:
        return self._req(
            {"type": "stb", "action": "get_servertime", "JsHttpRequest": "1-xml"}
        )

    def watchdog(self) -> dict:
        return self._req(
            {
                "type": "stb",
                "action": "watchdog",
                "cur_play_type": "1",
                "content_id": "0",
                "JsHttpRequest": "1-xml",
            }
        )

    def get_events(self) -> dict:
        data = self._req(
            {"type": "stb", "action": "get_events", "JsHttpRequest": "1-xml"}
        )
        js = data.get("js", {})
        if isinstance(js, dict):
            if js.get("reboot"):
                log.info(
                    "[MAG] Portal reboot istedi, session tazeleniyor: %s", self.portal
                )
                self.handshake()
            cmd = js.get("cmd", "")
            if cmd and cmd not in ("reset", "reboot", "update"):
                log.debug("[MAG] Portal komutu: %s — %s", cmd, self.portal)
        return data

    def get_categories(self, ctype: str = "itv") -> list:
        with self._lock:
            for action in ("get_categories", "get_genres"):
                try:
                    r = self.session.get(
                        self.active_url,
                        params={
                            "type": ctype,
                            "action": action,
                            "JsHttpRequest": "1-xml",
                        },
                        headers=self._headers,
                        timeout=10,
                    )
                    data = self._parse(r.text).get("js", [])
                    if data:
                        return data
                except Exception:
                    continue
        return []

    def get_all_channels(self) -> list:
        data = self._req(
            {"type": "itv", "action": "get_all_channels", "JsHttpRequest": "1-xml"},
            timeout=20,
        )
        return data.get("js", {}).get("data", [])

    def get_ordered_list(self, ctype: str, cat_id, page: int = 1) -> list:
        params = {
            "type": ctype,
            "action": "get_ordered_list",
            "p": page,
            "JsHttpRequest": "1-xml",
        }
        if ctype == "itv":
            params["genre"] = cat_id
        else:
            params["category"] = cat_id
        try:
            with self._lock:
                r = self.session.get(
                    self.active_url, params=params, headers=self._headers, timeout=12
                )
                return self._parse(r.text).get("js", {}).get("data", [])
        except Exception:
            return []

    def get_all_items(self, ctype: str, cat_id) -> list:
        items = []
        page = 1
        empty = 0
        while True:
            data = self.get_ordered_list(ctype, cat_id, page)
            if not data:
                empty += 1
                if empty > 2:
                    break
                page += 1
                continue
            items.extend(data)
            empty = 0
            if len(data) < 10:
                break
            page += 1
            if page > 100:
                break
        return items

    def create_link(self, cmd: str, ctype: str = "itv") -> str:
        """
        Portal API ile taze stream linki üret.
        """

        p_cmd = cmd if cmd.startswith("ffmpeg ") else f"ffmpeg {cmd}"

        # Özel Header seti (Referer/Origin ekliyoruz)
        special_headers = self._headers.copy()
        special_headers["Referer"] = f"{self.portal}/"
        special_headers["Origin"] = self.base

        with self._lock:
            try:
                r = self.session.get(
                    self.active_url,
                    params={
                        "type": ctype,
                        "action": "create_link",
                        "cmd": p_cmd,
                        "JsHttpRequest": "1-xml",
                    },
                    headers=special_headers,
                    timeout=10,
                )

                if r.status_code != 200:
                    log.debug(f"[MAG] create_link HTTP {r.status_code} döndü.")
                    # Hata durumunda yine de cmd'yi dene (belki direkt URL'dir)
                    return self._clean_url(cmd, "")

                api_url = self._parse(r.text).get("js", {}).get("cmd", "")
                cleaned = self._clean_url(api_url, cmd)

                if cleaned and "http" in cleaned:
                    m = re.search(r"[?&]stream=([^&]*)", cleaned)
                    if m and (m.group(1) == "" or len(m.group(1)) < 5):
                        log.debug("[MAG] create_link stream= boş/geçersiz. Fallback.")
                        return self._clean_url(cmd, "")  # Orijinal cmd'e dön
                    return cleaned

            except Exception as e:
                log.debug(f"[MAG] create_link exception: {e}")

        # Fallback: Orijinal cmd
        return self._clean_url(cmd, "")

    def get_fresh_link(self, ch: dict) -> str:
        cmd = ch.get("cmd", "")
        ctype = ch.get("_type", "itv")
        ch_name = ch.get("name", "")
        ch_id = ch.get("id", "")

        # 1. ADIM: Normal create_link dene
        cl_url = self.create_link(cmd, ctype)

        if cl_url and cl_url.startswith("http"):
            return cl_url

        log.warning(
            f"[MAG] get_fresh_link: create_link başarısız. Arama modu: {ch_name}"
        )

        # 3. ADIM: Portalda Ara (Taze token bul)
        if not ch_name:
            return ""

        try:
            search_params = {
                "type": ctype,
                "action": "get_ordered_list",
                "search": ch_name,
                "JsHttpRequest": "1-xml",
            }

            with self._lock:
                r = self.session.get(
                    self.active_url,
                    params=search_params,
                    headers=self._headers,
                    timeout=10,
                )

            data = self._parse(r.text).get("js", {}).get("data", [])

            fresh_cmd = None
            for item in data:
                if (
                    str(item.get("id")) == str(ch_id)
                    or item.get("name", "").lower() == ch_name.lower()
                ):
                    fresh_cmd = item.get("cmd", "")
                    log.info(f"[MAG] TAZE CMD BULUNDU: {fresh_cmd[:30]}...")
                    break

            if fresh_cmd:
                # Bulunan yeni cmd ile tekrar dene (bu sefer URL olabilir)
                final_url = self.create_link(fresh_cmd, ctype)
                if final_url and final_url.startswith("http"):
                    log.info(f"[MAG] Taze cmd ile link ÜRETİLDİ!")
                    ch["cmd"] = fresh_cmd  # Belleği güncelle
                    return final_url
                # Eğer hala URL yoksa, belki fresh_cmd kendisi URL'dir?
                if "http" in fresh_cmd:
                    return self._clean_url(fresh_cmd, "")

        except Exception as e:
            log.error(f"[MAG] Arama hatası: {e}")

        return ""

    def fetch_stream_url(self, item: dict, ctype: str) -> bool:
        cmd = item.get("cmd", "")
        if not cmd:
            return False
        url = self.create_link(cmd, ctype)
        if url and "http" in url:
            item["stream_url"] = url
            return True
        return False

    def verify_stream(self, cmd: str) -> str:
        dummy = {"cmd": cmd}
        self.fetch_stream_url(dummy, "itv")
        return dummy.get("stream_url", "")

    _SYNC_TIMEOUT = 20 * 60

    def sync_channels(self, ctypes=None, filter_cats=None):
        if ctypes is None:
            ctypes = ["itv"]
        if filter_cats is not None:
            self.filter_cats = filter_cats
        else:
            filter_cats = self.filter_cats

        if self.is_syncing:
            elapsed = time.time() - self._sync_started_at
            if elapsed > self._SYNC_TIMEOUT:
                log.warning(
                    "[!] %s sync timeout (%.0f sn), zorla sıfırlanıyor.",
                    self.portal,
                    elapsed,
                )
                self.is_syncing = False
            else:
                log.info(
                    "[!] %s zaten senkronize ediliyor, yeni işlem reddedildi.",
                    self.portal,
                )
                return

        self.is_syncing = True
        self._sync_started_at = time.time()
        log.info("[*] Kanal senkronizasyonu başladı: %s", self.portal)
        all_new = {}

        try:
            itv_cats = self.get_categories("itv")
            cat_map = {str(c.get("id")): c.get("title") for c in itv_cats}
            fast = self.get_all_channels()

            if fast:
                log.info("  [✓] Fast Sync: %d ham kanal alındı.", len(fast))
                has_filter = filter_cats is not None and "itv" in filter_cats
                itv_selected = (
                    [str(x) for x in filter_cats.get("itv", [])] if has_filter else []
                )
                if "*" in itv_selected:
                    has_filter = False

                has_cat_id = any(
                    str(item.get("category_id", "")).strip() for item in fast[:20]
                )

                if has_filter and not has_cat_id:
                    log.info(
                        "  [!] Fast Sync: kanallar category_id içermiyor. Klasik yönteme geçiliyor..."
                    )
                    fast = []
                else:
                    for item in fast:
                        cat_id = str(item.get("category_id", ""))
                        if has_filter and cat_id not in itv_selected:
                            continue
                        item["_cat"] = cat_map.get(cat_id, "Genel")
                        item["_type"] = "itv"
                        all_new[f"itv_{item['id']}"] = item

                    if not all_new and has_filter:
                        selected_titles = {
                            v.lower() for k, v in cat_map.items() if k in itv_selected
                        }
                        reverse_map = {}
                        for item in fast:
                            cid = str(item.get("category_id", ""))
                            if cid and cid not in reverse_map:
                                reverse_map[cid] = cat_map.get(cid, "").lower()
                        title_ids = {
                            k for k, v in reverse_map.items() if v in selected_titles
                        }
                        if title_ids:
                            log.info(
                                "  [~] İsim bazlı eşleşme: %d kategori ID.",
                                len(title_ids),
                            )
                            for item in fast:
                                cat_id = str(item.get("category_id", ""))
                                if cat_id not in title_ids:
                                    continue
                                item["_cat"] = cat_map.get(cat_id, "Genel")
                                item["_type"] = "itv"
                                all_new[f"itv_{item['id']}"] = item
                        else:
                            log.warning(
                                "  [!] ID ve isim eşleşmesi başarısız. Tüm kanallar alınıyor."
                            )
                            for item in fast:
                                cat_id = str(item.get("category_id", ""))
                                item["_cat"] = cat_map.get(cat_id, "Genel")
                                item["_type"] = "itv"
                                all_new[f"itv_{item['id']}"] = item

                if all_new:
                    log.info("  [✓] Fast Sync tamamlandı: %d kanal.", len(all_new))
                    self.channels = all_new
                    self.last_sync = time.time()
                    self.is_syncing = False
                    return

            log.info("  [!] Fast Sync veri döndürmedi, klasik yönteme geçiliyor.")
        except Exception as exc:
            log.warning("  [!] Fast Sync hatası: %s", exc)

        try:
            for ctype in ctypes:
                cats = self.get_categories(ctype)
                if filter_cats is not None and ctype in filter_cats:
                    sel = [str(i) for i in filter_cats[ctype]]
                    if "*" not in sel:
                        cats = [c for c in cats if str(c.get("id")) in sel]
                if not cats:
                    continue

                log.info("   > %s: %d kategori taranacak.", ctype.upper(), len(cats))

                for i, cat in enumerate(cats, 1):
                    cat_id = cat.get("id")
                    cat_title = cat.get("title", "?")
                    page = 1
                    empty_w = 0

                    while True:
                        params = {
                            "type": ctype,
                            "action": "get_ordered_list",
                            "p": page,
                            "JsHttpRequest": "1-xml",
                        }
                        if ctype == "itv":
                            params["genre"] = cat_id
                        else:
                            params["category"] = cat_id

                        try:
                            with self._lock:
                                r = self.session.get(
                                    self.active_url,
                                    params=params,
                                    headers=self._headers,
                                    timeout=12,
                                )
                            js = self._parse(r.text).get("js", {})
                            data = js.get("data", [])
                        except Exception:
                            break

                        if not data:
                            empty_w += 1
                            if empty_w > 1:
                                break
                            page += 1
                            continue

                        for item in data:
                            item["_cat"] = cat_title
                            item["_type"] = ctype
                            all_new[f"{ctype}_{item['id']}"] = item

                        self.channels.update(all_new)

                        if len(data) < 10 or page >= 100:
                            break
                        page += 1
                        empty_w = 0

                    if i % 5 == 0 or i == len(cats):
                        log.info(
                            "    [%s][%d/%d] '%s' bitti (top: %d)",
                            ctype.upper(),
                            i,
                            len(cats),
                            cat_title,
                            len(all_new),
                        )
        except Exception as exc:
            log.error("[!] sync_channels kritik hata: %s", exc)
        finally:
            self.channels = all_new
            self.last_sync = time.time()
            self.is_syncing = False
            log.info(
                "[+] Senkronizasyon TAMAMLANDI: %s → %d kanal.",
                self.portal,
                len(self.channels),
            )

    def start_heartbeat(self, interval: int = 15):
        if self._hb_running:
            return
        self._hb_running = True

        def _loop():
            fail = 0
            while self._hb_running:
                try:
                    self.get_profile()
                    self.get_events()
                    self.watchdog()
                    fail = 0
                except Exception:
                    fail += 1
                    if fail >= 5:
                        log.warning(
                            "[!] Heartbeat: Portal yanıt vermiyor, token yenileniyor..."
                        )
                        try:
                            self.handshake()
                            fail = 0
                        except Exception:
                            pass
                time.sleep(interval)

        t = threading.Thread(target=_loop, daemon=True, name=f"HB-{self.mac}")
        t.start()
        log.info("[+] Heartbeat başlatıldı (session canlı tutulacak)")

    def stop_heartbeat(self):
        self._hb_running = False

    @property
    def ua(self):
        return self.UA
