import yfinance as yf
import pandas as pd
import matplotlib.pyplot as plt
import requests
import io
import os
import sys
import time
from deep_translator import GoogleTranslator  # 新增翻譯模組

# ================= 設定區 =================
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# 版塊中英對照表
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
    """從 Wikipedia 抓取 S&P 500 成分股清單與詳細資訊"""
    print("正在獲取 S&P 500 成分股名單與詳細資訊...")
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

def get_company_summary(ticker):
    """從 yfinance 獲取簡介並翻譯成繁體中文"""
    try:
        ticker_obj = yf.Ticker(ticker)
        info = ticker_obj.info
        summary_en = info.get('longBusinessSummary', '')
        
        if not summary_en:
            return "暫無簡介"

        # 為了翻譯品質與速度，先擷取前 300 個字元 (通常包含最核心的第一段)
        if len(summary_en) > 300:
            summary_en = summary_en[:300]

        # 執行翻譯 (目標語言: 繁體中文)
        translator = GoogleTranslator(source='auto', target='zh-TW')
        summary_zh = translator.translate(summary_en)
        
        return summary_zh + "..." # 加上刪節號表示有後續
        
    except Exception as e:
        print(f"簡介獲取或翻譯失敗 ({ticker}): {e}")
        return "無法獲取簡介 (翻譯失敗)"

def send_to_discord(ticker, info, close_price, pct_change, image_buffer, summary):
    """發送至 Discord"""
    
    company_name = info.get('Security', ticker)
    sector_en = info.get('GICS Sector', 'Unknown')
    sector_cn = SECTOR_MAP.get(sector_en, sector_en)
    
    message_content = (
        f"**{ticker} - {company_name}**\n"
        f"🏢 版塊: {sector_cn} ({sector_en})\n"
        f"📝 簡介: {summary}\n"
        f"🔹 收盤價: ${close_price:.2f}\n"
        f"📈 漲跌幅: **{pct_change * 100:.2f}%**"
    )
    
    payload = {"content": message_content}
    image_buffer.seek(0)
    files = {"file": (f"{ticker}_1Y.png", image_buffer, "image/png")}
    
    response = requests.post(WEBHOOK_URL, data=payload, files=files)
    
    if response.status_code not in [200, 204]:
        print(f"發送 {ticker} 失敗，錯誤碼: {response.status_code}")

def main():
    sp500_info = get_sp500_tickers_info()
    tickers = list(sp500_info.keys())
    
    if not tickers:
        print("警告：使用備用清單")
        tickers = ['AAPL', 'NVDA', 'MSFT']
        sp500_info = {t: {'Security': t, 'GICS Sector': 'Unknown'} for t in tickers}
    
    print("正在下載股價資料...")
    data = yf.download(tickers, period="5d", progress=False)['Close']
    
    if data.empty:
        print("錯誤：無法下載任何股價資料")
        return

    returns = data.pct_change().iloc[-1]
    top_10 = returns.nlargest(10)
    
    print("\n--- 今日強勢股前 10 名 ---")
    requests.post(WEBHOOK_URL, json={"content": "📊 **今日 S&P 500 漲幅前十名個股報告 (中文版)** 📊"})
    
    for rank, (ticker, pct) in enumerate(top_10.items(), start=1):
        try:
            stock_data = yf.download(ticker, period="1y", progress=False)
            if stock_data.empty: continue
            
            close_price = stock_data['Close'].iloc[-1].item()
            
            plt.figure(figsize=(10, 5))
            plt.plot(stock_data.index, stock_data['Close'], color='#1f77b4', linewidth=1.5)
            plt.title(f"{ticker} - 1 Year Trend", fontsize=14)
            plt.grid(True, linestyle='--', alpha=0.5)
            plt.tight_layout()
            
            buf = io.BytesIO()
            plt.savefig(buf, format='png')
            plt.close()
            
            # 獲取並翻譯簡介
            summary = get_company_summary(ticker)
            company_info = sp500_info.get(ticker, {})
            
            send_to_discord(ticker, company_info, close_price, pct, buf, summary)
            
            # 休息 1 秒，避免翻譯請求太頻繁被擋
            time.sleep(1)
            
        except Exception as e:
            print(f"處理 {ticker} 時發生錯誤: {e}")

if __name__ == "__main__":
    main()
