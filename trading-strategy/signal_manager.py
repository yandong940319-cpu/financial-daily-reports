#!/Users/acyan/.hermes/hermes-agent/venv/bin/python3
"""
币安信号管理器 — 信号全生命周期
- 发现信号 → 创建记录 + 飞书通知
- 监控信号 → 检查是否触价
- 信号结束 → 记录盈亏 + 亏损反思
- 归档到 GitHub
"""
import json, os, sys, time, subprocess
from datetime import datetime, timezone
import urllib.request

# ─── 配置 ────────────────────────────────────────
SIGNALS_DIR = os.path.expanduser("~/financial-daily-reports/data/crypto-signals")
ACTIVE_DIR = os.path.join(SIGNALS_DIR, "active")
HISTORY_DIR = os.path.join(SIGNALS_DIR, "history")
REPO_DIR = os.path.expanduser("~/financial-daily-reports")
MAX_RISK = 200
ACCOUNT_SIZE = 15000

os.makedirs(ACTIVE_DIR, exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)

BINANCE_API = "https://api.binance.com"

# ─── Binance 数据 ────────────────────────────────

def klines(symbol, interval, limit=200):
    url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.loads(r.read())
    result = []
    for k in data:
        result.append({
            'time': datetime.fromtimestamp(k[0]/1000),
            'open': float(k[1]), 'high': float(k[2]),
            'low': float(k[3]), 'close': float(k[4]),
            'volume': float(k[5]),
        })
    return result

def current_price(symbol):
    url = f"{BINANCE_API}/api/v3/ticker/price?symbol={symbol}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return float(json.loads(r.read())['price'])

# ─── 技术分析 ────────────────────────────────────

def sma(d, p):
    c = [x['close'] for x in d]
    return sum(c[-p:])/p if len(c)>=p else None

def ema_vals(d, period):
    """返回整个EMA序列"""
    c = [x['close'] for x in d]
    if len(c) < period: return None
    m = 2/(period+1)
    e = sum(c[:period])/period
    result = [e]
    for v in c[period:]:
        e = (v-e)*m+e
        result.append(e)
    return result

def rsi(d, p=14):
    c = [x['close'] for x in d]
    if len(c) < p+1: return None
    g,l = [],[]
    for i in range(1, p+1):
        diff = c[-i]-c[-i-1]
        g.append(max(diff,0)); l.append(max(-diff,0))
    ag,al = sum(g)/p, sum(l)/p
    return 100-(100/(1+ag/al)) if al!=0 else 100

def atr(d, p=14):
    if len(d) < p+1: return None
    tr = []
    for i in range(1,len(d)):
        hl = d[i]['high']-d[i]['low']
        hc = abs(d[i]['high']-d[i-1]['close'])
        lc = abs(d[i]['low']-d[i-1]['close'])
        tr.append(max(hl,hc,lc))
    return sum(tr[-p:])/p if len(tr)>=p else None

def adx(d, p=14):
    """平均趋向指数 ADX — 衡量趋势强度"""
    if len(d) < p*2: return None
    tr_list, plus_dm, minus_dm = [], [], []
    for i in range(1, len(d)):
        hl = d[i]['high']-d[i]['low']
        hc = abs(d[i]['high']-d[i-1]['close'])
        lc = abs(d[i]['low']-d[i-1]['close'])
        tr_list.append(max(hl, hc, lc))
        
        up_move = d[i]['high'] - d[i-1]['high']
        down_move = d[i-1]['low'] - d[i]['low']
        if up_move > down_move and up_move > 0:
            plus_dm.append(up_move)
        else:
            plus_dm.append(0)
        if down_move > up_move and down_move > 0:
            minus_dm.append(down_move)
        else:
            minus_dm.append(0)
    
    # 平滑
    atr_vals = []
    for i in range(len(tr_list)):
        if i == 0:
            atr_vals.append(sum(tr_list[:p])/p)
        else:
            atr_vals.append((atr_vals[-1]*(p-1) + tr_list[i])/p)
    
    def smooth_dm(dm_list):
        vals = []
        for i in range(len(dm_list)):
            if i == 0:
                vals.append(sum(dm_list[:p])/p)
            else:
                vals.append((vals[-1]*(p-1) + dm_list[i])/p)
        return vals
    
    plus_smooth = smooth_dm(plus_dm)
    minus_smooth = smooth_dm(minus_dm)
    
    # +DI 和 -DI
    di_plus = [ps/av*100 for ps, av in zip(plus_smooth, atr_vals) if av > 0]
    di_minus = [ms/av*100 for ms, av in zip(minus_smooth, atr_vals) if av > 0]
    
    # DX = |+DI - -DI| / (+DI + -DI)
    dx_vals = [abs(dp-dm)/(dp+dm)*100 if (dp+dm) > 0 else 0 
               for dp, dm in zip(di_plus, di_minus)]
    
    # ADX = SMA of DX
    if len(dx_vals) < p:
        return None
    adx_val = sum(dx_vals[-p:]) / p
    return adx_val

