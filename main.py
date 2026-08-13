import os
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="TradingView to KIS TQQQ Auto-Trader")

# 환경 변수 로드
BASE_URL = os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")
CANO = os.getenv("CANO")
ACNT_PRDT_CD = os.getenv("ACNT_PRDT_CD")

# 트레이딩뷰 웹훅 페이로드 모델
class WebhookSignal(BaseModel):
    action: str  # "buy" 또는 "sell"
    ticker: str = "TQQQ"
    price: float
    qty: int

async def get_access_token() -> str:
    """한국투자증권 접근 토큰(Access Token) 발급 함수"""
    url = f"{BASE_URL}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=body, headers=headers)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"토큰 발급 실패: {response.text}")
        data = response.json()
        return data.get("access_token")

async def send_overseas_order(action: str, ticker: str, price: float, qty: int) -> dict:
    """한국투자증권 미국 주식(해외주식) 지정가 주문 함수"""
    token = await get_access_token()
    
    # 실전투자 기준 TR_ID (매수: JTTT1002U, 매도: JTTT1001U / 모의투자는 별도 TR_ID 확인 필요)
    tr_id = "JTTT1002U" if action.lower() == "buy" else "JTTT1001U"
    
    url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"
    
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id
    }
    
    # TQQQ는 나스닥(NASD) 거래소 종목
    body = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "OVRS_EXCG_CD": "NASD",
        "PDNO": ticker,
        "ORD_SVR_DVSN_CD": "0",
        "ORD_QTY": str(qty),
        "OVRS_ORD_UNPR": str(price),
        "ORD_DVSN": "00"  # 00: 지정가
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=body, headers=headers)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"주문 전송 실패: {response.text}")
        return response.json()

@app.post("/webhook")
async def tradingview_webhook(signal: WebhookSignal):
    """트레이딩뷰 웹훅 신호 수신 엔드포인트"""
    if signal.ticker.upper() != "TQQQ":
        raise HTTPException(status_code=400, detail="Only TQQQ is supported.")
        
    action = signal.action.lower()
    if action not in ["buy", "sell"]:
        raise HTTPException(status_code=400, detail="Action must be 'buy' or 'sell'")
        
    try:
        result = await send_overseas_order(
            action=action,
            ticker=signal.ticker,
            price=signal.price,
            qty=signal.qty
        )
        return {
            "status": "success",
            "action": action,
            "ticker": signal.ticker,
            "kis_response": result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def health_check():
    return {"status": "running", "target": "TQQQ Auto-Trader"}