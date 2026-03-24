import datetime as dt
import json
import os
import re
import threading
import time
import base64
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests
from flask import Flask, Response, jsonify
from playwright.sync_api import sync_playwright


@dataclass
class Config:
    source_url: str
    play_link_host_filter: str
    play_host_prefix: str
    keywords_regex: str
    schedule_minutes: int
    tz_name: str
    output_file: Path
    ids_file: Path
    timeout_seconds: int
    host: str
    port: int
    target_key: bytes = b"ABCDEFGHIJKLMNOPQRSTUVWX"


def load_config() -> Config:
    return Config(
        source_url=os.getenv("SOURCE_URL", "https://im-imgs-bucket.oss-accelerate.aliyuncs.com/index.js?t_5").strip(),
        play_link_host_filter=os.getenv("PLAY_LINK_HOST_FILTER", "play.sportsteam368.com").strip(),
        play_host_prefix=os.getenv("PLAY_HOST_PREFIX", "http://play.sportsteam368.com").strip(),
        keywords_regex=os.getenv("KEYWORDS_REGEX", r"高清直播|蓝光"),
        schedule_minutes=int(os.getenv("SCHEDULE_MINUTES", "30")),
        tz_name=os.getenv("TZ_NAME", "Asia/Shanghai"),
        output_file=Path(os.getenv("OUTPUT_FILE", "output/tokens.txt")),
        ids_file=Path(os.getenv("IDS_FILE", "output/ids.json")),
        timeout_seconds=int(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5000")),
    )


# ==========================================
# 核心：XXTEA 解密算法
# ==========================================
def str2long(s, w):
    v = []
    for i in range(0, len(s), 4):
        v0 = s[i]
        v1 = s[i+1] if i+1 < len(s) else 0
        v2 = s[i+2] if i+2 < len(s) else 0
        v3 = s[i+3] if i+3 < len(s) else 0
        v.append(v0 | (v1 << 8) | (v2 << 16) | (v3 << 24))
    if w:
        v.append(len(s))
    return v

def long2str(v, w):
    vl = len(v)
    if vl == 0: return b""
    n = (vl - 1) << 2
    if w:
        m = v[-1]
        if (m < n - 3) or (m > n): return None
        n = m
    s = bytearray()
    for i in range(vl):
        s.append(v[i] & 0xff)
        s.append((v[i] >> 8) & 0xff)
        s.append((v[i] >> 16) & 0xff)
        s.append((v[i] >> 24) & 0xff)
    return bytes(s[:n]) if w else bytes(s)

def xxtea_decrypt(data, key):
    if not data: return b""
    v = str2long(data, False)
    k = str2long(key, False)
    if len(k) < 4:
        k.extend([0] * (4 - len(k)))
    n = len(v) - 1
    if n < 1: return b""
    
    z = v[n]
    y = v[0]
    delta = 0x9E3779B9
    q = 6 + 52 // (n + 1)
    sum_val = (q * delta) & 0xffffffff
    
    while sum_val != 0:
        e = (sum_val >> 2) & 3
        for p in range(n, 0, -1):
            z = v[p - 1]
            mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(p & 3) ^ e] ^ z))
            y = v[p] = (v[p] - mx) & 0xffffffff
        z = v[n]
        mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(0 & 3) ^ e] ^ z))
        y = v[0] = (v[0] - mx) & 0xffffffff
        sum_val = (sum_val - delta) & 0xffffffff
        
    return long2str(v, True)


# ==========================================
# 辅助函数
# ==========================================
def now_in_tz(tz_name: str) -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(tz_name))
    except Exception:
        return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc).astimezone(
            dt.timezone(dt.timedelta(hours=8))
        )


def fetch_text(url: str, timeout_seconds: int) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=timeout_seconds)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def extract_document_write_lines(js_text: str) -> list[str]:
    return re.findall(r"document\.write\('([^']*)'\);", js_text)


def parse_mmdd_hhmm_to_datetime(value: str, now_bj: dt.datetime) -> dt.datetime | None:
    m = re.match(r"^(\d{2})-(\d{2})\s+(\d{2}):(\d{2})$", value)
    if not m:
        return None
    month, day, hour, minute = map(int, m.groups())

    candidates = []
    for y in (now_bj.year - 1, now_bj.year, now_bj.year + 1):
        try:
            candidates.append(
                now_bj.replace(
                    year=y, month=month, day=day, hour=hour, minute=minute,
                    second=0, microsecond=0,
                )
            )
        except ValueError:
            pass

    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - now_bj).total_seconds()))


def within_3h(event_time: dt.datetime, now_bj: dt.datetime) -> bool:
    return abs((event_time - now_bj).total_seconds()) <= 3 * 3600