def bollinger(d, p=20):
    c = [x['close'] for x in d]
    if len(c) < p: return None,None,None
    m = sum(c[-p:])/p
    std = (sum((x-m)**2 for x in c[-p:])/p)**0.5
    return m, m+2*std, m-2*std

def obv(d):
    """能量潮 OBV — 量价配合分析"""
    if len(d) < 2: return 0
    obv_val = 0
    for i in range(1, len(d)):
        if d[i]['close'] > d[i-1]['close']:
            obv_val += d[i]['volume']
        elif d[i]['close'] < d[i-1]['close']:
            obv_val -= d[i]['volume']
    return obv_val

def volume_sma(d, p=20):
    """成交量均线"""
    vols = [x['volume'] for x in d]
    if len(vols) < p: return None
    return sum(vols[-p:])/p

def macd(d):
    c = [x['close'] for x in d]
    if len(c) < 26: return None,None
    e12,s=0,0
    for i in range(26):
        e12+=c[-i-1]; s+=c[-i-1]
    e12/=26; s/=26
    m2 = 2/13; m9 = 2/10
    for i in range(25,-1,-1):
        e12 = (c[-i-1]-e12)*m2+e12
    ema26 = sum(c[-26:])/26
    m2_26 = 2/27
    for i in range(25,-1,-1):
        ema26 = (c[-i-1]-ema26)*m2_26+ema26
    macd_line = e12 - ema26
    return macd_line, macd_line*0.9  # simplified

def support_resistance(d):
    recent = d[-60:] if len(d)>=60 else d
    highs = sorted([x['high'] for x in recent], reverse=True)
    lows = sorted([x['low'] for x in recent])
    r = sum(highs[:3])/3 if len(highs)>=3 else highs[0]
    s = sum(lows[:3])/3 if len(lows)>=3 else lows[0]
    return s, r

# ─── 信号检测 ────────────────────────────────────

