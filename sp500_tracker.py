import yfinance as yf
import pandas as pd
import matplotlib.pyplot as plt
import requests
import io
import os
import sys

# ================= 設定區 =================
# 從 GitHub Actions 的環境變數中讀取 Webhook URL
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# 防呆機制：確保環境變數有正確載入
if not WEBHOOK_URL:
    print("錯誤：找不到 DISCORD_WEBHOOK_URL 環境變數！")
    print("請確認是否已在 GitHub 儲存庫的 Settings > Secrets and variables > Actions 中設定。")
    sys.exit(1) # 終止程式，讓 GitHub Actions 標示此任務為失敗 (Failed)
# ==========================================

def get_sp500_tickers():
    """從 Wikipedia 抓取 S&P 500 成分股清單"""
    print("正在獲取 S&P 500 成分股名單...")
    url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    table = pd.read_html(url)[0]
    tickers = table['Symbol'].str.replace('.', '-', regex=False).tolist()
    return tickers

def send_to_discord(ticker, close_price, pct_change, image_buffer):
    """將文字與圖表發送至 Discord"""
    message_content = (
        f"**{ticker}**\n"
        f"🔹 收盤價: ${close_price:.2f}\n"
        f"📈 漲跌幅: {pct_change * 100:.2f}%"
    )
    
    payload = {"content": message_content}
    image_buffer.seek(0)
    files = {"file": (f"{ticker}_1Y.png", image_buffer, "image/png")}
    
    response = requests.post(WEBHOOK_URL, data=payload, files=files)
    
    if response.status_code in [200, 204]:
        print(f"成功發送 {ticker} 的通知！")
    else:
        print(f"發送 {ticker} 失敗，錯誤碼: {response.status_code}")

def main():
    tickers = get_sp500_tickers()
    
    print("正在下載股價資料，這可能需要一到兩分鐘...")
    data = yf.download(tickers, period="5d", progress=False)['Close']
    
    # 取最後兩筆有效數據計算變化率
    returns = data.pct_change().iloc[-1]
    
    # 篩選出漲幅前 10 名
    top_10 = returns.nlargest(10)
    print("\n--- 今日強勢股前 10 名 ---")
    
    requests.post(WEBHOOK_URL, json={"content": "📊 **今日 S&P 500 漲幅前十名個股報告** 📊"})
    
    for rank, (ticker, pct) in enumerate(top_10.items(), start=1):
        try:
            stock_data = yf.download(ticker, period="1y", progress=False)
            close_price = stock_data['Close'].iloc[-1].item() 
            
            plt.figure(figsize=(10, 5))
            plt.plot(stock_data.index, stock_data['Close'], color='blue', linewidth=1.5)
            plt.title(f"{ticker} - 1 Year Stock Price Trend", fontsize=14)
            plt.xlabel("Date", fontsize=12)
            plt.ylabel("Price (USD)", fontsize=12)
            plt.grid(True, linestyle='--', alpha=0.6)
            plt.tight_layout()
            
            buf = io.BytesIO()
            plt.savefig(buf, format='png')
            plt.close() 
            
            send_to_discord(ticker, close_price, pct, buf)
            
        except Exception as e:
            print(f"處理 {ticker} 時發生錯誤: {e}")

if __name__ == "__main__":
    main()
  
