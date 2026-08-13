import os
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="TradingView to KIS Auto-Trader (Universal)")

# 환경 변수 로드
BASE_URL = os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")
CANO = os.getenv("CANO")
ACNT_PRDT_CD = os.getenv("ACNT_PRDT_CD")

class WebhookSignal(BaseModel):
    action: str  # "buy" 또는 "sell"
    ticker: str
    exchange: str  # 트레이딩뷰에서 보내는 거래소 (NYSE, NASD, AMEX 등)
    price: float
    qty: int

async def get_access_token() -> str:
    url = f"{BASE_URL}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=body, headers=headers)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"토큰 발급 실패: {response.text}")
        return response.json().get("access_token")

async def send_overseas_order(action: str, ticker: str, exchange: str, price: float, qty: int) -> dict:
    token = await get_access_token()
    tr_id = "JTTT1002U" if action.lower() == "buy" else "JTTT1001U"
    
    # 한국투자증권 거래소 코드 매핑
    # TV(트레이딩뷰) 거래소명 -> KIS(한투) 거래소 코드
    ex_map = {"NYSE": "NYSE", "NASD": "NASD", "NASDAQ": "NASD", "AMEX": "AMEX"}
    kis_exchange = ex_map.get(exchange.upper(), "NASD") # 기본값 NASD
    
    url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id
    }
    
    body = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "OVRS_EXCG_CD": kis_exchange,
        "PDNO": ticker,
        "ORD_SVR_DVSN_CD": "0",
        "ORD_QTY": str(qty),
        "OVRS_ORD_UNPR": str(price),
        "ORD_DVSN": "00"
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=body, headers=headers)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"주문 전송 실패: {response.text}")
        return response.json()

@app.post("/webhook")
async def tradingview_webhook(signal: WebhookSignal):
    try:
        result = await send_overseas_order(
            action=signal.action,
            ticker=signal.ticker,
            exchange=signal.exchange,
            price=signal.price,
            qty=signal.qty
        )
        return {"status": "success", "ticker": signal.ticker, "kis_response": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def health_check():
    return {"status": "running", "target": "Universal Auto-Trader"}