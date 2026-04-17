import logging
import os
import sys
import time
from stb_client import STBClient
from m3u_writer import M3UWriter
from collectors import collect_live

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s — %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("stb2m3u")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "listeler")

def _banner():
    print("\n" + "=" * 55)
    print("   VİPEMUHUNTER — STB2M3U  (Geliştirme Versiyonu)")
    print("   Stalker/Ministra → M3U_PLUS (SADECE LIVE TV)")
    print("=" * 55 + "\n")

def _choose_mode() -> str:
    print("\nKategori modu:")
    print("  1. Tümünü al  (hızlı)")
    print("  2. Seçerek al (kontrollü)")
    ans = input("Seçim [1/2]:  ").strip()
    return "all" if ans != "2" else "select"

def _choose_fast() -> bool:
    ans = input("\nHızlı mod? (link doğrulama atlanır, daha hızlı) [e/h]: ").strip().lower()
    return ans == "e"

def main():
    _banner()
    portal = input("Portal URL  :  ").strip()
    mac = input("MAC Adresi  :  ").strip()
    stb = STBClient(portal, mac)
    log.info("Bağlanılıyor...")
    if not stb.handshake():
        log.error("Bağlantı BAŞARISIZ. Portal veya MAC hatalı.")
        input("\nÇıkmak için ENTER...")
        sys.exit(1)
    log.info("Bağlantı başarılı!")
    stb.start_heartbeat()
    mode = _choose_mode()
    fast = _choose_fast()
    default_name = f"liste_{int(time.time())}.m3u"
    fname = input(f"\nDosya adı [{default_name}]:  ").strip() or default_name
    if not fname.endswith(".m3u"):
        fname += ".m3u"
    out_path = os.path.join(OUTPUT_DIR, fname)
    writer = M3UWriter(out_path, stb)
    log.info("CANLI TV toplanıyor...")
    collect_live(stb, writer, mode, fast)
    if writer.count() == 0:
        log.warning("Hiç içerik toplanamadı!")
        input("\nÇıkmak için ENTER...")
        sys.exit(0)
    writer.save()
    print("\n" + "=" * 55)
    print(f"  ✓ M3U HAZIR: {out_path}")
    print(f"  ✓ Toplam içerik: {writer.count()}")
    print("  ⚠  Bu pencereyi KAPATMAYIN!")
    print("     Heartbeat portalı canlı tutuyor.")
    print("=" * 55)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stb.stop_heartbeat()
        log.info("Heartbeat durduruldu. İyi yayınlar!")

if __name__ == "__main__":
    main()
