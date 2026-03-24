import os
import requests
from bs4 import BeautifulSoup
import base64
import re
import urllib.parse
import json
import time
from datetime import datetime, timedelta
import pytz
from playwright.sync_api import sync_playwright
from flask import Flask, jsonify, Response
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
OUTPUT_FILE = 'output/extracted_data.json'
LAST_RUN_TIME = "尚未执行"

# ==========================================\n# 核心：XXTEA 解密算法\n# ==========================================
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

# ==========================================\n# 爬虫任务逻辑\n# ==========================================
def scrape_job():
    global LAST_RUN_TIME
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.now(tz)
    LAST_RUN_TIME = now.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{LAST_RUN_TIME}] 开始执行抓取任务...")
    
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    
    # 1. 抓取包含赛程的 JS 文件
    try:
        # 添加时间戳防缓存
        js_url = f"https://im-imgs-bucket.oss-accelerate.aliyuncs.com/index.js?t={int(time.time())}"
        res = requests.get(js_url, headers=headers, timeout=10)
        res.encoding = 'utf-8'
        
        # 利用正则提取 document.write 中的 HTML
        html_parts = []
        for m in re.finditer(r"document\.write\((['\"])(.*?)\1\);", res.text):
            html_parts.append(m.group(2))
        full_html = "".join(html_parts)
        soup = BeautifulSoup(full_html, 'html.parser')
    except Exception as e:
        print(f"获取JS赛程失败: {e}")
        return

    matches_to_process = []
    
    # 定义时间窗口：当前时间 ±3 小时
    lower_bound = now - timedelta(hours=3)
    upper_bound = now + timedelta(hours=3)

    # 2. 解析每场比赛
    for ul in soup.find_all('ul', class_='play'):
        league_elem = ul.find('li', class_='lab_events')
        time_elem = ul.find('li', class_='lab_time')
        home_elem = ul.find('li', class_='lab_team_home')
        away_elem = ul.find('li', class_='lab_team_away')

        if not (league_elem and time_elem and home_elem and away_elem):
            continue

        league = league_elem.text.strip()
        time_str = time_elem.text.strip()
        home = home_elem.find('strong').text.strip() if home_elem.find('strong') else ""
        away = away_elem.find('strong').text.strip() if away_elem.find('strong') else ""

        try:
            # 补全时间为当年，处理跨年情况
            match_time = datetime.strptime(f"{now.year}-{time_str}", "%Y-%m-%d %H:%M")
            match_time = tz.localize(match_time)
            
            if match_time > now + timedelta(days=300):
                match_time = match_time.replace(year=now.year - 1)
            elif match_time < now - timedelta(days=300):
                match_time = match_time.replace(year=now.year + 1)
                
            # 判断是否在 ±3 小时内
            if not (lower_bound <= match_time <= upper_bound):
                continue
        except Exception:
            continue

        # 查找指定的 sportsteam368 链接
        target_link = None
        for a in ul.find_all('a', href=True):
            if 'play.sportsteam368.com' in a['href']:
                target_link = a['href']
                break
        
        if target_link:
            match_name = f"JRS {league} {home} VS {away} {time_str}"
            matches_to_process.append({
                'name': match_name,
                'url': target_link
            })

    # 3. 访问子页面获取 高清/蓝光 的 data-play 链接
    final_play_urls = []
    for m in matches_to_process:
        try:
            res = requests.get(m['url'], headers=headers, timeout=10)
            soup = BeautifulSoup(res.text, 'html.parser')
            for a in soup.find_all('a', attrs={'data-play': True}):
                text_content = a.text
                if '高清直播' in text_content or '蓝光' in text_content:
                    # 拼接完整播放地址
                    play_url = "http://play.sportsteam368.com" + a['data-play']
                    final_play_urls.append({
                        'name': m['name'],
                        'url': play_url
                    })
                    break # 找到一个高清通道即可
        except Exception:
            continue

    # 4. 模拟浏览器访问终极页面，提取 ID/encodedStr
    final_data = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        
        for item in final_play_urls:
            page = context.new_page()
            requests_list = []
            page.on("request", lambda request: requests_list.append(request.url))
            
            try:
                page.goto(item['url'], wait_until='networkidle', timeout=15000)
                content = page.content()
                
                # 策略1：优先正则提取页面中的 var encodedStr
                match = re.search(r"var\s+encodedStr\s*=\s*['\"]([^'\"]+)['\"]", content)
                extracted_id = None
                
                if match:
                    extracted_id = match.group(1)
                else:
                    # 策略2：利用网络请求资源树提取 paps.html?id=
                    for req_url in requests_list:
                        if 'paps.html?id=' in req_url:
                            extracted_id = req_url.split('paps.html?id=')[-1]
                            break
                            
                if extracted_id:
                    final_data.append({
                        'name': item['name'],
                        'id': extracted_id
                    })
            except Exception:
                pass
            finally:
                page.close()
        browser.close()

    # 5. 保存结果
    os.makedirs('output', exist_ok=True)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(final_data, f, ensure_ascii=False, indent=2)
    print(f"任务完成，共保存 {len(final_data)} 场比赛数据。")

