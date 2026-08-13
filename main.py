import os
import traceback
import time
import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

app = FastAPI(title="TradingView to KIS Real-Only Final")

BASE_URL = "https://openapi.koreainvestment.com:9443"
APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")
CANO = os.getenv("CANO")
ACNT_PRDT_CD = os.getenv("ACNT_PRDT_CD")

token_cache = {"access_token": None, "expires_at": 0}

class WebhookSignal(BaseModel):
    action: str
    ticker: str
    exchange: str
    price: float
    qty: int

async def get_access_token() -> str:
    global token_cache
    if token_cache["access_token"] and time.time() < token_cache["expires_at"]:
        return token_cache["access_token"]

    url = f"{BASE_URL}/oauth2/tokenP"
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=body)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"토큰 발급 실패: {response.text}")
        
        data = response.json()
        token_cache["access_token"] = data.get("access_token")
        token_cache["expires_at"] = time.time() + 43200
        return token_cache["access_token"]

async def send_overseas_order(action: str, ticker: str, exchange: str, price: float, qty: int) -> dict:
    token = await get_access_token()
    
    tr_id = "TTTS1002U" if action.lower() == "buy" else "TTTS1001U"
    
    # [핵심 수정] BATS, ARCA 등 생소한 거래소 코드가 오면 미국 주식 표준(NASD)으로 강제 변환
    ex_upper = exchange.upper()
    if ex_upper in ["NYSE"]:
        kis_exchange = "NYSE"
    elif ex_upper in ["AMEX"]:
        kis_exchange = "AMEX"
    else:
        # BATS, NASDAQ, NASD 등 나머지는 모두 한국투자증권이 인식하는 NASD로 처리
        kis_exchange = "NASD"

    url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P"
    }
    
    body = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "OVRS_EXCG_CD": kis_exchange,
        "PDNO": ticker.strip().upper(),
        "ORD_SVR_DVSN_CD": "0", 
        "ORD_QTY": str(int(qty)),
        "OVRS_ORD_UNPR": str(round(price, 2)),
        "ORD_DVSN": "00"
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=body, headers=headers)
        print(f"KIS 주문 응답: {response.status_code} - {response.text}")
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"주문 전송 실패: {response.text}")
        return response.json()

@app.post("/webhook")
async def tradingview_webhook(signal: WebhookSignal):
    try:
        print(f"수신 데이터 -> action: {signal.action}, ticker: {signal.ticker}, exchange: {signal.exchange}, price: {signal.price}, qty: {signal.qty}")
        result = await send_overseas_order(
            action=signal.action, ticker=signal.ticker, 
            exchange=signal.exchange, price=signal.price, qty=signal.qty
        )
        return {"status": "success", "result": result}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def health_check():
    return {"status": "running"}