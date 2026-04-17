import logging
import os
import time
log = logging.getLogger("m3u_writer")

class M3UWriter:
    def __init__(self, output_path: str, stb=None):
        self.path = output_path
        self.stb = stb
        self._items: list = []
        out_dir = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(out_dir, exist_ok=True)

    def add_live(self, item: dict, url: str, cat: str):
        self._items.append({
            "name": item.get("name", "Kanal"),
            "group": cat,
            "url": url,
            "logo": item.get("logo", ""),
            "type": "itv"
        })

    def save(self) -> int:
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write("#EXTM3U\n")
            for itm in self._items:
                logo_attr = f' tvg-logo="{itm["logo"]}"' if itm.get("logo") else ""
                f.write(f'\n#EXTINF:-1 group-title="{itm["group"]}"{logo_attr},{itm["name"]}\n')
                if self.stb:
                    f.write(f'#EXTVLCOPT:http-user-agent={self.stb.UA}\n')
                    cookies = f"mac={self.stb.mac}; stb_lang=en; timezone=Europe/Amsterdam"
                    f.write(f'#EXTVLCOPT:http-header=Cookie: {cookies}\n')
                    f.write(f'#EXTVLCOPT:http-referrer={self.stb.portal}/\n')
                f.write('#EXTVLCOPT:network-caching=5000\n')
                f.write('#EXTVLCOPT:live-caching=5000\n')
                f.write('#EXTVLCOPT:http-reconnect=true\n')
                f.write(f'{itm["url"]}\n')
        count = len(self._items)
        log.info("[+] M3U kaydedildi: %s (%d içerik)", self.path, count)
        return count

    def count(self) -> int:
        return len(self._items)
