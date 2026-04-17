"""
VIPEMU HUNTER ELITE - Segment-Based Streaming Engine v2
Mimarı:
- Smart URL refresh (süre dolmadan)
- Seamless geçiş
- Session koruma
"""

import logging
import os
import shutil
import subprocess
import threading
import time
import requests
import urllib3
import re
from collections import deque

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger("stream_hub")

# Sabitler
READ_CHUNK = 188 * 64
RING_SIZE = 2000
URL_REFRESH_BEFORE_SEC = 8
HUB_IDLE_TIMEOUT = 3
HUB_MAX_FAIL = 15

STREAM_HUBS = {}
HUBS_LOCK = threading.Lock()
_FFMPEG_BIN = ""
_FFMPEG_LOCK = threading.Lock()


def _find_ffmpeg():
    try:
        import imageio_ffmpeg

        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except ImportError:
        pass

    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p:
            return p

    base = os.path.dirname(os.path.abspath(__file__))
    for name in ("ffmpeg.exe", "ffmpeg"):
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
    return ""


def get_ffmpeg():
    global _FFMPEG_BIN
    if _FFMPEG_BIN:
        return _FFMPEG_BIN
    with _FFMPEG_LOCK:
        if not _FFMPEG_BIN:
            _FFMPEG_BIN = _find_ffmpeg()
        return _FFMPEG_BIN


