import os
import traceback
import time
import httpx

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


app = FastAPI(title="TradingView to KIS Overseas Auto Trading")


# =========================================================
# KIS 실전 API
# =========================================================

BASE_URL = "https://openapi.koreainvestment.com:9443"

APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")
CANO = os.getenv("CANO")
ACNT_PRDT_CD = os.getenv("ACNT_PRDT_CD")


# =========================================================
# 환경변수 확인
# =========================================================

required_env = {
    "APP_KEY": APP_KEY,
    "APP_SECRET": APP_SECRET,
    "CANO": CANO,
    "ACNT_PRDT_CD": ACNT_PRDT_CD,
}

missing_env = [
    key for key, value in required_env.items()
    if not value
]

if missing_env:
    print(
        "WARNING: 다음 Render 환경변수가 없습니다: "
        + ", ".join(missing_env)
    )


# =========================================================
# Access Token Cache
# =========================================================

token_cache = {
    "access_token": None,
    "expires_at": 0,
}


# =========================================================
# TradingView Webhook 데이터
# =========================================================

class WebhookSignal(BaseModel):
    action: str
    ticker: str
    exchange: str
    price: float
    qty: int


# =========================================================
# 종목별 KIS 거래소 매핑
#
# 중요:
# TradingView의 {{exchange}} 값을 그대로 KIS에 보내지 않는다.
#
# TradingView:
#   SOXL -> BATS 또는 기타 코드
#
# KIS:
#   SOXL -> AMEX
#
# TradingView:
#   TQQQ -> BATS 등
#
# KIS:
#   TQQQ -> NASD
#
# =========================================================

SYMBOL_EXCHANGE_MAP = {

    # -----------------------------------------------------
    # AMEX / NYSE Arca 계열
    # -----------------------------------------------------

    "SOXL": "AMEX",

    # -----------------------------------------------------
    # NASDAQ 계열
    # -----------------------------------------------------

    "TQQQ": "NASD",
    "NVDA": "NASD",
    "AAPL": "NASD",
    "MSFT": "NASD",
    "AMZN": "NASD",
    "META": "NASD",
    "GOOGL": "NASD",
    "GOOG": "NASD",
    "TSLA": "NASD",

}


# =========================================================
# TradingView 거래소 코드 정리
# =========================================================

def normalize_tradingview_exchange(exchange: str) -> str:

    if not exchange:
        return ""

    exchange = exchange.strip().upper()

    exchange_map = {

        "NASDAQ": "NASD",
        "NASD": "NASD",

        "NYSE": "NYSE",

        "AMEX": "AMEX",

        "BATS": "BATS",

        "ARCA": "ARCA",

        "NYSEARCA": "ARCA",
    }

    return exchange_map.get(exchange, exchange)


# =========================================================
# KIS 거래소 결정
# =========================================================

def get_kis_exchange(
    ticker: str,
    tradingview_exchange: str
) -> str:

    ticker = ticker.strip().upper()

    tv_exchange = normalize_tradingview_exchange(
        tradingview_exchange
    )

    # -----------------------------------------------------
    # 1. 종목별 매핑을 최우선으로 사용
    #
    # TradingView exchange보다 우선한다.
    # -----------------------------------------------------

    if ticker in SYMBOL_EXCHANGE_MAP:

        kis_exchange = SYMBOL_EXCHANGE_MAP[ticker]

        print(
            f"거래소 결정: "
            f"{ticker} -> {kis_exchange} "
            f"(종목별 매핑)"
        )

        return kis_exchange

    # -----------------------------------------------------
    # 2. TradingView에서 KIS와 동일한 거래소 코드가
    #    들어온 경우
    # -----------------------------------------------------

    if tv_exchange in [
        "NASD",
        "NYSE",
        "AMEX"
    ]:

        print(
            f"거래소 결정: "
            f"{ticker} -> {tv_exchange} "
            f"(TradingView exchange)"
        )

        return tv_exchange

    # -----------------------------------------------------
    # 3. BATS / ARCA인데 등록되지 않은 종목
    #
    # 절대로 임의로 NASD/NYSE로 보내지 않는다.
    # -----------------------------------------------------

    if tv_exchange in [
        "BATS",
        "ARCA"
    ]:

        raise HTTPException(
            status_code=400,
            detail=(
                f"거래소 매핑이 필요합니다. "
                f"ticker={ticker}, "
                f"TradingView exchange={tradingview_exchange}. "
                f"SYMBOL_EXCHANGE_MAP에 "
                f"'{ticker}'를 등록하세요."
            )
        )

    # -----------------------------------------------------
    # 4. 알 수 없는 거래소
    # -----------------------------------------------------

    raise HTTPException(
        status_code=400,
        detail=(
            f"지원하지 않는 거래소입니다. "
            f"ticker={ticker}, "
            f"TradingView exchange={tradingview_exchange}"
        )
    )


