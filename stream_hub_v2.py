"""
VIPEMU HUNTER ELITE - Segment-Based Streaming Engine
Mimarı:
- Segment buffer (prefetch)
- Smart URL refresh (süre dolmadan)
- Seamless segment geçişi
"""

import logging
import os
import shutil
import subprocess
import threading
import time
from collections import deque
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger("stream_hub")

# Sabitler
TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47
READ_CHUNK = 188 * 64
SEGMENT_BUFFER_SIZE = 10
PREFETCH_AHEAD = 3
URL_REFRESH_BEFORE_SEC = 5

STREAM_HUBS = {}
HUBS_LOCK = threading.Lock()
_FFMPEG_BIN = ""
_FFMPEG_LOCK = threading.Lock()


def _find_ffmpeg():
    try:
        import imageio_ffmpeg

        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            log.info("[FFMPEG] imageio-ffmpeg: %s", p)
            return p
    except ImportError:
        pass

    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p:
            log.info("[FFMPEG] PATH: %s", p)
            return p

    base = os.path.dirname(os.path.abspath(__file__))
    for name in ("ffmpeg.exe", "ffmpeg"):
        p = os.path.join(base, name)
        if os.path.isfile(p):
            log.info("[FFMPEG] Yerel: %s", p)
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


class SegmentBuffer:
    def __init__(self, max_size=SEGMENT_BUFFER_SIZE):
        self.buffer = deque(maxlen=max_size)
        self.lock = threading.Condition(threading.Lock())
        self.current_index = 0
        self.total_segments = 0

    def add(self, data):
        with self.lock:
            self.buffer.append(data)
            self.total_segments += 1
            self.lock.notify_all()

    def get_next(self):
        with self.lock:
            if self.current_index >= len(self.buffer):
                return None
            data = self.buffer[self.current_index]
            self.current_index += 1
            self.lock.notify_all()
            return data

    def has_data(self):
        with self.lock:
            return self.current_index < len(self.buffer)

    def clear(self):
        with self.lock:
            self.buffer.clear()
            self.current_index = 0


class StreamHub:
    def __init__(self, hub_key: str, p_client, ch_item: dict):
        self.key = hub_key
        self.p = p_client
        self.ch = ch_item
        self.running = False
        self.subs = 0
        self.last_sub = time.time()
        self.started_at = time.time()
        self.total_bytes = 0
        self._thread = None
        self._segment_buffer = SegmentBuffer()
        self._proc = None
        self._fail_count = 0

    def start(self):
        self.running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"Hub-{self.key}"
        )
        self._thread.start()
        log.info("[HUB] Segment-Based Engine başlatıldı: %s", self.key)

    def stop(self):
        self.running = False
        self._kill_proc()
        with HUBS_LOCK:
            STREAM_HUBS.pop(self.key, None)
        log.info("[HUB] Kapatıldı: %s", self.key)

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
        if hasattr(self.p, "get_fresh_link"):
            url = self.p.get_fresh_link(self.ch)
            if url and url.startswith("http"):
                return url

        url = self.p.create_link(self.ch.get("cmd", ""), self.ch.get("_type", "itv"))
        return url or ""

    def _fetch_segment(self, url, session, timeout=30):
        try:
            resp = session.get(
                url,
                headers=self._portal_headers(),
                stream=True,
                timeout=timeout,
                verify=False,
            )
            if resp.status_code == 200:
                return resp
            elif resp.status_code in (404, 462, 458, 407):
                log.warning(f"[HUB] HTTP {resp.status_code}, URL yenileniyor")
                resp.close()
                return None
            else:
                log.warning(f"[HUB] HTTP {resp.status_code}")
                resp.close()
                return None
        except Exception as e:
            log.warning(f"[HUB] Segment fetch hatası: {e}")
            return None

    def _run(self):
        ffmpeg = get_ffmpeg()
        if not ffmpeg:
            log.error("[HUB] FFmpeg bulunamadı!")
            return

        log.info("[HUB] FFmpeg başlatılıyor: %s", self.key)

        while self.running:
            if self.subs == 0 and (time.time() - self.last_sub) > 3:
                log.info("[HUB] Abone yok, kapatılıyor")
                break

            url = self._get_url()
            if not url:
                self._fail_count += 1
                log.warning(f"[HUB] URL alınamadı ({self._fail_count})")
                if self._fail_count >= 10:
                    break
                time.sleep(min(self._fail_count, 10))
                continue

            self._fail_count = 0
            log.info(f"[HUB] Stream başlıyor: {url[:50]}...")

            self._run_segment_stream(ffmpeg, url)

            if not self.running:
                break

            log.info("[HUB] Yeniden bağlanılıyor...")
            time.sleep(0.5)

        self.stop()

    def _run_segment_stream(self, ffmpeg_bin: str, first_url: str):
        session = requests.Session()
        session.verify = False

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
            log.error(f"[HUB] FFmpeg başlatma hatası: {e}")
            return

        # Reader thread - buffer'a yaz
        def reader():
            try:
                while self.running:
                    chunk = proc.stdout.read(READ_CHUNK)
                    if not chunk:
                        break
                    if chunk:
                        self.total_bytes += len(chunk)
                        self._segment_buffer.add(chunk)
            except:
                pass

        reader_t = threading.Thread(target=reader, daemon=True)
        reader_t.start()

        # URL yenileme kontrolü
        url = first_url
        last_refresh = time.time()
        url_lifetime = 40  # sn - portal'a göre ayarlanabilir

        while self.running:
            # Süre dolmadan URL yenile
            if time.time() - last_refresh > url_lifetime - URL_REFRESH_BEFORE_SEC:
                new_url = self._get_url()
                if new_url and new_url != url:
                    log.info("[HUB] URL yenileniyor (süre dolmadan)")
                    url = new_url
                    last_refresh = time.time()
                    # Session yenile
                    try:
                        self.p.handshake()
                    except:
                        pass

            # Fetch ve FFmpeg'e yaz
            resp = self._fetch_segment(url, session)
            if not resp:
                time.sleep(1)
                continue

            try:
                for chunk in resp.iter_content(chunk_size=READ_CHUNK):
                    if not self.running or not chunk:
                        break
                    try:
                        proc.stdin.write(chunk)
                    except:
                        break
                resp.close()
            except:
                break

            if not self.running:
                break

        # Cleanup
        try:
            proc.stdin.close()
        except:
            pass
        reader_t.join(timeout=2)
        try:
            proc.wait(timeout=2)
        except:
            proc.kill()
        self._proc = None

    def subscribe(self):
        with HUBS_LOCK:
            self.subs += 1
            return 0

    def unsubscribe(self):
        with HUBS_LOCK:
            self.subs = max(0, self.subs - 1)
            if self.subs == 0:
                self.last_sub = time.time()

    def read_chunk(self, timeout=5.0):
        deadline = time.time() + timeout
        while self.running and time.time() < deadline:
            data = self._segment_buffer.get_next()
            if data:
                return data
            time.sleep(0.1)
        return None

    def has_data(self):
        return self._segment_buffer.has_data() and self.running


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