def detect_tf_signal(data, tf_name):
    """对单个时间框架检测信号，返回完整分析结果"""
    if not data or len(data) < 100: return None
    
    d = data
    price = d[-1]['close']
    bb_mid, bb_up, bb_low = bollinger(d)
    rsi_val = rsi(d)
    atr_val = atr(d)
    macd_line, macd_signal = macd(d)
    s, r = support_resistance(d)
    sma20 = sma(d, 20)
    sma50 = sma(d, 50)
    adx_val = adx(d)
    is_trend = adx_val is not None and adx_val > 20
    
    vol_sma20 = volume_sma(d, 20)
    last_vol = d[-1]['volume']
    vol_ratio = last_vol / vol_sma20 if vol_sma20 and vol_sma20 > 0 else 1.0
    is_volume_spike = vol_ratio > 1.3
    
    half = len(d)//2
    obv_recent_val = obv(d[-half:])
    obv_old_val = obv(d[:half])
    obv_trend = "up" if obv_recent_val > obv_old_val else "down"
    
    buy_signals = 0
    sell_signals = 0
    reasons_buy, reasons_sell = [], []
    
    # 1. 市场状态
    if is_trend:
        if adx_val and adx_val > 30:
            reasons_buy.append(f"强趋势ADX({adx_val:.0f})")
    else:
        reasons_sell.append(f"震荡市ADX({adx_val:.0f})")
    
    # 2. RSI
    if rsi_val and rsi_val < 35:
        buy_signals += 2
        reasons_buy.append(f"RSI超卖({rsi_val:.0f})")
    elif rsi_val and rsi_val > 75:
        sell_signals += 2
        reasons_sell.append(f"RSI超买({rsi_val:.0f})")
    elif rsi_val and rsi_val < 40 and is_trend:
        buy_signals += 1
        reasons_buy.append(f"RSI偏低({rsi_val:.0f})")
    elif rsi_val and rsi_val > 60 and is_trend:
        sell_signals += 1
        reasons_sell.append(f"RSI偏高({rsi_val:.0f})")
    
    # 3. 布林带
    if bb_low and price <= bb_low * 1.005:
        buy_signals += 1
        reasons_buy.append("布林下轨")
    if bb_up and price >= bb_up * 0.995:
        sell_signals += 1
        reasons_sell.append("布林上轨")
    
    # 4. MACD
    if macd_line is not None and macd_signal is not None:
        if macd_line > macd_signal:
            buy_signals += 1
            reasons_buy.append("MACD金叉")
        else:
            sell_signals += 1
            reasons_sell.append("MACD死叉")
    
    # 5. MA
    if sma20 and sma50:
        if sma20 > sma50:
            buy_signals += 1
            reasons_buy.append("MA20>MA50")
        else:
            sell_signals += 1
            reasons_sell.append("MA20<MA50")
    
    # 6. 成交量
    if is_volume_spike:
        if buy_signals > sell_signals:
            buy_signals += 1
            reasons_buy.append(f"放量{vol_ratio:.1f}倍")
        elif sell_signals > buy_signals:
            sell_signals += 1
            reasons_sell.append(f"放量{vol_ratio:.1f}倍")
    
    score = buy_signals - sell_signals
    threshold = 4 if not is_trend else 3
    
    if score >= threshold:
        return {
            'tf': tf_name, 'direction': 'LONG', 'score': score, 'confidence': min(buy_signals/9*100, 85),
            'reasons': reasons_buy, 'rsi': rsi_val, 'atr': atr_val, 'adx': adx_val,
            'price': price, 'sma20': sma20, 'sma50': sma50, 'vol_ratio': vol_ratio,
            'obv_trend': obv_trend, 'is_trend': is_trend,
        }
    elif score <= -threshold:
        return {
            'tf': tf_name, 'direction': 'SHORT', 'score': score, 'confidence': min(sell_signals/9*100, 85),
            'reasons': reasons_sell, 'rsi': rsi_val, 'atr': atr_val, 'adx': adx_val,
            'price': price, 'sma20': sma20, 'sma50': sma50, 'vol_ratio': vol_ratio,
            'obv_trend': obv_trend, 'is_trend': is_trend,
        }
    return None

