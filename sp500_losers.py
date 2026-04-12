import yfinance as yf
import pandas as pd
import matplotlib.pyplot as plt
import requests
import io
import os
import sys
import time
from deep_translator import GoogleTranslator

# ================= 設定區 =================
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

SECTOR_MAP = {
    'Information Technology': '資訊科技',
    'Health Care': '醫療保健',
    'Financials': '金融',
    'Consumer Discretionary': '非必需消費',
    'Communication Services': '通訊服務',
    'Industrials': '工業',
    'Consumer Staples': '必需消費',
    'Energy': '能源',
    'Utilities': '公用事業',
    'Real Estate': '房地產',
    'Materials': '原物料'
}

if not WEBHOOK_URL:
    print("錯誤：找不到 DISCORD_WEBHOOK_URL 環境變數！")
    sys.exit(1)
# ==========================================

def get_sp500_tickers_info():
    """從 Wikipedia 抓取 S&P 500 成分股清單"""
    print("正在獲取 S&P 500 成分股名單...")
    url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        df = pd.read_html(io.StringIO(response.text))[0]
        df['Symbol'] = df['Symbol'].str.replace('.', '-', regex=False)
        info_dict = df.set_index('Symbol')[['Security', 'GICS Sector']].to_dict(orient='index')
        return info_dict
    except Exception as e:
        print(f"無法抓取 Wiki 資料: {e}")
        return {}

def get_company_details(ticker, close_price):
    """獲取簡介翻譯，並手動計算對齊看盤軟體的股息率"""
    try:
        ticker_obj = yf.Ticker(ticker)
        info = ticker_obj.info
        
        # --- 獲取本益比 ---
        pe_ratio = info.get('trailingPE', info.get('forwardPE', 'N/A'))
        if isinstance(pe_ratio, (int, float)):
            pe_ratio = f"{pe_ratio:.2f}"
            
        # --- 獲取並精準計算股息率 (TTM) ---
        trailing_div_rate = info.get('trailingAnnualDividendRate')
        
        if isinstance(trailing_div_rate, (int, float)) and close_price > 0:
            # 手動計算：過去12個月股息 / 當前收盤價
            div_yield = (trailing_div_rate / close_price) * 100
            div_yield_str = f"{div_yield:.2f}%" if div_yield > 0 else "0.00%"
        else:
            # 備用方案
            raw_yield = info.get('dividendYield')
            if isinstance(raw_yield, (int, float)):
                if raw_yield > 0.3:
                    div_yield_str = f"{raw_yield:.2f}%"
                else:
                    div_yield_str = f"{raw_yield * 100:.2f}%"
            else:
                div_yield_str = "N/A"

        summary_en = info.get('longBusinessSummary', '')
        if not summary_en:
            return "暫無簡介", pe_ratio, div_yield_str

        if len(summary_en) > 300:
            summary_en = summary_en[:300]

        translator = GoogleTranslator(source='auto', target='zh-TW')
        summary_zh = translator.translate(summary_en) + "..."
        
        return summary_zh, pe_ratio, div_yield_str
        
    except Exception as e:
        print(f"資料獲取或翻譯失敗 ({ticker}): {e}")
        return "無法獲取簡介", "N/A", "N/A"

def send_to_discord(ticker, info, close_price, pct_change, image_buffer, summary, pe_ratio, div_yield):
    """發送至 Discord"""
    company_name = info.get('Security', ticker)
    sector_en = info.get('GICS Sector', 'Unknown')
    sector_cn = SECTOR_MAP.get(sector_en, sector_en)
    
    message_content = (
        f"📉 **{ticker} - {company_name}**\n"
        f"🏢 版塊: {sector_cn} ({sector_en})\n"
        f"📊 本益比 (P/E): **{pe_ratio}** |  💰 股息率: **{div_yield}**\n"
        f"📝 簡介: {summary}\n"
        f"🔹 收盤價: ${close_price:.2f}\n"
        f"🔻 跌幅: **{pct_change * 100:.2f}%**" 
    )
    
    payload = {"content": message_content}
    image_buffer.seek(0)
    files = {"file": (f"{ticker}_1Y.png", image_buffer, "image/png")}
    
    requests.post(WEBHOOK_URL, data=payload, files=files)

def main():
    sp500_info = get_sp500_tickers_info()
    tickers = list(sp500_info.keys())
    
    if not tickers:
        tickers = ['AAPL', 'NVDA', 'MSFT'] # 備用
        sp500_info = {t: {'Security': t, 'GICS Sector': 'Unknown'} for t in tickers}
    
    print("正在下載股價資料...")
    data = yf.download(tickers, period="5d", progress=False)['Close']
    
    if data.empty:
        return

    returns = data.pct_change().iloc[-1]
    top_10_losers = returns.nsmallest(10)
    
    print("\n--- 今日跌幅最重前 10 名 ---")
    requests.post(WEBHOOK_URL, json={"content": "📉 **今日 S&P 500 跌幅最重個股報告** 📉"})
    
    for rank, (ticker, pct) in enumerate(top_10_losers.items(), start=1):
        try:
            stock_data = yf.download(ticker, period="9mo", progress=False)
            if stock_data.empty: continue
            
            close_price = stock_data['Close'].iloc[-1].item()
            
            plt.figure(figsize=(10, 5))
            plt.plot(stock_data.index, stock_data['Close'], color='green', linewidth=1.5)
            plt.title(f"{ticker} - 1 Year Trend (Drop)", fontsize=14)
            plt.grid(True, linestyle='--', alpha=0.5)
            plt.tight_layout()
            
            buf = io.BytesIO()
            plt.savefig(buf, format='png')
            plt.close()
            
            # --- 將 close_price 傳入以計算精準股息率 ---
            summary, pe_ratio, div_yield = get_company_details(ticker, close_price)
            company_info = sp500_info.get(ticker, {})
            
            send_to_discord(ticker, company_info, close_price, pct, buf, summary, pe_ratio, div_yield)
            time.sleep(1) 
            
        except Exception as e:
            print(f"處理 {ticker} 時發生錯誤: {e}")

if __name__ == "__main__":
    main()
