import os
import traceback
import time
import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="TradingView to KIS Auto-Trader (Optimized)")

BASE_URL = os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")
CANO = os.getenv("CANO")
ACNT_PRDT_CD = os.getenv("ACNT_PRDT_CD")

# 토큰 캐싱을 위한 전역 변수
token_cache = {
    "access_token": None,
    "expires_at": 0
}

class WebhookSignal(BaseModel):
    action: str
    ticker: str
    exchange: str
    price: float
    qty: int

async def get_access_token() -> str:
    global token_cache
    current_time = time.time()
    
    # 토큰이 남아있고 만료 시간 전이면 기존 토큰 재사용 (1분 제한 방지)
    if token_cache["access_token"] and current_time < token_cache["expires_at"]:
        return token_cache["access_token"]

    url = f"{BASE_URL}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=body, headers=headers)
        if response.status_code != 200:
            print(f"토큰 발급 에러 응답: {response.text}")
            raise HTTPException(status_code=500, detail=f"토큰 발급 실패: {response.text}")
        
        data = response.json()
        token_cache["access_token"] = data.get("access_token")
        # 한투 토큰 유효기간은 보통 24시간이나 안전하게 12시간(43200초)으로 설정 후 재발급
        token_cache["expires_at"] = current_time + 43200
        return token_cache["access_token"]

async def send_overseas_order(action: str, ticker: str, exchange: str, price: float, qty: int) -> dict:
    token = await get_access_token()
    
    # 실전(openapi) vs 모의(openapivts) 주소 여부에 따른 TR_ID 설정
    is_vts = "vts" in BASE_URL
    if is_vts:
        tr_id = "VTTS1002U" if action.lower() == "buy" else "VTTS1001U"
    else:
        tr_id = "JTTT1002U" if action.lower() == "buy" else "JTTT1001U"
    
    # 거래소 매핑 (트레이딩뷰 exchange가 BATS 등 다양하게 들어올 경우 NASD/NYSE로 안전하게 정렬)
    ex_map = {"NYSE": "NYSE", "NASD": "NASD", "NASDAQ": "NASD", "AMEX": "AMEX", "BATS": "NASD"}
    kis_exchange = ex_map.get(exchange.upper(), "NASD")

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
        print(f"KIS 주문 응답 코드: {response.status_code}, 내용: {response.text}")
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"주문 전송 실패: {response.text}")
        return response.json()

@app.post("/webhook")
async def tradingview_webhook(request: Request, signal: WebhookSignal):
    try:
        print(f"수신된 웹훅 데이터: action={signal.action}, ticker={signal.ticker}, exchange={signal.exchange}, price={signal.price}, qty={signal.qty}")
        
        result = await send_overseas_order(
            action=signal.action,
            ticker=signal.ticker,
            exchange=signal.exchange,
            price=signal.price,
            qty=signal.qty
        )
        return {"status": "success", "ticker": signal.ticker, "kis_response": result}
    except Exception as e:
        print("====== 웹훅 처리 중 에러 발생 ======")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def health_check():
    return {"status": "running", "target": "Universal Auto-Trader Optimized"}