class StreamHub:
    def __init__(self, hub_key: str, p_client, ch_item: dict):
        self.key = hub_key
        self.p = p_client
        self.ch = ch_item
        self.cmd = ch_item.get("cmd", "")
        self.ctype = ch_item.get("_type", "itv")

        self.ring = deque(maxlen=RING_SIZE)
        self.seq = 0
        self.lock = threading.Condition(threading.Lock())
        self.running = False
        self.subs = 0
        self.last_sub = time.time()
        self.started_at = time.time()
        self.total_bytes = 0
        self._thread = None
        self._proc = None
        self._fail_count = 0
        self._last_url = ""
        self._url_generated_at = 0

    def start(self):
        self.running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"Hub-{self.key}"
        )
        self._thread.start()
        log.info("[HUB/v2] Başlatıldı: %s", self.key)

    def stop(self):
        self.running = False
        self._kill_proc()
        with HUBS_LOCK:
            STREAM_HUBS.pop(self.key, None)
        log.info("[HUB/v2] Kapatıldı: %s", self.key)

    def _kill_proc(self):
        proc = self._proc
        if proc:
            try:
                proc.stdin.close()
            except:
                pass
            try:
                proc.kill()
                proc.wait(timeout=2)
            except:
                pass
            self._proc = None

    def _portal_headers(self):
        h = {
            "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG250 stbapp ver: 4 rev: 1812 Mobile Safari/533.3",
            "X-User-Agent": "Model: MAG254; Link: Ethernet",
            "Cookie": f"mac={self.p.mac}; stb_lang=en; timezone=Europe/Amsterdam",
            "Accept": "*/*",
            "Referer": f"{self.p.portal}/",
            "Origin": self.p.base,
        }
        auth = getattr(self.p, "_headers", {}).get("Authorization")
        if auth:
            h["Authorization"] = auth
        return h

    def _get_url(self):
        try:
            url = self.p.create_link(self.cmd, self.ctype)
            if url and url.startswith("http"):
                self._last_url = url
                self._url_generated_at = time.time()
                return url
        except Exception as e:
            log.warning(f"[HUB/v2] URL alma hatası: {e}")
        return ""

    def _refresh_token(self):
        try:
            self.p.handshake()
            log.info("[HUB/v2] Token yenilendi")
        except Exception as e:
            log.warning(f"[HUB/v2] Token yenileme hatası: {e}")

    def _run(self):
        ffmpeg = get_ffmpeg()
        if not ffmpeg:
            log.error("[HUB/v2] FFmpeg bulunamadı!")
            return

        while self.running:
            if self.subs == 0 and (time.time() - self.last_sub) > HUB_IDLE_TIMEOUT:
                log.info("[HUB/v2] Abone yok, kapatılıyor")
                break

            url = self._get_url()
            if not url:
                self._fail_count += 1
                log.warning(
                    f"[HUB/v2] URL alınamadı ({self._fail_count}/{HUB_MAX_FAIL})"
                )
                if self._fail_count >= HUB_MAX_FAIL:
                    break
                time.sleep(min(self._fail_count, 10))
                continue

            self._fail_count = 0
            log.info(f"[HUB/v2] Stream: {url[:60]}...")

            self._run_stream_loop(ffmpeg, url)

            if not self.running:
                break

            log.info("[HUB/v2] Yeniden bağlanılıyor...")
            time.sleep(0.3)

        self.stop()

    def _run_stream_loop(self, ffmpeg_bin: str, initial_url: str):
        session = requests.Session()
        session.verify = False

        url = initial_url
        last_url_gen_time = self._url_generated_at

        while self.running:
            # Süre dolmadan önce token yenile
            elapsed = time.time() - last_url_gen_time
            if elapsed > 35:  # 40 sn - 5 sn = 35
                new_url = self._get_url()
                if new_url and new_url != url:
                    log.info("[HUB/v2] URL yenileniyor (proaktif)")
                    url = new_url
                    last_url_gen_time = self._url_generated_at
                    self._refresh_token()
                elif new_url == url:
                    # Aynı URL ama süre doldu, yine de yenile
                    self._refresh_token()

            # Stream fetch
            try:
                resp = session.get(
                    url,
                    headers=self._portal_headers(),
                    stream=True,
                    timeout=(30, None),
                )

                if resp.status_code == 404:
                    log.warning("[HUB/v2] HTTP 404 - URL yenileniyor")
                    resp.close()
                    url = self._get_url()
                    if not url:
                        break
                    last_url_gen_time = self._url_generated_at
                    time.sleep(0.5)
                    continue

                if resp.status_code == 462:
                    log.warning("[HUB/v2] HTTP 462 - Token expired")
                    resp.close()
                    self._refresh_token()
                    url = self._get_url()
                    if not url:
                        break
                    last_url_gen_time = self._url_generated_at
                    time.sleep(0.5)
                    continue

                if resp.status_code == 458:
                    log.warning("[HUB/v2] HTTP 458 - Session expired")
                    resp.close()
                    self._refresh_token()
                    url = self._get_url()
                    if not url:
                        break
                    last_url_gen_time = self._url_generated_at
                    time.sleep(0.5)
                    continue

                if resp.status_code not in (200, 206):
                    log.warning(f"[HUB/v2] HTTP {resp.status_code}")
                    resp.close()
                    time.sleep(1)
                    continue

            except Exception as e:
                log.warning(f"[HUB/v2] Bağlantı hatası: {e}")
                time.sleep(1)
                continue

            # FFmpeg başlat
            try:
                proc = subprocess.Popen(
                    [
                        ffmpeg_bin,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-probesize",
                        "1000000",
                        "-analyzeduration",
                        "1000000",
                        "-f",
                        "mpegts",
                        "-i",
                        "pipe:0",
                        "-c",
                        "copy",
                        "-f",
                        "mpegts",
                        "-avoid_negative_ts",
                        "make_zero",
                        "-fflags",
                        "+genpts+nobuffer",
                        "-err_detect",
                        "ignore_err",
                        "pipe:1",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                self._proc = proc
            except Exception as e:
                log.error(f"[HUB/v2] FFmpeg başlatma: {e}")
                return

            # Veri aktarımı
            try:
                # Reader thread başlat
                def reader():
                    try:
                        while self.running:
                            chunk = proc.stdout.read(READ_CHUNK)
                            if not chunk:
                                break
                            if chunk:
                                with self.lock:
                                    self.ring.append((self.seq, chunk))
                                    self.seq += 1
                                    self.lock.notify_all()
                    except:
                        pass

                reader_t = threading.Thread(target=reader, daemon=True)
                reader_t.start()

                for chunk in resp.iter_content(chunk_size=READ_CHUNK):
                    if not self.running or not chunk:
                        break
                    try:
                        proc.stdin.write(chunk)
                    except:
                        break
                resp.close()

                # Reader thread bitene kadar bekle
                reader_t.join(timeout=2)

            except:
                pass

            # FFmpeg kapat
            try:
                proc.stdin.close()
            except:
                pass
            try:
                proc.wait(timeout=2)
            except:
                proc.kill()
            self._proc = None

            if not self.running:
                break

            # Döngü devam ediyor - yeni URL al
            new_url = self._get_url()
            if new_url and new_url != url:
                url = new_url
                last_url_gen_time = self._url_generated_at

    def subscribe(self):
        with HUBS_LOCK:
            self.subs += 1

    def unsubscribe(self):
        with HUBS_LOCK:
            self.subs = max(0, self.subs - 1)
            if self.subs == 0:
                self.last_sub = time.time()

    def has_data(self):
        return self.running and self.subs > 0

    def read_from(self, start_seq: int, timeout: float = 5.0):
        with self.lock:
            deadline = time.time() + timeout
            while self.running:
                if self.ring:
                    oldest = self.ring[0][0]
                    newest = self.ring[-1][0]

                    if start_seq < oldest:
                        s, c = self.ring[0]
                        return c, s + 1

                    if newest >= start_seq:
                        for s, c in self.ring:
                            if s == start_seq:
                                return c, start_seq + 1
                            if s > start_seq:
                                return c, s + 1

                remaining = deadline - time.time()
                if remaining <= 0:
                    return b"", start_seq
                self.lock.wait(remaining)

        return None, -1

    def get_pat_pmt(self) -> bytes:
        return b""

    def wait_for_data(self, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        with self.lock:
            while self.running:
                if self.seq > 0:
                    return True
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self.lock.wait(remaining)
        return False


def get_or_create_hub(portal_id: str, ch_id: str, p_client, ch_item: dict):
    key = f"{portal_id}:{ch_id}"
    with HUBS_LOCK:
        hub = STREAM_HUBS.get(key)
        if hub and hub.running:
            return hub
        hub = StreamHub(key, p_client, ch_item)
        STREAM_HUBS[key] = hub
        hub.start()
        return hub


class MAGSessionLoop:
    def __init__(self, p_client):
        self.p = p_client
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"MAGLoop-{self.p.mac}"
        )
        self._thread.start()
        log.info("[MAG] Session loop başlatıldı: %s", self.p.portal)

    def stop(self):
        self._running = False

    def _run(self):
        p = self.p
        try:
            p.get_profile()
            time.sleep(0.5)
            p.get_servertime()
        except Exception:
            pass

        last_refresh = time.time()

        while self._running:
            try:
                p.watchdog()
                p.get_events()

                if (time.time() - last_refresh) >= 8 * 60:
                    log.info("[MAG] Token yenileniyor: %s", p.portal)
                    if p.handshake():
                        p.get_profile()
                        log.info("[MAG] Token yenilendi.")
                    last_refresh = time.time()

            except Exception as exc:
                log.error("[MAG] Loop hatası: %s", exc)

            for _ in range(25):
                if not self._running:
                    return
                time.sleep(1)


MAG_SYNC_TIMEOUT = 20 * 60