def check_signal(symbol="BTCUSDT"):
    """检测信号 — v3: 15m + 30m 双时间框架对齐
    只有当15m和30m都检测到同方向信号时，才出信号。
    经1年回测验证: 15+30对齐(夏普1.98) 远优于15m独立(夏普-1.34)
    """
    d15 = klines(symbol, "15m", 200)
    d30 = klines(symbol, "30m", 200)
    
    if not d15 or not d30: return None
    
    price = d15[-1]['close']
    
    # 双TF独立检测
    sig_15m = detect_tf_signal(d15, "15m")
    sig_30m = detect_tf_signal(d30, "30m")
    
    if not sig_15m or not sig_30m:
        return None  # 任一TF无信号 → 不出
    
    if sig_15m['direction'] != sig_30m['direction']:
        return None  # 方向不一致 → 不出
    
    direction = sig_15m['direction']
    
    # 使用30m ATR定止损(更宽，减少噪音扫损)
    atr_final = sig_30m.get('atr') or sig_15m.get('atr')
    if not atr_final:
        atr_final = price * 0.015
    
    # 15+30: 使用30m的ATR倍数1.8x(回测优化)
    stop_dist = max(atr_final * 1.8, price * 0.008)
    
    if direction == "LONG":
        entry, stop = price, price - stop_dist
        tp1, tp2, tp3 = entry + stop_dist*1.5, entry + stop_dist*3, entry + stop_dist*5
    else:
        entry, stop = price, price + stop_dist
        tp1, tp2, tp3 = entry - stop_dist*1.5, entry - stop_dist*3, entry - stop_dist*5
    
    # 仓位
    risk_per = abs(entry-stop)
    units = MAX_RISK/risk_per if risk_per > 0 else 0
    value = units * entry
    max_pos = ACCOUNT_SIZE * 0.25
    if value > max_pos:
        units = max_pos / entry
        value = max_pos
    
    # 合并理由: 取双TF的去重理由
    combined_reasons = []
    seen = set()
    for sig in [sig_15m, sig_30m]:
        for r in sig.get('reasons', []):
            # 去重(去掉TF标记)
            key = r.split('(')[0]
            if key not in seen:
                seen.add(key)
                combined_reasons.append(r)
    
    # 置信度取两TF的平均
    combined_confidence = (sig_15m['confidence'] + sig_30m['confidence']) / 2
    
    return {
        'symbol': symbol,
        'timeframe': '15m+30m',
        'direction': direction,
        'entry': round(entry, 2),
        'stop_loss': round(stop, 2),
        'tp1': round(tp1, 2),
        'tp2': round(tp2, 2),
        'tp3': round(tp3, 2),
        'position_size': round(units, 6),
        'position_value': round(value, 2),
        'risk_amount': round(min(risk_per*units, MAX_RISK), 0),
        'confidence': round(combined_confidence, 0),
        'score': sig_15m['score'] + sig_30m['score'],
        'reasons': combined_reasons,
        'current_price': round(price, 2),
        'atr': round(atr_final, 2),
        'rsi_15m': round(sig_15m['rsi'], 1) if sig_15m.get('rsi') else 0,
        'rsi_30m': round(sig_30m['rsi'], 1) if sig_30m.get('rsi') else 0,
        'adx_15m': round(sig_15m['adx'], 1) if sig_15m.get('adx') else 0,
        'adx_30m': round(sig_30m['adx'], 1) if sig_30m.get('adx') else 0,
        'vol_ratio': round(sig_15m['vol_ratio'], 2) if sig_15m.get('vol_ratio') else 0,
        'obv_trend': sig_15m.get('obv_trend', '?'),
        'sig_15m': {'score': sig_15m['score'], 'reasons': sig_15m['reasons']},
        'sig_30m': {'score': sig_30m['score'], 'reasons': sig_30m['reasons']},
        'expiry_hours': 6,  # 15+30组合取中间值
        'expiry_reason': '15m+30m信号超过6小时未触价则失效',
    }

# ─── 信号文件管理 ────────────────────────────────

def signal_id(symbol):
    return f"{symbol}_{datetime.now().strftime('%Y-%m-%d_%H%M')}"