# =========================================================
# KIS 미국주식 TR ID
# =========================================================

def get_order_tr_id(action: str) -> str:

    action = action.strip().lower()

    if action == "buy":

        return "TTTT1002U"

    if action == "sell":

        return "TTTT1006U"

    raise HTTPException(
        status_code=400,
        detail=(
            f"지원하지 않는 action: {action}. "
            f"buy 또는 sell만 가능합니다."
        )
    )


# =========================================================
# Access Token 발급
# =========================================================

async def get_access_token() -> str:

    global token_cache

    # -----------------------------------------------------
    # 캐시된 토큰 사용
    # -----------------------------------------------------

    if (
        token_cache["access_token"]
        and time.time() < token_cache["expires_at"]
    ):

        return token_cache["access_token"]

    # -----------------------------------------------------
    # 토큰 발급
    # -----------------------------------------------------

    url = f"{BASE_URL}/oauth2/tokenP"

    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
    }

    async with httpx.AsyncClient(
        timeout=20.0
    ) as client:

        response = await client.post(
            url,
            json=body
        )

    if response.status_code != 200:

        raise HTTPException(
            status_code=500,
            detail=(
                f"KIS 토큰 발급 실패: "
                f"{response.status_code} "
                f"{response.text}"
            )
        )

    try:

        data = response.json()

    except Exception:

        raise HTTPException(
            status_code=500,
            detail=(
                f"KIS 토큰 응답 JSON 오류: "
                f"{response.text}"
            )
        )

    access_token = data.get("access_token")

    if not access_token:

        raise HTTPException(
            status_code=500,
            detail=(
                f"KIS Access Token이 없습니다: "
                f"{response.text}"
            )
        )

    token_cache["access_token"] = access_token

    # 약 11시간 캐시
    token_cache["expires_at"] = (
        time.time() + 11 * 60 * 60
    )

    print("KIS Access Token 발급 성공")

    return access_token


# =========================================================
# 해외주식 주문
# =========================================================

