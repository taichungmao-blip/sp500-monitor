import io
import json
import logging
import math
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
import requests
import yfinance as yf
from deep_translator import GoogleTranslator

LOG = logging.getLogger('sp500_tracker')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
BASE_DIR = Path(__file__).resolve().parent
WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL')
CONSTITUENTS_CACHE = BASE_DIR / 'sp500_constituents_cache.json'
TRANSLATION_CACHE = BASE_DIR / 'sp500_translation_cache.json'
CONSTITUENTS_MAX_AGE_DAYS = 7
TRANSLATION_RETRIES = 3
REQUEST_TIMEOUT = 25
SECTOR_MAP = {
    'Information Technology': '資訊科技', 'Technology': '科技',
    'Health Care': '醫療保健', 'Healthcare': '醫療保健',
    'Financials': '金融', 'Financial Services': '金融服務',
    'Consumer Discretionary': '非必需消費品', 'Consumer Cyclical': '非必需消費品',
    'Communication Services': '通訊服務', 'Industrials': '工業',
    'Consumer Staples': '民生必需品', 'Consumer Defensive': '民生必需品',
    'Energy': '能源', 'Utilities': '公用事業', 'Real Estate': '房地產',
    'Materials': '原物料', 'Basic Materials': '基礎原物料',
    'Unknown': '未知'
}


def load_json(path, default):
    try:
        if path.exists():
            with path.open('r', encoding='utf-8') as f:
                value = json.load(f)
            return value if isinstance(value, type(default)) else default
    except (OSError, ValueError) as exc:
        LOG.warning('讀取快取失敗 %s: %s', path, exc)
    return default


def save_json(path, data):
    try:
        tmp = path.with_name(path.name + '.tmp')
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except OSError as exc:
        LOG.warning('寫入快取失敗 %s: %s', path, exc)


def normalize_symbol(value):
    symbol = str(value).strip().upper().replace('.', '-')
    return symbol if re.fullmatch(r'[A-Z][A-Z0-9-]{0,12}', symbol) else None


def get_sp500_tickers_info():
    """讀取 Wikipedia S&P 500 成分股表；失敗時使用本地快取。"""
    cached = load_json(CONSTITUENTS_CACHE, {})
    now = time.time()
    if (cached.get('stocks') and isinstance(cached['stocks'], dict)
            and now - cached.get('saved_at', 0) < CONSTITUENTS_MAX_AGE_DAYS * 86400):
        LOG.info('使用 S&P 500 成分股快取（%s 檔）', len(cached['stocks']))
        return cached['stocks']
    url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; SP500Tracker/1.0)'
        })
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        stocks = {}
        for df in tables:
            df.columns = [str(c).strip() for c in df.columns]
            if not {'Symbol', 'Security', 'GICS Sector'}.issubset(df.columns):
                continue
            for _, row in df.iterrows():
                symbol = normalize_symbol(row['Symbol'])
                if symbol:
                    stocks[symbol] = {
                        'Security': str(row['Security']).strip(),
                        'GICS Sector': str(row['GICS Sector']).strip()
                    }
            if len(stocks) >= 450:
                break
        if len(stocks) < 450:
            raise ValueError(f'抓到的成分股僅 {len(stocks)} 檔，疑似表格格式變更')
        save_json(CONSTITUENTS_CACHE, {'saved_at': now, 'stocks': stocks})
        LOG.info('成功取得 %d 檔 S&P 500 成分股', len(stocks))
        return stocks
    except Exception as exc:
        LOG.warning('Wikipedia 名單取得失敗: %s', exc)
        if isinstance(cached.get('stocks'), dict) and cached['stocks']:
            LOG.warning('使用舊成分股快取（可能不是最新名單）')
            return cached['stocks']
        LOG.warning('無成分股快取，僅使用 3 檔備用股票，排行榜不代表完整 S&P 500')
        return {s: {'Security': s, 'GICS Sector': 'Unknown'} for s in ['AAPL', 'NVDA', 'MSFT']}


def translate_summary(text, ticker, cache):
    if not text:
        return '暫無公司簡介'
    # 以原文為鍵；公司簡介更新後會重新翻譯。
    if text in cache and isinstance(cache[text], str) and cache[text].strip():
        return cache[text]
    for attempt in range(TRANSLATION_RETRIES):
        try:
            translated = GoogleTranslator(source='en', target='zh-TW').translate(text)
            if translated and translated.strip():
                cache[text] = translated.strip()
                save_json(TRANSLATION_CACHE, cache)
                return translated.strip()
            raise ValueError('翻譯回傳空字串')
        except Exception as exc:
            LOG.warning('%s 翻譯第 %d 次失敗: %s', ticker, attempt + 1, exc)
            if attempt + 1 < TRANSLATION_RETRIES:
                time.sleep(2 * (attempt + 1) + random.uniform(0, 0.5))
    return text + '（翻譯服務暫時無法使用）'