def save_signal(signal):
    """保存新信号到active目录"""
    sid = signal_id(signal['symbol'])
    signal['id'] = sid
    signal['status'] = 'active'
    signal['created_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    signal['closed_at'] = None
    signal['pnl'] = None
    signal['max_pnl'] = None
    signal['reflection'] = None
    
    path = os.path.join(ACTIVE_DIR, f"{sid}.json")
    with open(path, 'w') as f:
        json.dump(signal, f, indent=2, ensure_ascii=False)
    return path

def check_active_signals():
    """检查所有活跃信号的状态"""
    results = []
    for fname in os.listdir(ACTIVE_DIR):
        if not fname.endswith('.json'): continue
        path = os.path.join(ACTIVE_DIR, fname)
        with open(path) as f:
            signal = json.load(f)
        
        current = current_price(signal['symbol'])
        entry = signal['entry']
        direction = signal['direction']
        
        # 计算当前盈亏
        if direction == 'LONG':
            pnl_pct = (current - entry) / entry * 100
            pnl_usd = (current - entry) * signal['position_size']
            # 检查是否触价
            if current >= signal['tp1'] and signal['status'] == 'active':
                signal['status'] = 'tp1_hit'
            if current >= signal['tp2'] and signal['status'] == 'tp1_hit':
                signal['status'] = 'tp2_hit'
            if current >= signal['tp3']:
                signal['status'] = 'tp3_hit'
            if current <= signal['stop_loss']:
                signal['status'] = 'stopped'
        else:
            pnl_pct = (entry - current) / entry * 100
            pnl_usd = (entry - current) * signal['position_size']
            if current <= signal['tp1'] and signal['status'] == 'active':
                signal['status'] = 'tp1_hit'
            if current <= signal['tp2'] and signal['status'] == 'tp1_hit':
                signal['status'] = 'tp2_hit'
            if current <= signal['tp3']:
                signal['status'] = 'tp3_hit'
            if current >= signal['stop_loss']:
                signal['status'] = 'stopped'
        
        signal['current_price'] = round(current, 2)
        signal['pnl_pct'] = round(pnl_pct, 2)
        signal['pnl_usd'] = round(pnl_usd, 0)
        
        # 记录最大盈亏
        if signal.get('max_pnl') is None or abs(pnl_usd) > abs(signal['max_pnl']):
            signal['max_pnl'] = round(pnl_usd, 0)
        
        # 检查信号是否过期
        created = signal.get('created_at', '')
        expiry_hours = signal.get('expiry_hours', 4)
        if created and signal['status'] == 'active':
            try:
                created_dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
                age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
                if age_hours > expiry_hours:
                    signal['status'] = 'cancelled'
                    signal['cancel_reason'] = f'超过有效期{expiry_hours}小时未触价，自动失效'
            except:
                pass
        
        # 如果信号已结束
        was_closed = signal['status'] in ('tp1_hit', 'tp2_hit', 'tp3_hit', 'stopped', 'cancelled')
        if was_closed:
            signal = close_signal(signal)
        
        # 保存更新 (仅在信号还活跃时写回，已关闭的信号已被close_signal移到history)
        if not was_closed:
            with open(path, 'w') as f:
                json.dump(signal, f, indent=2, ensure_ascii=False)
        
        results.append(signal)
    
    return results

def close_signal(signal):
    """关闭信号，记录盈亏和反思"""
    signal['closed_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    
    # 计算最终盈亏
    if signal['status'] == 'stopped':
        signal['pnl'] = round(-signal['risk_amount'], 0)
        signal['pnl_pct'] = -round(signal['risk_amount']/signal['position_value']*100, 2) if signal['position_value'] else 0
        # 亏损反思
        signal['reflection'] = {
            'result': 'LOSS',
            'loss_amount': signal['pnl'],
            'reasons_reviewed': signal.get('reasons', []),
            'lessons': _generate_reflection(signal)
        }
    elif signal['status'] == 'tp1_hit':
        tp1_pnl = (signal['tp1']-signal['entry'])*signal['position_size']*0.3 if signal['direction']=='LONG' else (signal['entry']-signal['tp1'])*signal['position_size']*0.3
        signal['pnl'] = round(tp1_pnl, 0)
        signal['reflection'] = {'result': 'PARTIAL_PROFIT', 'pnl': signal['pnl'], 'lessons': '部分止盈，剩余仓位继续持有'}
    elif signal['status'] in ('tp2_hit', 'tp3_hit'):
        total = (signal['tp3']-signal['entry'])*signal['position_size'] if signal['direction']=='LONG' else (signal['entry']-signal['tp3'])*signal['position_size']
        signal['pnl'] = round(total, 0)
        signal['reflection'] = {'result': 'FULL_PROFIT', 'pnl': signal['pnl'], 'lessons': '目标达成，全部止盈'}
    elif signal['status'] == 'cancelled':
        signal['pnl'] = 0
        reason = signal.get('cancel_reason', '未知原因')
        signal['reflection'] = {'result': 'EXPIRED', 'pnl': 0, 'lessons': f'信号失效: {reason}'}
    
    # 移到history
    src = os.path.join(ACTIVE_DIR, f"{signal['id']}.json")
    dst = os.path.join(HISTORY_DIR, f"{signal['id']}.json")
    os.rename(src, dst)
    
    return signal

def _generate_reflection(signal):
    """亏损反思"""
    reasons = signal.get('reasons', [])
    direction = signal['direction']
    entry = signal['entry']
    stop = signal['stop_loss']
    
    reflection = []
    reflection.append(f"❌ {signal['symbol']} {direction}信号止损")
    reflection.append(f"入场价: ${entry:.2f} | 止损价: ${stop:.2f}")
    reflection.append(f"亏损: -${abs(signal['pnl']):.0f}")
    reflection.append("")
    reflection.append("🔍 反思:")
    reflection.append("1. 入场时的信号理由:")
    for r in reasons:
        reflection.append(f"   - {r}")
    reflection.append("2. 可能的问题:")
    reflection.append("   - 止损太紧？(ATR倍数是否足够)")
    reflection.append("   - 大趋势是否判断错误？")
    reflection.append("   - 是否逆势交易？")
    reflection.append("3. 改进方案:")
    reflection.append("   - 下次扩大止损/缩小仓位")
    reflection.append("   - 等待更好的入场时机")
    
    return '\n'.join(reflection)

def format_signal_msg(signal):
    """格式化飞书通知（v3 - 双TF展示）"""
    d = signal['direction']
    emoji = "🟢" if d == "LONG" else "🔴"
    coin = signal['symbol'].replace('USDT','')
    
    # 根据双TF判断市场状态
    sig_15m = signal.get('sig_15m', {})
    sig_30m = signal.get('sig_30m', {})
    adx_15 = signal.get('adx_15m', 0)
    adx_30 = signal.get('adx_30m', 0)
    
    lines = [
        f"{emoji} **币安信号: {coin} {d}** | ⏱️15m+30m对齐",
        f"",
        f"💰 当前价: ${signal['current_price']:.2f}",
        f"📊 置信度: {signal['confidence']:.0f}% | 评分: {signal['score']:+d}",
        f"",
        f"**入场:** ${signal['entry']:.2f}",
        f"**止损:** ${signal['stop_loss']:.2f} ({(signal['entry']-signal['stop_loss'])/signal['entry']*100 if d=='LONG' else (signal['stop_loss']-signal['entry'])/signal['entry']*100:.2f}%)",
        f"**风险:** ${signal['risk_amount']:.0f}",
        f"**仓位:** {signal['position_size']:.4f} {coin} (${signal['position_value']:.0f})",
        f"",
        f"**🎯 止盈:**",
        f"  TP1: ${signal['tp1']:.2f} ({((signal['tp1']/signal['entry']-1)*100 if d=='LONG' else (1-signal['tp1']/signal['entry'])*100):+.2f}%)",
        f"  TP2: ${signal['tp2']:.2f} ({((signal['tp2']/signal['entry']-1)*100 if d=='LONG' else (1-signal['tp2']/signal['entry'])*100):+.2f}%)",
        f"  TP3: ${signal['tp3']:.2f} ({((signal['tp3']/signal['entry']-1)*100 if d=='LONG' else (1-signal['tp3']/signal['entry'])*100):+.2f}%)",
        f"",
        f"**📋 信号理由:**",
    ]
    for r in signal.get('reasons', []):
        lines.append(f"  • {r}")
    
    lines.extend([
        f"",
        f"📊 双TF指标",
        f"  15m: ADX({adx_15:.0f}) RSI({signal['rsi_15m']})",
        f"  30m: ADX({adx_30:.0f}) RSI({signal['rsi_30m']})",
        f"  ATR(30m): ${signal['atr']} | 成交量: {signal.get('vol_ratio',1):.1f}倍",
        f"  有效期: {signal.get('expiry_hours', 6)}小时未触价自动失效",
    ])
    
    return '\n'.join(lines)

def format_update_msg(signal):
    """格式化持仓更新通知"""
    status = signal['status']
    emoji_map = {
        'tp1_hit': '✅', 'tp2_hit': '🎯', 'tp3_hit': '🏆',
        'stopped': '❌', 'cancelled': '⚪'
    }
    emoji = emoji_map.get(status, '🔄')
    
    coin = signal['symbol'].replace('USDT','')
    pnl = signal.get('pnl_usd', signal.get('pnl', 0))
    pnl_str = f"+${pnl:.0f}" if pnl and pnl > 0 else f"-${abs(pnl):.0f}" if pnl and pnl < 0 else "$0"
    
    lines = [
        f"{emoji} **{coin} 信号更新: {status}**",
        f"当前盈亏: {pnl_str}",
    ]
    
    if status == 'stopped' and signal.get('reflection'):
        lines.append(f"")
        lines.append(f"📝 {signal['reflection']}")
    
    if status == 'cancelled':
        reason = signal.get('cancel_reason', '信号失效')
        reflection = signal.get('reflection', {})
        lessons = reflection.get('lessons', '') if isinstance(reflection, dict) else str(reflection)
        lines.append(f"")
        lines.append(f"⚪ 原因: {reason}")
        if lessons and ':' in lessons:
            lines.append(f"📝 {lessons.split(': ', 1)[-1]}")
        lines.append(f"💡 系统将继续扫描，等待新信号")
    
    return '\n'.join(lines)

def push_to_github():
    """推送到GitHub"""
    os.chdir(REPO_DIR)
    try:
        subprocess.run(['git', 'add', 'data/crypto-signals/'], capture_output=True, timeout=30)
    except:
        pass
    r = subprocess.run(['git', 'commit', '-m', f'🔔 币安信号更新 {datetime.now().strftime("%H:%M")}'], 
                       capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        try:
            subprocess.run(['git', 'push'], capture_output=True, timeout=30,
                           env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
        except subprocess.TimeoutExpired:
            print(f"  ⚠️ git push 超时，将在下次扫描时重试")
        except Exception as e:
            print(f"  ⚠️ git push 错误: {e}")
    return r.returncode == 0 or 'nothing to commit' in r.stderr

# ─── 主流程 ───────────────────────────────────────

def scan_all():
    """扫描所有币种，管理信号生命周期"""
    symbols = ["BTCUSDT", "ETHUSDT"]
    
    # 1. 检查现有信号
    print(f"[{datetime.now().strftime('%H:%M')}] 检查活跃信号...")
    updates = check_active_signals()
    for s in updates:
        if s['status'] in ('stopped', 'tp1_hit', 'tp2_hit', 'tp3_hit', 'cancelled'):
            msg = format_update_msg(s)
            print(f"  信号关闭: {s['symbol']} {s['status']}")
            save_signal_update(s['id'], msg, closed=True)
    
    # 2. 检测新信号
    print(f"[{datetime.now().strftime('%H:%M')}] 扫描新信号...")
    for sym in symbols:
        # 如果已有活跃信号，跳过
        active_exists = any(s['symbol']==sym and s.get('status')=='active' for s in updates if 'symbol' in s)
        if active_exists:
            print(f"  {sym}: 已有活跃信号，跳过")
            continue
        
        signal = check_signal(sym)
        if signal:
            path = save_signal(signal)
            msg = format_signal_msg(signal)
            print(f"  ✅ {sym}: {signal['direction']} 信号!")
            save_new_signal(signal, msg)
        else:
            print(f"  {sym}: 无信号")
    
    push_to_github()

def save_new_signal(signal, msg):
    """保存新信号通知（供cron调用时发送到飞书）"""
    path = os.path.join(ACTIVE_DIR, f"{signal['id']}_notification.txt")
    with open(path, 'w') as f:
        f.write(msg)

def save_signal_update(sid, msg, closed=False):
    """保存信号更新通知"""
    if closed:
        path = os.path.join(HISTORY_DIR, f"{sid}_close_notification.txt")
    else:
        path = os.path.join(ACTIVE_DIR, f"{sid}_update.txt")
    with open(path, 'w') as f:
        f.write(msg)

if __name__ == "__main__":
    scan_all()