async def send_overseas_order(
    action: str,
    ticker: str,
    exchange: str,
    price: float,
    qty: int
) -> dict:

    # -----------------------------------------------------
    # 입력값 정리
    # -----------------------------------------------------

    action = action.strip().lower()
    ticker = ticker.strip().upper()

    # -----------------------------------------------------
    # 입력값 검증
    # -----------------------------------------------------

    if not ticker:

        raise HTTPException(
            status_code=400,
            detail="ticker가 비어 있습니다."
        )

    if qty <= 0:

        raise HTTPException(
            status_code=400,
            detail=f"주문 수량이 올바르지 않습니다: {qty}"
        )

    if price <= 0:

        raise HTTPException(
            status_code=400,
            detail=f"주문 가격이 올바르지 않습니다: {price}"
        )

    # -----------------------------------------------------
    # TR ID
    # -----------------------------------------------------

    tr_id = get_order_tr_id(action)

    # -----------------------------------------------------
    # KIS 거래소
    # -----------------------------------------------------

    kis_exchange = get_kis_exchange(
        ticker=ticker,
        tradingview_exchange=exchange
    )

    # -----------------------------------------------------
    # Access Token
    # -----------------------------------------------------

    token = await get_access_token()

    # -----------------------------------------------------
    # KIS 주문 URL
    # -----------------------------------------------------

    url = (
        f"{BASE_URL}"
        f"/uapi/overseas-stock/v1/trading/order"
    )

    # -----------------------------------------------------
    # Header
    # -----------------------------------------------------

    headers = {

        "content-type":
            "application/json; charset=utf-8",

        "authorization":
            f"Bearer {token}",

        "appkey":
            APP_KEY,

        "appsecret":
            APP_SECRET,

        "tr_id":
            tr_id,

        "custtype":
            "P",
    }

    # -----------------------------------------------------
    # 가격
    # -----------------------------------------------------

    order_price = f"{price:.2f}"

    # -----------------------------------------------------
    # 주문 Body
    # -----------------------------------------------------

    body = {

        "CANO":
            CANO,

        "ACNT_PRDT_CD":
            ACNT_PRDT_CD,

        "OVRS_EXCG_CD":
            kis_exchange,

        "PDNO":
            ticker,

        "ORD_SVR_DVSN_CD":
            "0",

        "ORD_QTY":
            str(int(qty)),

        "OVRS_ORD_UNPR":
            order_price,

        "ORD_DVSN":
            "00",
    }

    # -----------------------------------------------------
    # 주문 전송 로그
    # -----------------------------------------------------

    print("")
    print("========================================")
    print("KIS 해외주식 주문 요청")
    print("========================================")
    print(f"Action              : {action}")
    print(f"Ticker              : {ticker}")
    print(f"TradingView Exchange: {exchange}")
    print(f"KIS Exchange        : {kis_exchange}")
    print(f"Price               : {order_price}")
    print(f"Quantity            : {qty}")
    print(f"TR ID               : {tr_id}")
    print("========================================")

    # -----------------------------------------------------
    # 주문 전송
    # -----------------------------------------------------

    async with httpx.AsyncClient(
        timeout=20.0
    ) as client:

        response = await client.post(
            url,
            json=body,
            headers=headers
        )

    # -----------------------------------------------------
    # 응답 로그
    # -----------------------------------------------------

    print(
        f"KIS 주문 HTTP 응답: "
        f"{response.status_code}"
    )

    print(
        f"KIS 주문 원문 응답: "
        f"{response.text}"
    )

    # -----------------------------------------------------
    # HTTP 상태 확인
    # -----------------------------------------------------

    if response.status_code != 200:

        raise HTTPException(
            status_code=500,
            detail=(
                f"KIS HTTP 오류: "
                f"{response.status_code} "
                f"{response.text}"
            )
        )

    # -----------------------------------------------------
    # JSON 변환
    # -----------------------------------------------------

    try:

        data = response.json()

    except Exception:

        raise HTTPException(
            status_code=500,
            detail=(
                f"KIS JSON 응답 파싱 실패: "
                f"{response.text}"
            )
        )

    # -----------------------------------------------------
    # KIS 업무 처리 결과 확인
    #
    # HTTP 200이어도 주문 성공이 아닐 수 있다.
    #
    # rt_cd == "0"만 성공
    # -----------------------------------------------------

    if data.get("rt_cd") != "0":

        print("")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("KIS 주문 실패")
        print(
            f"rt_cd : {data.get('rt_cd')}"
        )
        print(
            f"msg_cd: {data.get('msg_cd')}"
        )
        print(
            f"msg1  : {data.get('msg1')}"
        )
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("")

        raise HTTPException(
            status_code=500,
            detail=(
                f"KIS 주문 실패 | "
                f"rt_cd={data.get('rt_cd')} | "
                f"msg_cd={data.get('msg_cd')} | "
                f"msg1={data.get('msg1')}"
            )
        )

    # -----------------------------------------------------
    # 주문 성공
    # -----------------------------------------------------

    print("")
    print("========================================")
    print("KIS 주문 성공")
    print("========================================")
    print(data)
    print("========================================")
    print("")

    return data


# =========================================================
# TradingView Webhook
# =========================================================

@app.post("/webhook")
async def tradingview_webhook(
    signal: WebhookSignal
):

    try:

        print("")
        print("========================================")
        print("TradingView Webhook 수신")
        print("========================================")
        print(f"Action  : {signal.action}")
        print(f"Ticker  : {signal.ticker}")
        print(f"Exchange: {signal.exchange}")
        print(f"Price   : {signal.price}")
        print(f"Qty     : {signal.qty}")
        print("========================================")
        print("")

        result = await send_overseas_order(

            action=signal.action,

            ticker=signal.ticker,

            exchange=signal.exchange,

            price=signal.price,

            qty=signal.qty,
        )

        # KIS rt_cd == "0"일 때만 성공 응답

        return {
            "status": "success",
            "result": result
        }

    except HTTPException:

        # 이미 만들어진 오류를 그대로 전달
        raise

    except Exception as e:

        traceback.print_exc()

        raise HTTPException(
            status_code=500,
            detail=f"서버 내부 오류: {str(e)}"
        )


# =========================================================
# Health Check
# =========================================================

@app.get("/")
def health_check():

    return {
        "status": "running",
        "mode": "REAL"
    }