def valid_number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def get_company_details(ticker, close_price, cache):
    """翻譯故障不影響財務數據。殖利率以近一年每股配息/收盤價估算。"""
    info = {}
    for attempt in range(2):
        try:
            info = yf.Ticker(ticker).info or {}
            break
        except Exception as exc:
            LOG.warning('%s 公司資訊讀取失敗 (%d/2): %s', ticker, attempt + 1, exc)
            if attempt == 0:
                time.sleep(2)
    pe = valid_number(info.get('trailingPE'))
    if pe is None:
        pe = valid_number(info.get('forwardPE'))
    pe_str = f'{pe:.2f}' if pe is not None and pe > 0 else 'N/A'
    annual_dividend = valid_number(info.get('trailingAnnualDividendRate'))
    if annual_dividend is not None and annual_dividend >= 0 and close_price > 0:
        div_str = f'{annual_dividend / close_price * 100:.2f}%'
    else:
        # yfinance 的 dividendYield 通常是小數比率；不依數值大小猜測單位。
        raw_yield = valid_number(info.get('dividendYield'))
        div_str = f'{raw_yield * 100:.2f}%' if raw_yield is not None and raw_yield >= 0 else 'N/A'
    summary_en = str(info.get('longBusinessSummary') or '').strip()
    truncated = len(summary_en) > 300
    summary = translate_summary(summary_en[:300], ticker, cache)
    if truncated:
        summary += '…'
    return summary, pe_str, div_str


def discord_post(*, content, image_buffer=None, ticker=None):
    if not WEBHOOK_URL:
        raise RuntimeError('缺少 DISCORD_WEBHOOK_URL 環境變數')
    for attempt in range(4):
        try:
            if image_buffer is None:
                response = requests.post(WEBHOOK_URL, json={'content': content}, timeout=REQUEST_TIMEOUT)
            else:
                image_buffer.seek(0)
                response = requests.post(
                    WEBHOOK_URL, data={'content': content},
                    files={'file': (f'{ticker}_1Y.png', image_buffer, 'image/png')},
                    timeout=REQUEST_TIMEOUT
                )
            if response.status_code in (200, 204):
                return True
            if response.status_code == 429:
                try:
                    delay = float(response.json().get('retry_after', 2))
                except (ValueError, TypeError, AttributeError):
                    delay = 2
                LOG.warning('Discord 限流，等待 %.1f 秒', delay)
                time.sleep(min(max(delay, 1), 60))
                continue
            if response.status_code >= 500:
                LOG.warning('Discord 暫時性錯誤 %s', response.status_code)
            else:
                LOG.error('Discord 發送失敗 HTTP %s: %s', response.status_code, response.text[:200])
                return False
        except requests.RequestException as exc:
            LOG.warning('Discord 網路錯誤: %s', exc)
        if attempt < 3:
            time.sleep(2 ** attempt)
    return False


def configure_chinese_font():
    from matplotlib import font_manager
    candidates = [
        'Microsoft JhengHei', 'PingFang TC', 'Noto Sans CJK TC',
        'Noto Sans CJK JP', 'Noto Sans CJK SC', 'Source Han Sans TW',
        'WenQuanYi Zen Hei', 'SimHei'
    ]
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in installed:
            plt.rcParams['font.sans-serif'] = [name, 'DejaVu Sans']
            plt.rcParams['axes.unicode_minus'] = False
            return True
    LOG.warning('找不到中文字型，圖表標題使用英文以避免亂碼')
    return False


def extract_close(downloaded):
    if downloaded is None or downloaded.empty:
        return pd.DataFrame()
    if isinstance(downloaded.columns, pd.MultiIndex):
        if 'Close' in downloaded.columns.get_level_values(0):
            close = downloaded['Close']
        elif 'Close' in downloaded.columns.get_level_values(1):
            close = downloaded.xs('Close', axis=1, level=1)
        else:
            return pd.DataFrame()
    else:
        if 'Close' not in downloaded.columns:
            return pd.DataFrame()
        close = downloaded['Close']
    if isinstance(close, pd.Series):
        close = close.to_frame()
    return close.apply(pd.to_numeric, errors='coerce')


