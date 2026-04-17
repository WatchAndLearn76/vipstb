import logging
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
log = logging.getLogger("collectors")

def _ask_categories(categories: list, type_name: str) -> list:
    if not categories:
        print(f"\n[!] {type_name}: Hiçbir kategori bulunamadı.")
        return []
    active = [c for c in categories if c and isinstance(c, dict) and c.get("title")]
    print(f"\n--- {type_name.upper()} KATEGORİLERİ ---")
    for i, cat in enumerate(active):
        print(f"[{i:3}] {cat['title']}")
    sel = input("\nSeçeceğiniz kategoriler (örn: 1,5,10 | ENTER=atla): ").strip()
    if not sel:
        return []
    try:
        idx = [int(s.strip()) for s in sel.split(",") if s.strip().isdigit()]
        return [active[i] for i in idx if i < len(active)]
    except Exception:
        return []

def process_item(stb, item: dict, writer, type_mode: str, cat_title: str, fast: bool, use_proxy: bool = False) -> bool:
    cmd = item.get("cmd", "")
    if fast:
        url = cmd
    else:
        url = stb.verify_stream(cmd)
    if url and isinstance(url, str):
        if not use_proxy:
            url = stb._clean_url(url, cmd)
    if not url:
        return False
    if use_proxy:
        safe_cmd = urllib.parse.quote(cmd)
        url = f"http://127.0.0.1:5000/play?type={type_mode}&cmd={safe_cmd}"
    if type_mode == "itv":
        writer.add_live(item, url, cat_title)
    return True

def collect(stb, writer, mode: str, type_mode: str, fast: bool = False, use_proxy: bool = False):
    label = {"itv": "CANLI TV"}[type_mode]
    categories = stb.get_categories(type_mode)
    if not categories:
        log.warning("[!] %s: Kategori listesi alınamadı.", label)
        return
    if mode == "select":
        categories = _ask_categories(categories, label)
        if not categories:
            return
    max_workers = 1 if fast else 30
    for cat in categories:
        cat_title = cat.get("title", "Bilinmiyor")
        log.info("[*] %s: %s taranıyor... ", label, cat_title)
        items = stb.get_all_items(type_mode, cat.get("id"))
        total = len(items)
        done = 0
        added = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_item, stb, itm, writer, type_mode, cat_title, fast, use_proxy) for itm in items]
            for future in as_completed(futures):
                try:
                    if future.result():
                        added += 1
                except Exception as exc:
                    log.debug("İşlem hatası: %s", exc)
                done += 1
                if done % 10 == 0 or done == total:
                    print(f"    [*] İlerleme: {done}/{total} | Eklenen: {added}", end="\r")
        print(f"\n[OK] {cat_title}: {added} içerik listeye eklendi.")

def collect_live(stb, writer, mode, fast=False, use_proxy=False):
    collect(stb, writer, mode, "itv", fast, use_proxy)
