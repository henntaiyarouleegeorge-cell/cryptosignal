"""
CryptoSignal Bot v2 - Auto Scanner Edition
Features: Binance auto-scan, OI, Funding Rate, Social Trending, Staged TP/SL, Admin Panel
Deploy: Render.com free tier
"""
import os, json, sqlite3, hashlib, logging, time, math, requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from cryptography.fernet import Fernet
from apscheduler.schedulers.background import BackgroundScheduler
import google.generativeai as genai
import atexit

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static')
CORS(app)

# ── Config ────────────────────────────────────────────────
_mk = os.environ.get('MASTER_KEY', '').encode()
try:
    fernet = Fernet(_mk) if len(_mk) == 44 else Fernet(Fernet.generate_key())
except Exception:
    fernet = Fernet(Fernet.generate_key())

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
ADMIN_TOKEN    = os.environ.get('ADMIN_TOKEN', 'admin123')
DB_PATH        = os.environ.get('DB_PATH', '/data/users.db')
BINANCE_BASE   = "https://fapi.binance.com"
STABLECOINS    = {'USDC','BUSD','TUSD','USDP','DAI','FDUSD','USDD','EURC','PYUSD'}

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# ── Database ──────────────────────────────────────────────
def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                tg_token_enc TEXT NOT NULL,
                tg_chat_id_enc TEXT NOT NULL,
                frequency_hours INTEGER DEFAULT 4,
                active INTEGER DEFAULT 1,
                created_at TEXT,
                last_sent TEXT,
                next_send TEXT,
                nickname TEXT
            );
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin TEXT, direction TEXT, entry REAL,
                tp1 REAL, tp2 REAL, tp3 REAL, sl REAL,
                leverage INTEGER, confidence INTEGER,
                rsi REAL, funding_rate REAL, oi_usd REAL,
                volume_24h REAL, score REAL, analysis TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS send_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT, signal_id INTEGER,
                status TEXT, created_at TEXT
            );
        ''')
        conn.commit()

# ── Crypto helpers ────────────────────────────────────────
def encrypt(t): return fernet.encrypt(t.encode()).decode()
def decrypt(t): return fernet.decrypt(t.encode()).decode()
def make_uid(tok, cid): return hashlib.sha256(f"{tok}:{cid}".encode()).hexdigest()[:16]

# ── Technical Analysis ─────────────────────────────────────
def ema(prices, period):
    k = 2 / (period + 1)
    e = prices[0]
    result = [e]
    for p in prices[1:]:
        e = p * k + e * (1 - k)
        result.append(e)
    return result

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas[-period:]]
    losses = [max(-d, 0) for d in deltas[-period:]]
    ag = sum(gains) / period or 1e-9
    al = sum(losses) / period or 1e-9
    rs = ag / al
    return round(100 - 100 / (1 + rs), 2)

def calc_macd(closes):
    if len(closes) < 26:
        return 0, 0, 0
    fast = ema(closes, 12)
    slow = ema(closes, 26)
    macd_line   = [f - s for f, s in zip(fast, slow)]
    signal_line = ema(macd_line, 9)
    histogram   = macd_line[-1] - signal_line[-1]
    return round(macd_line[-1], 6), round(signal_line[-1], 6), round(histogram, 6)

def calc_bollinger(closes, period=20):
    if len(closes) < period:
        return closes[-1], closes[-1], closes[-1]
    window = closes[-period:]
    mid = sum(window) / period
    std = math.sqrt(sum((p - mid)**2 for p in window) / period)
    return round(mid - 2*std, 6), round(mid, 6), round(mid + 2*std, 6)

# ── Binance Data ───────────────────────────────────────────
def binance_get(path, params=None, timeout=8):
    try:
        r = requests.get(BINANCE_BASE + path, params=params, timeout=timeout)
        if r.ok:
            return r.json()
    except Exception as e:
        logger.warning(f"Binance error {path}: {e}")
    return None

def get_klines(symbol, interval="4h", limit=60):
    data = binance_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not data:
        return []
    return [float(k[4]) for k in data]  # close prices

def get_oi(symbol):
    data = binance_get("/fapi/v1/openInterest", {"symbol": symbol})
    return float(data['openInterest']) if data else 0

def get_oi_history(symbol):
    """OI change over 4h"""
    data = binance_get("/futures/data/openInterestHist",
                       {"symbol": symbol, "period": "4h", "limit": 6})
    if not data or len(data) < 2:
        return 0
    oi_old = float(data[0]['sumOpenInterest'])
    oi_new = float(data[-1]['sumOpenInterest'])
    return round((oi_new - oi_old) / oi_old * 100, 2) if oi_old else 0

def get_funding(symbol):
    data = binance_get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 3})
    if not data:
        return 0
    return round(float(data[-1]['fundingRate']) * 100, 4)

def get_long_short_ratio(symbol):
    data = binance_get("/futures/data/globalLongShortAccountRatio",
                       {"symbol": symbol, "period": "4h", "limit": 1})
    if not data:
        return 1.0
    return round(float(data[0]['longShortRatio']), 3)

# ── Social / Trending ─────────────────────────────────────
def get_trending_coins():
    trending = {}
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=6)
        if r.ok:
            for i, c in enumerate(r.json().get('coins', [])[:15]):
                sym = c['item']['symbol'].upper()
                trending[sym] = {'rank': i+1, 'score': c['item'].get('score', 0),
                                 'market_cap_rank': c['item'].get('market_cap_rank', 999)}
    except Exception as e:
        logger.warning(f"CoinGecko trending error: {e}")
    return trending

# ── Leverage & TP/SL Calculator ───────────────────────────
def calc_signal_levels(price, direction, confidence, volatility_pct):
    """Calculate entry, staged TP, SL, and leverage recommendation"""
    # SL always 3%
    sl_pct = 0.03
    # TP staged: 2.5%, 5%, 8%
    tp1_pct, tp2_pct, tp3_pct = 0.025, 0.05, 0.08

    if direction == "LONG":
        sl  = round(price * (1 - sl_pct), 6)
        tp1 = round(price * (1 + tp1_pct), 6)
        tp2 = round(price * (1 + tp2_pct), 6)
        tp3 = round(price * (1 + tp3_pct), 6)
    else:
        sl  = round(price * (1 + sl_pct), 6)
        tp1 = round(price * (1 - tp1_pct), 6)
        tp2 = round(price * (1 - tp2_pct), 6)
        tp3 = round(price * (1 - tp3_pct), 6)

    # Leverage: based on confidence and volatility
    if confidence >= 80 and volatility_pct < 5:
        lev = 10
    elif confidence >= 70 and volatility_pct < 8:
        lev = 7
    elif confidence >= 60:
        lev = 5
    else:
        lev = 3

    return sl, tp1, tp2, tp3, lev

# ── Scoring Engine ─────────────────────────────────────────
def score_coin(ticker, closes, rsi, macd, macd_sig, oi_change, funding, ls_ratio, trending_coins):
    coin    = ticker['symbol'].replace('USDT', '')
    price   = float(ticker['lastPrice'])
    chg_24h = float(ticker['priceChangePercent'])
    volume  = float(ticker['quoteVolume'])  # USD volume 24h

    score     = 0
    direction = "LONG"
    reasons   = []

    # ── RSI ────────────────────────────────────────────────
    if 28 <= rsi <= 42:
        score += 30; reasons.append(f"RSI過賣回升({rsi:.1f})")
    elif 58 <= rsi <= 72:
        score += 22; reasons.append(f"RSI強勢({rsi:.1f})")
        if rsi > 65: direction = "LONG"
    elif rsi > 75:
        score += 12; reasons.append(f"RSI超買({rsi:.1f})"); direction = "SHORT"
    elif rsi < 25:
        score += 18; reasons.append(f"RSI極度過賣({rsi:.1f})")

    # ── MACD ───────────────────────────────────────────────
    if macd > macd_sig and macd_sig != 0:
        score += 20; reasons.append("MACD金叉")
    elif macd < macd_sig:
        score += 10; reasons.append("MACD死叉")
        if direction == "LONG": direction = "SHORT"

    # ── Price Momentum ────────────────────────────────────
    if 3 <= chg_24h <= 12:
        score += 18; reasons.append(f"24h漲{chg_24h:.1f}%動能強")
    elif -12 <= chg_24h <= -3:
        score += 14; reasons.append(f"24h跌{abs(chg_24h):.1f}%反彈機會")
        direction = "LONG"

    # ── OI Change ─────────────────────────────────────────
    if oi_change > 5:
        score += 15; reasons.append(f"OI4h+{oi_change:.1f}%機構入場")
    elif oi_change < -5:
        score += 8; reasons.append(f"OI4h{oi_change:.1f}%去槓桿")

    # ── Funding Rate ──────────────────────────────────────
    if -0.01 <= funding <= 0.01:
        score += 12; reasons.append("資金費率中性")
    elif funding < -0.03:
        score += 18; reasons.append(f"資金費率負({funding:.3f}%)空單過多")
        direction = "LONG"
    elif funding > 0.05:
        score += 10; reasons.append(f"資金費率高({funding:.3f}%)多單過熱"); direction = "SHORT"

    # ── Long/Short Ratio ──────────────────────────────────
    if ls_ratio < 0.8:
        score += 10; reasons.append(f"多空比{ls_ratio}空方主導(反轉機會)")
        direction = "LONG"
    elif ls_ratio > 1.5:
        score += 8; reasons.append(f"多空比{ls_ratio}多方過熱")
        direction = "SHORT"

    # ── Volume ────────────────────────────────────────────
    if volume > 500_000_000:
        score += 12; reasons.append("天量交易額")
    elif volume > 100_000_000:
        score += 8; reasons.append("大額交易量")

    # ── Social Trending ───────────────────────────────────
    if coin in trending_coins:
        tr = trending_coins[coin]
        score += max(20 - tr['rank'], 5)
        reasons.append(f"CoinGecko趨勢#{tr['rank']}")

    # Volatility for leverage calc
    if len(closes) >= 20:
        recent = closes[-20:]
        volatility = (max(recent) - min(recent)) / min(recent) * 100
    else:
        volatility = 5

    return score, direction, reasons, volatility

# ── Main Scanner ───────────────────────────────────────────
_signal_cache = {'signals': [], 'updated': None}

def run_scanner():
    """Scan Binance futures, score coins, return top signals"""
    global _signal_cache
    logger.info("🔍 Scanner started...")

    # 1. Get all 24h tickers
    tickers_raw = binance_get("/fapi/v1/ticker/24hr") or []
    tickers = {t['symbol']: t for t in tickers_raw
               if t['symbol'].endswith('USDT')
               and t['symbol'].replace('USDT','') not in STABLECOINS
               and float(t.get('quoteVolume', 0)) >= 15_000_000}  # Min $15M volume

    # 2. Get trending social data
    trending = get_trending_coins()

    candidates = []
    processed = 0

    for symbol, ticker in list(tickers.items())[:120]:  # Process top 120 by volume
        try:
            closes = get_klines(symbol, "4h", 60)
            if len(closes) < 30:
                continue

            rsi = calc_rsi(closes)
            macd_val, macd_sig, _ = calc_macd(closes)
            oi_change = get_oi_history(symbol)
            funding   = get_funding(symbol)
            ls_ratio  = get_long_short_ratio(symbol)

            score, direction, reasons, volatility = score_coin(
                ticker, closes, rsi, macd_val, macd_sig,
                oi_change, funding, ls_ratio, trending
            )

            if score >= 55:  # Threshold
                price = float(ticker['lastPrice'])
                sl, tp1, tp2, tp3, lev = calc_signal_levels(price, direction, score, volatility)
                oi_usd = get_oi(symbol) * price

                candidates.append({
                    'symbol': symbol,
                    'coin': symbol.replace('USDT', ''),
                    'price': price,
                    'direction': direction,
                    'score': score,
                    'confidence': min(int(score * 0.95), 95),
                    'rsi': rsi,
                    'macd': macd_val,
                    'macd_signal': macd_sig,
                    'oi_change_4h': oi_change,
                    'funding_rate': funding,
                    'ls_ratio': ls_ratio,
                    'volume_24h': float(ticker['quoteVolume']),
                    'price_change_24h': float(ticker['priceChangePercent']),
                    'volatility': round(volatility, 2),
                    'entry': price,
                    'tp1': tp1, 'tp2': tp2, 'tp3': tp3, 'sl': sl,
                    'leverage': lev,
                    'reasons': reasons,
                    'social_trending': symbol.replace('USDT','') in trending,
                    'social_rank': trending.get(symbol.replace('USDT',''), {}).get('rank', 0)
                })

            processed += 1
            if processed % 20 == 0:
                time.sleep(0.5)  # Rate limit protection

        except Exception as e:
            logger.debug(f"Error scanning {symbol}: {e}")

    # Sort by score, return top 5
    candidates.sort(key=lambda x: x['score'], reverse=True)
    top = candidates[:5]

    _signal_cache = {'signals': top, 'updated': datetime.utcnow().isoformat()}
    logger.info(f"✅ Scanner done. Found {len(candidates)} candidates, top {len(top)}")
    return top

def get_cached_signals():
    """Return cached signals or run scanner if stale"""
    updated = _signal_cache.get('updated')
    if not updated or (datetime.utcnow() - datetime.fromisoformat(updated)).total_seconds() > 3600:
        return run_scanner()
    return _signal_cache['signals']

# ── Gemini Signal Analysis ────────────────────────────────
SIGNAL_PROMPT = """你是頂尖加密貨幣對沖基金擁有20年經驗的冠軍交易員。
你的看法專業、深入、有獨特見解，你的專長是從海量且碎片化的資訊中，拼湊出加密生態系的真實、正確且有邏輯的樣貌，也能精準判斷買賣點、起漲點、預測主力（鯨魚/機構）動向。

以下是系統自動掃描出的信號數據，請你用專業角度進行深度分析：

📊 幣種：{coin} ({direction})
💰 當前價格：${price}
📈 24h漲跌：{change_24h}%
⚡ 信心度：{confidence}%

【技術指標】
• RSI(14)：{rsi}
• MACD：{macd} | 信號線：{macd_signal}
• 波動率(20期)：{volatility}%

【衍生品數據（重要！）】
• OI 4h變化：{oi_change}%（正值=多頭進場，負值=去槓桿）
• 資金費率：{funding_rate}%（正值=多頭付費，負值=空頭付費）
• 多空比：{ls_ratio}（>1多頭主導，<1空頭主導）
• 24h交易量：${volume_m}億美元

【系統建議點位】
• 進場區間：${entry}
• 🎯 TP1（+2.5%）：${tp1}
• 🎯 TP2（+5%）：${tp2}
• 🎯 TP3（+8%）：${tp3}
• 🛑 止損（-3%）：${sl}
• ⚡ 建議槓桿：{leverage}x

【社群指標】
• CoinGecko 趨勢：{social_status}
{social_rank_text}

【系統評分依據】
{reasons}

請基於以上數據，提供：
1. 🔍 **主力動向分析** - 根據OI、資金費率、多空比判斷機構/鯨魚意圖
2. 📊 **技術面確認** - 確認或否定系統信號
3. 🎯 **精確進場策略** - 最佳進場時機、分批建倉建議
4. ⚠️ **風險提示** - 這筆交易最大的風險點
5. ✅ **最終結論** - 做還是不做？幾成倉位？

分析時間：{datetime}
用繁體中文，適合Telegram閱讀格式，重要數字用*粗體*，加入適當emoji。
結尾加入免責聲明：「以上分析僅供參考，請做好風險管理。」"""

def generate_signal_report(sig):
    if not GEMINI_API_KEY:
        return None

    prompt = SIGNAL_PROMPT.format(
        coin=sig['coin'],
        direction="做多🟢 LONG" if sig['direction'] == "LONG" else "做空🔴 SHORT",
        price=f"{sig['price']:,.4f}" if sig['price'] < 1 else f"{sig['price']:,.2f}",
        change_24h=f"{sig['price_change_24h']:+.2f}",
        confidence=sig['confidence'],
        rsi=sig['rsi'],
        macd=sig['macd'],
        macd_signal=sig['macd_signal'],
        volatility=sig['volatility'],
        oi_change=sig['oi_change_4h'],
        funding_rate=sig['funding_rate'],
        ls_ratio=sig['ls_ratio'],
        volume_m=f"{sig['volume_24h']/1e8:.2f}",
        entry=f"{sig['entry']:,.4f}" if sig['entry'] < 1 else f"{sig['entry']:,.2f}",
        tp1=f"{sig['tp1']:,.4f}" if sig['tp1'] < 1 else f"{sig['tp1']:,.2f}",
        tp2=f"{sig['tp2']:,.4f}" if sig['tp2'] < 1 else f"{sig['tp2']:,.2f}",
        tp3=f"{sig['tp3']:,.4f}" if sig['tp3'] < 1 else f"{sig['tp3']:,.2f}",
        sl=f"{sig['sl']:,.4f}" if sig['sl'] < 1 else f"{sig['sl']:,.2f}",
        leverage=sig['leverage'],
        social_status="🔥 上榜" if sig['social_trending'] else "無",
        social_rank_text=f"• 趨勢排名：#{sig['social_rank']}" if sig['social_rank'] else "",
        reasons="\n".join(f"• {r}" for r in sig['reasons']),
        datetime=datetime.now().strftime("%Y-%m-%d %H:%M")
    )

    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        r = model.generate_content(prompt)
        return r.text
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return None

# ── Format Signal Message ─────────────────────────────────
def format_signal_tg(sig, analysis=None):
    dir_emoji = "🟢" if sig['direction'] == "LONG" else "🔴"
    p = sig['price']
    fmt = lambda x: f"{x:,.4f}" if x < 1 else f"{x:,.2f}"

    msg = f"""
{'='*35}
{dir_emoji} *{sig['coin']}USDT {sig['direction']}* | 信心 *{sig['confidence']}%*
{'='*35}

💵 進場價：*${fmt(p)}*
🎯 TP1：*${fmt(sig['tp1'])}*（+2.5%）
🎯 TP2：*${fmt(sig['tp2'])}*（+5%）
🎯 TP3：*${fmt(sig['tp3'])}*（+8%）
🛑 止損：*${fmt(sig['sl'])}*（-3%）
⚡ 建議槓桿：*{sig['leverage']}x*

📊 *技術指標*
• RSI：{sig['rsi']}
• 資金費率：{sig['funding_rate']:+.4f}%
• OI 4h變化：{sig['oi_change_4h']:+.1f}%
• 多空比：{sig['ls_ratio']}
• 24h量：${sig['volume_24h']/1e6:.0f}M
{"• 🔥 CoinGecko趨勢 #"+str(sig['social_rank']) if sig['social_trending'] else ""}

⚠️ *建議倉位分配*
進場分3批：30% → 40% → 30%
""".strip()

    if analysis:
        msg += f"\n\n📝 *AI 深度分析*\n\n{analysis}"

    return msg

# ── Telegram Push ─────────────────────────────────────────
def tg_send(token, chat_id, text):
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
                timeout=10
            )
            if not r.ok:
                logger.error(f"TG error: {r.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"TG send error: {e}")
            return False
    return True

# ── Scheduled Push ────────────────────────────────────────
def push_signals():
    """Main scheduled job: push signals to due users"""
    now = datetime.utcnow()
    signals = get_cached_signals()

    if not signals:
        logger.warning("No signals found")
        return

    with get_db() as conn:
        users = conn.execute(
            "SELECT * FROM users WHERE active=1 AND (next_send IS NULL OR next_send<=?)",
            (now.isoformat(),)
        ).fetchall()

    logger.info(f"Pushing to {len(users)} users, {len(signals)} signals")

    for user in users:
        uid = user['id']
        try:
            tok = decrypt(user['tg_token_enc'])
            cid = decrypt(user['tg_chat_id_enc'])

            # Header
            header = f"📡 *CryptoSignal AI 報單*\n🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*35}\n掃描出 *{len(signals)}* 個高信心信號\n"
            tg_send(tok, cid, header)

            for sig in signals:
                analysis = generate_signal_report(sig)
                msg = format_signal_tg(sig, analysis)
                success = tg_send(tok, cid, msg)

                with get_db() as conn:
                    conn.execute(
                        "INSERT INTO send_logs (user_id, signal_id, status, created_at) VALUES (?,?,?,?)",
                        (uid, 0, 'ok' if success else 'fail', now.isoformat())
                    )
                    conn.commit()
                time.sleep(1)  # Avoid TG rate limit

            next_send = (now + timedelta(hours=user['frequency_hours'])).isoformat()
            with get_db() as conn:
                conn.execute(
                    "UPDATE users SET last_sent=?, next_send=? WHERE id=?",
                    (now.isoformat(), next_send, uid)
                )
                conn.commit()

        except Exception as e:
            logger.error(f"Error pushing to {uid}: {e}")

# ── API ───────────────────────────────────────────────────
def require_admin():
    tok = request.headers.get('X-Admin-Token','')
    return tok == ADMIN_TOKEN

@app.route('/api/register', methods=['POST'])
def register():
    d = request.json or {}
    tok = d.get('tg_token','').strip()
    cid = str(d.get('tg_chat_id','')).strip()
    freq = int(d.get('frequency_hours', 4))
    nick = d.get('nickname','').strip()[:30]

    if not tok or not cid:
        return jsonify({'error': '請填入 TG Bot Token 和 Chat ID'}), 400

    # Validate token
    try:
        r = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=5)
        if not r.ok:
            return jsonify({'error': 'TG Bot Token 無效，請確認格式'}), 400
        bot = r.json()['result']['username']
    except Exception:
        return jsonify({'error': '無法連接 Telegram API'}), 400

    uid = make_uid(tok, cid)
    with get_db() as conn:
        exists = conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
        if exists:
            conn.execute(
                "UPDATE users SET frequency_hours=?, active=1, nickname=? WHERE id=?",
                (freq, nick, uid)
            )
        else:
            conn.execute(
                "INSERT INTO users (id,tg_token_enc,tg_chat_id_enc,frequency_hours,active,created_at,nickname) VALUES (?,?,?,?,1,?,?)",
                (uid, encrypt(tok), encrypt(cid), freq, datetime.utcnow().isoformat(), nick)
            )
        conn.commit()

    welcome = f"""✅ *CryptoSignal AI 訂閱成功！*

🤖 Bot: @{bot}
⏰ 推播頻率：每 {freq} 小時
🔍 系統自動掃描幣安全市場，篩選高信心信號

*接下來會自動推送：*
• 幣種信號（含方向、槓桿建議）
• 分段止盈 TP1/TP2/TP3 + 止損 SL
• OI、資金費率、多空比分析
• AI 深度解讀

輸入 /start 啟動你的 Bot 即可開始接收 🚀"""

    tg_send(tok, cid, welcome)
    return jsonify({'success': True, 'user_id': uid, 'bot': bot})

@app.route('/api/status/<uid>')
def status(uid):
    with get_db() as conn:
        u = conn.execute(
            "SELECT id,frequency_hours,active,last_sent,next_send,nickname,created_at FROM users WHERE id=?",
            (uid,)
        ).fetchone()
    if not u:
        return jsonify({'error': '用戶不存在'}), 404
    return jsonify(dict(u))

@app.route('/api/update/<uid>', methods=['PUT'])
def update(uid):
    d = request.json or {}
    fields, vals = [], []
    for k in ('frequency_hours', 'active', 'nickname'):
        if k in d:
            fields.append(f"{k}=?")
            vals.append(d[k])
    if not fields:
        return jsonify({'error': '無更新內容'}), 400
    vals.append(uid)
    with get_db() as conn:
        conn.execute(f"UPDATE users SET {','.join(fields)} WHERE id=?", vals)
        conn.commit()
    return jsonify({'success': True})

@app.route('/api/unsubscribe/<uid>', methods=['DELETE'])
def unsub(uid):
    with get_db() as conn:
        conn.execute("UPDATE users SET active=0 WHERE id=?", (uid,))
        conn.commit()
    return jsonify({'success': True})

@app.route('/api/test/<uid>', methods=['POST'])
def test(uid):
    with get_db() as conn:
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Not found'}), 404
    try:
        tok = decrypt(u['tg_token_enc'])
        cid = decrypt(u['tg_chat_id_enc'])
        sigs = get_cached_signals()
        if not sigs:
            return jsonify({'error': '暫無信號，稍後再試'}), 500
        sig = sigs[0]
        analysis = generate_signal_report(sig)
        msg = f"🧪 *測試報單*\n\n" + format_signal_tg(sig, analysis)
        ok = tg_send(tok, cid, msg)
        return jsonify({'success': ok})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/signals')
def get_signals():
    sigs = get_cached_signals()
    safe = []
    for s in sigs:
        safe.append({k: v for k, v in s.items() if k not in ('reasons',)})
    return jsonify({'signals': safe, 'updated': _signal_cache.get('updated')})

# ── Admin API ─────────────────────────────────────────────
@app.route('/api/admin/stats')
def admin_stats():
    if not require_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    with get_db() as conn:
        total  = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
        logs   = conn.execute(
            "SELECT COUNT(*) FROM send_logs WHERE created_at>=?",
            ((datetime.utcnow()-timedelta(days=1)).isoformat(),)
        ).fetchone()[0]
    return jsonify({'total': total, 'active': active, 'signals_24h': logs,
                    'cached_signals': len(_signal_cache.get('signals',[])),
                    'cache_updated': _signal_cache.get('updated')})

@app.route('/api/admin/users')
def admin_users():
    if not require_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    with get_db() as conn:
        users = conn.execute(
            "SELECT id,frequency_hours,active,last_sent,next_send,nickname,created_at FROM users ORDER BY created_at DESC"
        ).fetchall()
    return jsonify({'users': [dict(u) for u in users]})

@app.route('/api/admin/toggle/<uid>', methods=['POST'])
def admin_toggle(uid):
    if not require_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    with get_db() as conn:
        u = conn.execute("SELECT active FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            return jsonify({'error': 'Not found'}), 404
        new_state = 0 if u['active'] else 1
        conn.execute("UPDATE users SET active=? WHERE id=?", (new_state, uid))
        conn.commit()
    return jsonify({'success': True, 'active': new_state})

@app.route('/api/admin/broadcast', methods=['POST'])
def admin_broadcast():
    if not require_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    msg = (request.json or {}).get('message','').strip()
    if not msg:
        return jsonify({'error': '訊息不能為空'}), 400

    with get_db() as conn:
        users = conn.execute("SELECT * FROM users WHERE active=1").fetchall()

    sent = 0
    for u in users:
        try:
            tg_send(decrypt(u['tg_token_enc']), decrypt(u['tg_chat_id_enc']), f"📢 *管理員公告*\n\n{msg}")
            sent += 1
        except Exception:
            pass

    return jsonify({'success': True, 'sent': sent})

@app.route('/api/admin/scan', methods=['POST'])
def admin_scan():
    if not require_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    import threading
    threading.Thread(target=run_scanner, daemon=True).start()
    return jsonify({'success': True, 'message': '掃描已啟動'})

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'time': datetime.utcnow().isoformat()})

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def spa(path):
    if path and os.path.exists(os.path.join(app.static_folder or 'static', path)):
        return send_from_directory(app.static_folder or 'static', path)
    return send_from_directory(app.static_folder or 'static', 'index.html')

# ── Scheduler ─────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone='UTC')
scheduler.add_job(push_signals,  'interval', minutes=30, id='push_job')
scheduler.add_job(run_scanner,   'interval', hours=1,    id='scan_job')
scheduler.start()
atexit.register(lambda: scheduler.shutdown(wait=False))

init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