def fetch_daily_closes(tickers):
    chunks = []
    for i in range(0, len(tickers), 75):
        batch = tickers[i:i + 75]
        for attempt in range(3):
            try:
                df = yf.download(batch, period='10d', auto_adjust=False,
                                 progress=False, threads=False)
                close = extract_close(df)
                if close.empty:
                    raise ValueError('無收盤價資料')
                if len(batch) == 1:
                    close.columns = batch
                chunks.append(close)
                break
            except Exception as exc:
                LOG.warning('股價批次 %d 下載失敗 (%d/3): %s', i // 75 + 1, attempt + 1, exc)
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
        time.sleep(1)
    return pd.concat(chunks, axis=1) if chunks else pd.DataFrame()


def send_to_discord(ticker, info, close_price, pct_change, image_buffer,
                    summary, pe_ratio, div_yield):
    company_name = info.get('Security') or ticker
    sector_en = info.get('GICS Sector') or 'Unknown'
    sector_cn = SECTOR_MAP.get(sector_en, sector_en)
    direction = '📈 漲幅' if pct_change >= 0 else '📉 跌幅'
    message = (
        f'📈 **{ticker} - {company_name}**\n'
        f'🏢 板塊: {sector_cn} ({sector_en})\n'
        f'📊 本益比 (P/E): **{pe_ratio}** | 💰 股息率: **{div_yield}**\n'
        f'📝 簡介: {summary}\n'
        f'🔹 收盤價: ${close_price:.2f}\n'
        f'{direction}: **{abs(pct_change) * 100:.2f}%**'
    )
    return discord_post(content=message[:1900], image_buffer=image_buffer, ticker=ticker)


def main():
    if not WEBHOOK_URL:
        LOG.error('錯誤：找不到 DISCORD_WEBHOOK_URL 環境變數')
        return 1
    sp500_info = get_sp500_tickers_info()
    tickers = list(sp500_info)
    LOG.info('正在下載 %d 檔股票收盤價...', len(tickers))
    closes = fetch_daily_closes(tickers)
    if closes.empty:
        LOG.error('無法取得收盤價資料')
        return 1
    # ffill 僅補齊不同股票休市/缺值；最後兩個共同交易日計算。
    closes = closes.sort_index().ffill().dropna(how='all')
    if len(closes) < 2:
        LOG.error('有效交易日不足兩天')
        return 1
    previous, latest = closes.iloc[-2], closes.iloc[-1]
    returns = ((latest / previous) - 1).replace([float('inf'), -float('inf')], float('nan')).dropna()
    returns = returns[returns > 0].nlargest(10)
    if returns.empty:
        LOG.info('無上漲股票可推播')
        return 0
    font_ok = configure_chinese_font()
    cache = load_json(TRANSLATION_CACHE, {})
    discord_post(content='📊 **今日 S&P 500 漲幅前十名個股報告（繁體中文版）** 📊')
    for rank, (ticker, pct) in enumerate(returns.items(), start=1):
        try:
            df = yf.download(ticker, period='1y', auto_adjust=False,
                             progress=False, threads=False)
            series_df = extract_close(df)
            if series_df.empty:
                LOG.warning('%s 無一年股價資料，略過', ticker)
                continue
            series = series_df.iloc[:, 0].dropna()
            if series.empty:
                continue
            price = float(series.iloc[-1])
            summary, pe, div_yield = get_company_details(ticker, price, cache)
            fig, ax = plt.subplots(figsize=(10, 5))
            try:
                ax.plot(series.index, series.values, color='#1f77b4', linewidth=1.5)
                ax.set_title(f'{ticker} - ' + ('近一年股價走勢' if font_ok else '1 Year Price Trend'))
                ax.set_ylabel('USD')
                ax.grid(True, linestyle='--', alpha=0.5)
                fig.tight_layout()
                buf = io.BytesIO()
                fig.savefig(buf, format='png', dpi=130)
            finally:
                plt.close(fig)
            success = send_to_discord(ticker, sp500_info.get(ticker, {}), price,
                                      float(pct), buf, summary, pe, div_yield)
            LOG.info('第 %d 名 %s：%s', rank, ticker, '發送成功' if success else '發送失敗')
            time.sleep(2)
        except Exception:
            LOG.exception('處理 %s 時發生錯誤', ticker)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

if __name__ == "__main__":
    main()