# ==========================================\n# 统一的播放列表生成逻辑 (支持 M3U 和 TXT)\n# ==========================================
def generate_playlist(fmt="m3u", mode="clean"):
    if not os.path.exists(OUTPUT_FILE):
        return "请稍后再试，爬虫尚未生成数据"
        
    with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
        data_list = json.load(f)
    
    target_key = b"ABCDEFGHIJKLMNOPQRSTUVWX"
    
    if fmt == "m3u":
        content = "#EXTM3U\n"
    else:
        content = "体育直播,#genre#\n"
        
    for item in data_list:
        try:
            raw_id = item['id']
            match_name = item['name']
            if not raw_id: continue
            
            decoded_id = urllib.parse.unquote(raw_id)
            pad = 4 - (len(decoded_id) % 4)
            if pad != 4: decoded_id += "=" * pad
                
            bin_data = base64.b64decode(decoded_id)
            decrypted_bytes = xxtea_decrypt(bin_data, target_key)
            
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

# ==========================================\n# Web 接口\n# ==========================================
@app.route('/')
def index():
    return jsonify({
        "status": "running",
        "last_run_time": LAST_RUN_TIME,
        "endpoints": ["/ids", "/ids.txt", "/m3u", "/m3u_plus", "/txt", "/txt_plus"]
    })

# /ids 和 /ids.txt 路由，按照要求展示对应比赛与ID
@app.route('/ids')
@app.route('/ids.txt')
def get_ids():
    if not os.path.exists(OUTPUT_FILE):
        return Response("数据尚未生成", mimetype='text/plain; charset=utf-8')
    with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
        data_list = json.load(f)
    
    lines = [f"{item['name']} ---- {item['id']}" for item in data_list]
    return Response("\n".join(lines), mimetype='text/plain; charset=utf-8', headers={"Access-Control-Allow-Origin": "*"})

@app.route('/m3u')
def get_m3u_clean():
    return Response(generate_playlist("m3u", "clean"), mimetype='text/plain; charset=utf-8', headers={"Access-Control-Allow-Origin": "*"})

@app.route('/m3u_plus')
def get_m3u_plus():
    return Response(generate_playlist("m3u", "plus"), mimetype='text/plain; charset=utf-8', headers={"Access-Control-Allow-Origin": "*"})

@app.route('/txt')
def get_txt_clean():
    return Response(generate_playlist("txt", "clean"), mimetype='text/plain; charset=utf-8', headers={"Access-Control-Allow-Origin": "*"})

@app.route('/txt_plus')
def get_txt_plus():
    return Response(generate_playlist("txt", "plus"), mimetype='text/plain; charset=utf-8', headers={"Access-Control-Allow-Origin": "*"})

if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    # 启动时立即执行一次，以后每 30 分钟执行一次
    scheduler.add_job(scrape_job, 'interval', minutes=30, next_run_time=datetime.now(pytz.timezone('Asia/Shanghai')))
    scheduler.start()
    app.run(host='0.0.0.0', port=5000, use_reloader=False)