def extract_match_items(js_text: str, league_prefix: str = "JRS") -> list[dict]:
    lines = extract_document_write_lines(js_text)
    items: list[dict] = []
    current: dict | None = None

    league_re = re.compile(r'class="lab_events"[^>]*><span class="name">([^<]+)</span>')
    time_re = re.compile(r'class="lab_time">([^<]+)<')
    home_re = re.compile(r'class="lab_team_home"><strong class="name">([^<]+)</strong>')
    away_re = re.compile(r'class="lab_team_away"><strong class="name">([^<]+)</strong>')
    href_re = re.compile(r'href="([^"]+)"')

    for line in lines:
        if line.startswith('<ul class="item play'):
            current = {"league": "", "time": "", "home": "", "away": "", "hrefs": []}
            continue

        if current is None:
            continue

        m = league_re.search(line)
        if m:
            current["league"] = f"{league_prefix} {m.group(1).strip()}"

        m = time_re.search(line)
        if m:
            current["time"] = m.group(1).strip()

        m = home_re.search(line)
        if m:
            current["home"] = m.group(1).strip()

        m = away_re.search(line)
        if m:
            current["away"] = m.group(1).strip()

        for hm in href_re.findall(line):
            if hm.startswith("http://") or hm.startswith("https://"):
                current["hrefs"].append(hm.strip())

        if line == "</ul>":
            if current["league"] and current["time"] and current["home"] and current["away"]:
                current["hrefs"] = sorted(set(current["hrefs"]))
                items.append(current)
            current = None

    return items


def extract_data_play_urls(page_text: str, cfg: Config) -> list[str]:
    pattern = re.compile(
        rf'<a[^>]*data-play="([^"]+)"[^>]*>\s*<em[^>]*></em>\s*<strong>([^<]*({cfg.keywords_regex})[^<]*)</strong>',
        re.IGNORECASE,
    )
    urls = []
    for m in pattern.finditer(page_text):
        data_play = m.group(1).strip()
        full_url = urljoin(cfg.play_host_prefix.rstrip("/") + "/", data_play.lstrip("/"))
        urls.append(full_url)
    return sorted(set(urls))


# ==========================================
# 状态与存储
# ==========================================
class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_run_at: str | None = None
        self.last_error: str | None = None
        self.last_count: int = 0

STATE = AppState()


