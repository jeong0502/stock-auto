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
# 종목별 KIS 거래소 지정
#
# 중요:
# TradingView의 BATS / ARCA를 보고
# 임의로 NYSE라고 판단하지 않는다.
#
# KIS에서 실제로 거래할 종목의 KIS 거래소를
# 명시적으로 관리한다.
#
# 아래 종목들은 현재 NASD 대상으로 설정.
# =========================================================

SYMBOL_EXCHANGE_MAP = {

    # 반도체 3배 ETF
    "SOXL": "NASD",

    # 나스닥 3배 ETF
    "TQQQ": "NASD",

    # 나스닥 종목
    "NVDA": "NASD",
    "AAPL": "NASD",
    "MSFT": "NASD",
    "AMZN": "NASD",
    "META": "NASD",
    "GOOGL": "NASD",
    "GOOG": "NASD",
    "TSLA": "NASD",

    # 필요하면 여기에 계속 추가
    #
    # "SPY": "AMEX",
    # "QQQ": "NASD",
    # "DIA": "AMEX",
    #
    # 단, 거래소는 KIS 종목정보 기준으로
    # 확인한 뒤 추가할 것.
}


# =========================================================
# TradingView 거래소 코드 정규화
# =========================================================

def normalize_tradingview_exchange(exchange: str) -> str:

    if not exchange:
        return ""

    exchange = exchange.strip().upper()

    exchange_map = {

        # NASDAQ 계열
        "NASDAQ": "NASD",
        "NASD": "NASD",

        # NYSE
        "NYSE": "NYSE",

        # AMEX
        "AMEX": "AMEX",

        # TradingView에서 발생할 수 있는 코드
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
    # 1. 종목별 KIS 거래소가 지정되어 있으면
    #    TradingView exchange보다 이것을 우선한다.
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
    # 2. TradingView가 KIS와 동일하게 명확한 코드를
    #    보내는 경우
    # -----------------------------------------------------

    if tv_exchange in ["NASD", "NYSE", "AMEX"]:

        print(
            f"거래소 결정: "
            f"{ticker} -> {tv_exchange} "
            f"(TradingView exchange)"
        )

        return tv_exchange

    # -----------------------------------------------------
    # 3. BATS / ARCA는 임의로 거래소를 결정하지 않는다.
    #
    # 예:
    # BATS -> NASD
    # BATS -> NYSE
    #
    # 이런 식으로 무조건 변환하면 잘못된 주문이
    # 발생할 수 있다.
    # -----------------------------------------------------

    if tv_exchange in ["BATS", "ARCA"]:

        raise HTTPException(
            status_code=400,
            detail=(
                f"KIS 거래소를 자동 결정할 수 없습니다. "
                f"ticker={ticker}, "
                f"TradingView exchange={tradingview_exchange}. "
                f"SYMBOL_EXCHANGE_MAP에 "
                f"'{ticker}'의 KIS 거래소를 추가하세요."
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
# 미국주식 TR ID
# =========================================================

def get_order_tr_id(action: str) -> str:

    action = action.strip().lower()

    if action == "buy":

        return "TTTT1002U"

    elif action == "sell":

        return "TTTT1006U"

    else:

        raise HTTPException(
            status_code=400,
            detail=(
                f"지원하지 않는 action: {action}. "
                f"buy 또는 sell만 가능합니다."
            )
        )


# =========================================================
# KIS Access Token
# =========================================================

async def get_access_token() -> str:

    global token_cache

    # -----------------------------------------------------
    # 기존 토큰 사용
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

    async with httpx.AsyncClient(timeout=20.0) as client:

        response = await client.post(
            url,
            json=body
        )

    if response.status_code != 200:

        raise HTTPException(
            status_code=500,
            detail=(
                f"KIS 토큰 발급 HTTP 오류: "
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
                f"KIS 토큰 응답 파싱 실패: "
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

    # 11시간 캐시
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
    # 문자열 정리
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
            detail=f"잘못된 주문 수량: {qty}"
        )

    if price <= 0:

        raise HTTPException(
            status_code=400,
            detail=f"잘못된 주문 가격: {price}"
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
    # 주문 API
    # -----------------------------------------------------

    url = (
        f"{BASE_URL}"
        f"/uapi/overseas-stock/v1/trading/order"
    )

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
    # 주문 BODY
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
    # 주문 요청 로그
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
    # KIS 주문
    # -----------------------------------------------------

    async with httpx.AsyncClient(timeout=20.0) as client:

        response = await client.post(
            url,
            json=body,
            headers=headers
        )

    # -----------------------------------------------------
    # HTTP 응답 로그
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
    # HTTP 오류
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
    # JSON 파싱
    # -----------------------------------------------------

    try:

        data = response.json()

    except Exception:

        raise HTTPException(
            status_code=500,
            detail=(
                f"KIS JSON 파싱 실패: "
                f"{response.text}"
            )
        )

    # -----------------------------------------------------
    # KIS 업무 처리 결과
    #
    # HTTP 200 != 주문 성공
    #
    # rt_cd == "0"이어야 성공
    # -----------------------------------------------------

    if data.get("rt_cd") != "0":

        msg_cd = data.get("msg_cd", "")
        msg1 = data.get("msg1", "")

        print("")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("KIS 주문 실패")
        print(f"rt_cd : {data.get('rt_cd')}")
        print(f"msg_cd: {msg_cd}")
        print(f"msg1  : {msg1}")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("")

        raise HTTPException(
            status_code=500,
            detail=(
                f"KIS 주문 실패 | "
                f"rt_cd={data.get('rt_cd')} | "
                f"msg_cd={msg_cd} | "
                f"msg1={msg1}"
            )
        )

    # -----------------------------------------------------
    # 주문 성공
    # -----------------------------------------------------

    print("")
    print("========================================")
    print("KIS 주문 성공")
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

        # 여기까지 왔다는 것은
        # KIS rt_cd == "0"이라는 의미

        return {

            "status": "success",

            "result": result,
        }

    except HTTPException:

        # 이미 만들어진 HTTPException을
        # 그대로 전달

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

        "mode": "REAL",

    }