def write_ids(path: Path, ids: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")


def read_ids(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception:
        return []
    return []


# ==========================================
# 播放列表生成逻辑 (集成 XXTEA)
# ==========================================
def generate_playlist(cfg: Config, fmt="m3u", mode="clean") -> str:
    data_list = read_ids(cfg.ids_file)
    if not data_list:
        return "请稍后再试，爬虫尚未生成数据或暂无比赛"
        
    if fmt == "m3u":
        content = "#EXTM3U\n"
    else:
        content = "体育直播,#genre#\n"
        
    for item in data_list:
        try:
            raw_id = item['id']
            match_name = f"{item['league']} {item['home']} VS {item['away']} {item['time']}"
            if not raw_id: continue
            
            decoded_id = urllib.parse.unquote(raw_id)
            pad = 4 - (len(decoded_id) % 4)
            if pad != 4: decoded_id += "=" * pad
                
            bin_data = base64.b64decode(decoded_id)
            decrypted_bytes = xxtea_decrypt(bin_data, cfg.target_key)
            
            if decrypted_bytes:
                json_str = decrypted_bytes.decode('utf-8', errors='ignore')
                data = json.loads(json_str)
                
                if 'url' in data:
                    raw_stream_url = data["url"]
                    if mode == "plus":
                        stream_url = f"{raw_stream_url}|Referer="
                    else:
                        stream_url = raw_stream_url
                    
                    if fmt == "m3u":
                        content += f'#EXTINF:-1 group-title="体育直播",{match_name}\n{stream_url}\n'
                    else:
                        content += f'{match_name},{stream_url}\n'
        except Exception:
            continue
            
    return content


# ==========================================
# 核心抓取循环 (集成 Playwright)
# ==========================================
def run_once(cfg: Config) -> None:
    now_bj = now_in_tz(cfg.tz_name)
    js_text = fetch_text(cfg.source_url, cfg.timeout_seconds)
    raw_items = extract_match_items(js_text, league_prefix="JRS")

    # 1. 过滤时间和拼接目标
    match_links: list[tuple[str, dict]] = []
    for item in raw_items:
        evt = parse_mmdd_hhmm_to_datetime(item["time"], now_bj)
        if not evt or not within_3h(evt, now_bj):
            continue
        meta = {
            "league": item["league"],
            "time": item["time"],
            "home": item["home"],
            "away": item["away"],
        }
        for href in item["hrefs"]:
            if cfg.play_link_host_filter and cfg.play_link_host_filter not in href:
                continue
            match_links.append((href, meta))

    # 2. 收集 data-play 任务，并保留父页面 parent_url 防盗链
    data_play_tasks: list[tuple[str, str, dict]] = []
    seen_pair = set()
    for href, meta in match_links:
        try:
            page_html = fetch_text(href, cfg.timeout_seconds)
            for dp in extract_data_play_urls(page_html, cfg):
                key = (dp, meta["league"], meta["time"], meta["home"], meta["away"])
                if key not in seen_pair:
                    seen_pair.add(key)
                    # 传入 parent_url: href
                    data_play_tasks.append((dp, href, meta))
        except Exception as exc:
            print(f"[warn] open candidate failed: {href} err={exc}")

    # 3. 使用 Playwright 统一处理深度提取 (单次启动浏览器，提升性能)
    mapped_ids: list[dict] = []
    seen_mapped = set()

    if data_play_tasks:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            
            for dp_url, parent_url, meta in data_play_tasks:
                page = context.new_page()
                requests_list = []
                page.on("request", lambda request: requests_list.append(request.url))
                
                extracted_id = None
                try:
                    # 强力防盗链破解
                    page.set_extra_http_headers({"Referer": parent_url})
                    page.goto(dp_url, wait_until='domcontentloaded', timeout=15000)
                    page.wait_for_timeout(2000)
                    
                    content = page.content()
                    
                    # 策略1：正则提取
                    match = re.search(r"var\s+encodedStr\s*=\s*['\"]([^'\"]+)['\"]", content)
                    if match:
                        extracted_id = match.group(1)
                    
                    # 策略2：资源树兜底
                    if not extracted_id:
                        for req_url in requests_list:
                            if 'paps.html?id=' in req_url:
                                extracted_id = req_url.split('paps.html?id=')[-1].split('&')[0]
                                break
                except Exception as exc:
                    print(f"[warn] Playwright failed: {dp_url} err={exc}")
                finally:
                    page.close()

                if extracted_id:
                    row = {
                        "id": extracted_id,
                        "league": meta["league"],
                        "time": meta["time"],
                        "home": meta["home"],
                        "away": meta["away"],
                    }
                    sk = (row["id"], row["league"], row["time"], row["home"], row["away"])
                    if sk not in seen_mapped:
                        seen_mapped.add(sk)
                        mapped_ids.append(row)
                        print(f"✅ 成功抓取: {meta['home']} VS {meta['away']}")
                else:
                    print(f"❌ 抓取为空: {meta['home']} VS {meta['away']}")
                    
            browser.close()

    # 4. 排序保存
    mapped_ids.sort(key=lambda x: (x["time"], x["league"], x["home"], x["away"], x["id"]))
    write_ids(cfg.ids_file, mapped_ids)

    with STATE.lock:
        STATE.last_run_at = now_bj.isoformat()
        STATE.last_error = None
        STATE.last_count = len(mapped_ids)

    print(f"[info] mapped ids={len(mapped_ids)} -> {cfg.ids_file}")


def scheduler_loop(cfg: Config) -> None:
    # 启动时立刻执行一次
    try:
        run_once(cfg)
    except Exception as exc:
        print(f"[error] Initial run failed: {exc}")
        
    while True:
        sleep_seconds = max(cfg.schedule_minutes, 1) * 60
        print(f"[info] sleep {sleep_seconds}s")
        time.sleep(sleep_seconds)
        try:
            run_once(cfg)
        except Exception as exc:
            with STATE.lock:
                STATE.last_error = str(exc)
            print(f"[error] {exc}")


# ==========================================
# Web 路由
# ==========================================
def create_app(cfg: Config) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> Response:
        with STATE.lock:
            payload = {
                "status": "running",
                "last_run_at": STATE.last_run_at,
                "last_error": STATE.last_error,
                "mapped_id_count": STATE.last_count,
                "ids_file": str(cfg.ids_file),
                "endpoints": ["/", "/ids", "/ids.txt", "/m3u", "/m3u_plus", "/txt", "/txt_plus", "/run-once"],
            }
        return jsonify(payload)

    @app.get("/ids")
    def ids_json() -> Response:
        data = read_ids(cfg.ids_file)
        return jsonify({"count": len(data), "items": data})

    @app.get("/ids.txt")
    def ids_text() -> Response:
        data = read_ids(cfg.ids_file)
        lines = [f'{i["league"]}|{i["time"]}|{i["home"]} vs {i["away"]}|{i["id"]}' for i in data]
        return Response("\n".join(lines) + ("\n" if lines else ""), mimetype="text/plain; charset=utf-8")

    @app.get("/m3u")
    def get_m3u_clean() -> Response:
        return Response(generate_playlist(cfg, "m3u", "clean"), mimetype='text/plain; charset=utf-8')

    @app.get("/m3u_plus")
    def get_m3u_plus() -> Response:
        return Response(generate_playlist(cfg, "m3u", "plus"), mimetype='text/plain; charset=utf-8')

    @app.get("/txt")
    def get_txt_clean() -> Response:
        return Response(generate_playlist(cfg, "txt", "clean"), mimetype='text/plain; charset=utf-8')

    @app.get("/txt_plus")
    def get_txt_plus() -> Response:
        return Response(generate_playlist(cfg, "txt", "plus"), mimetype='text/plain; charset=utf-8')

    @app.post("/run-once")
    def trigger_once() -> Response:
        threading.Thread(target=run_once, args=(cfg,), daemon=True).start()
        return jsonify({"queued": True})

    return app


def main() -> None:
    cfg = load_config()
    thread = threading.Thread(target=scheduler_loop, args=(cfg,), daemon=True)
    thread.start()
    app = create_app(cfg)
    app.run(host=cfg.host, port=cfg.port, use_reloader=False)

if __name__ == "__main__":
    main()
