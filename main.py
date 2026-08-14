import os
import asyncio
import logging
import traceback
import time
from contextlib import asynccontextmanager
from typing import Optional, Any

import httpx

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel


# =========================================================
# 기본 설정
# =========================================================

BASE_URL = "https://openapi.koreainvestment.com:9443"

APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")
CANO = os.getenv("CANO")
ACNT_PRDT_CD = os.getenv("ACNT_PRDT_CD")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# =========================================================
# Logging
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("kis-auto-trading")


# =========================================================
# HTTP Client
# =========================================================

http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=10.0,
            read=20.0,
            write=20.0,
            pool=10.0,
        ),
        limits=httpx.Limits(
            max_connections=30,
            max_keepalive_connections=10,
        ),
    )

    logger.info("HTTP Client 시작")

    yield

    if http_client:
        await http_client.aclose()

    logger.info("HTTP Client 종료")


app = FastAPI(
    title="TradingView → KIS Overseas Auto Trading",
    lifespan=lifespan,
)


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
    logger.warning(
        "필수 Render 환경변수 누락: %s",
        ", ".join(missing_env),
    )

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.warning(
        "Telegram 환경변수가 설정되지 않았습니다."
    )


# =========================================================
# Token Cache + Lock
# =========================================================

token_cache = {
    "access_token": None,
    "expires_at": 0,
}

token_lock = asyncio.Lock()


# =========================================================
# 주문 중복 방지
# =========================================================

order_lock = asyncio.Lock()

# TradingView가 같은 신호를 중복 전송하는 경우 방어
recent_signals = {}

SIGNAL_DEDUP_SECONDS = 5


# =========================================================
# 체결 감시 설정
# =========================================================

FILL_CHECK_INTERVAL = 1.5

# 최초 주문 후 최대 약 15초 동안 체결 확인
FILL_TIMEOUT_SECONDS = 15

# 미체결 발생 시 최대 재주문 횟수
MAX_REPRICE_ATTEMPTS = 2


# =========================================================
# Webhook Model
# =========================================================

class WebhookSignal(BaseModel):
    action: str
    ticker: str
    exchange: str
    price: float
    qty: int

    # 기존 JSON에는 없으므로 기본값 유지
    avg_price: float = 0.0
    profit: float = 0.0


# =========================================================
# 종목별 KIS 거래소
# =========================================================

# TradingView의 BATS / ARCA는 실제 상장거래소와 다를 수 있으므로
# 주요 종목은 명시적으로 관리합니다.
#
# 앞으로 종목을 추가할 때 이곳에 등록하면 됩니다.

SYMBOL_EXCHANGE_MAP = {

    # -----------------------------------------------------
    # AMEX
    # -----------------------------------------------------

    "SOXL": "AMEX",
    "SOXS": "AMEX",
    "SPXL": "AMEX",
    "SPXS": "AMEX",

    # -----------------------------------------------------
    # NASDAQ
    # -----------------------------------------------------

    "TQQQ": "NASD",
    "SQQQ": "NASD",

    "NVDA": "NASD",
    "AAPL": "NASD",
    "MSFT": "NASD",
    "AMZN": "NASD",
    "META": "NASD",
    "GOOGL": "NASD",
    "GOOG": "NASD",
    "TSLA": "NASD",
    "QQQ": "NASD",

    # -----------------------------------------------------
    # NYSE
    # -----------------------------------------------------

    # 필요할 때 여기에 추가
    # "XYZ": "NYSE",
}


# =========================================================
# TradingView 거래소 정규화
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
    tradingview_exchange: str,
) -> str:

    ticker = ticker.strip().upper()

    tv_exchange = normalize_tradingview_exchange(
        tradingview_exchange
    )

    # 1순위: 종목별 명시 매핑
    if ticker in SYMBOL_EXCHANGE_MAP:
        kis_exchange = SYMBOL_EXCHANGE_MAP[ticker]

        logger.info(
            "거래소 결정: %s -> %s (종목별 매핑)",
            ticker,
            kis_exchange,
        )

        return kis_exchange

    # 2순위: TradingView가 KIS 표준 거래소를 직접 보내는 경우
    if tv_exchange in ["NASD", "NYSE", "AMEX"]:

        logger.info(
            "거래소 결정: %s -> %s (TradingView 직접 매핑)",
            ticker,
            tv_exchange,
        )

        return tv_exchange

    # 3순위
    #
    # BATS / ARCA는 실제 상장 거래소와 동일하지 않을 수 있으므로
    # 임의로 NASD/NYSE를 결정하지 않습니다.
    #
    # 이것을 억지로 처리하면 "해당종목정보가 없습니다"가
    # 발생할 수 있습니다.

    raise HTTPException(
        status_code=400,
        detail=(
            f"KIS 거래소를 결정할 수 없습니다: "
            f"{ticker} / TradingView={tradingview_exchange}. "
            f"SYMBOL_EXCHANGE_MAP에 종목을 등록하세요."
        ),
    )


# =========================================================
# KIS TR ID
# =========================================================

def get_order_tr_id(action: str) -> str:

    action = action.strip().lower()

    if action == "buy":
        return "TTTT1002U"

    if action == "sell":
        return "TTTT1006U"

    raise HTTPException(
        status_code=400,
        detail=f"지원하지 않는 action: {action}",
    )


# =========================================================
# HTTP Client 확인
# =========================================================

def get_client() -> httpx.AsyncClient:

    if http_client is None:
        raise RuntimeError("HTTP Client가 초기화되지 않았습니다.")

    return http_client


# =========================================================
# Telegram
# =========================================================

async def send_telegram_message(message: str):

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    try:

        client = get_client()

        response = await client.post(
            url,
            json=payload,
        )

        if response.status_code != 200:
            logger.warning(
                "Telegram 오류: %s",
                response.text,
            )

    except Exception as e:

        logger.exception(
            "Telegram 전송 실패: %s",
            e,
        )


# =========================================================
# Access Token
# =========================================================

async def get_access_token() -> str:

    global token_cache

    # 이미 살아있는 토큰
    if (
        token_cache["access_token"]
        and time.time() < token_cache["expires_at"]
    ):
        return token_cache["access_token"]

    # 동시 토큰 갱신 방지
    async with token_lock:

        # Lock 대기 중 다른 요청이 갱신했을 가능성
        if (
            token_cache["access_token"]
            and time.time() < token_cache["expires_at"]
        ):
            return token_cache["access_token"]

        logger.info("KIS Access Token 발급 요청")

        url = f"{BASE_URL}/oauth2/tokenP"

        body = {
            "grant_type": "client_credentials",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
        }

        client = get_client()

        response = await client.post(
            url,
            json=body,
        )

        if response.status_code != 200:

            raise HTTPException(
                status_code=500,
                detail=f"KIS 토큰 발급 실패: {response.text}",
            )

        data = response.json()

        access_token = data.get("access_token")

        if not access_token:

            raise HTTPException(
                status_code=500,
                detail="KIS Access Token이 없습니다.",
            )

        token_cache["access_token"] = access_token

        # 실제 만료보다 조금 여유 있게 사용
        token_cache["expires_at"] = (
            time.time() + 11 * 60 * 60
        )

        logger.info("KIS Access Token 발급 성공")

        return access_token


# =========================================================
# 공통 KIS Header
# =========================================================

async def get_kis_headers(
    tr_id: str,
) -> dict:

    token = await get_access_token()

    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }


# =========================================================
# KIS 해외주식 현재가
# =========================================================

async def get_current_price(
    ticker: str,
    exchange: str,
) -> Optional[float]:

    """
    해외주식 현재체결가

    TR_ID:
        HHDFS00000300
    """

    try:

        token = await get_access_token()

        url = (
            f"{BASE_URL}"
            f"/uapi/overseas-price/v1/quotations/price"
        )

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": "HHDFS00000300",
        }

        params = {
            "AUTH": "",
            "EXCD": exchange,
            "SYMB": ticker,
        }

        client = get_client()

        response = await client.get(
            url,
            headers=headers,
            params=params,
        )

        if response.status_code != 200:
            logger.warning(
                "현재가 조회 HTTP 오류: %s",
                response.text,
            )
            return None

        data = response.json()

        if data.get("rt_cd") != "0":
            logger.warning(
                "현재가 조회 실패: %s",
                data,
            )
            return None

        output = data.get("output", {})

        # KIS 현재체결가의 대표 필드
        price_candidates = [
            output.get("last"),
            output.get("last_pric"),
            output.get("ovrs_nmix_prpr"),
            output.get("stck_prpr"),
        ]

        for value in price_candidates:

            if value not in [None, ""]:

                try:
                    price = float(value)

                    if price > 0:
                        return price

                except (ValueError, TypeError):
                    pass

    except Exception as e:

        logger.exception(
            "현재가 조회 예외: %s",
            e,
        )

    return None


# =========================================================
# KIS 현재 1호가
# =========================================================

async def get_best_bid_ask(
    ticker: str,
    exchange: str,
) -> tuple[Optional[float], Optional[float]]:

    """
    해외주식 현재가 1호가

    TR_ID:
        HHDFS76200100
    """

    try:

        token = await get_access_token()

        url = (
            f"{BASE_URL}"
            f"/uapi/overseas-price/v1/quotations/"
            f"inquire-asking-price"
        )

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": "HHDFS76200100",
        }

        params = {
            "AUTH": "",
            "EXCD": exchange,
            "SYMB": ticker,
        }

        client = get_client()

        response = await client.get(
            url,
            headers=headers,
            params=params,
        )

        if response.status_code != 200:

            logger.warning(
                "호가 조회 HTTP 오류: %s",
                response.text,
            )

            return None, None

        data = response.json()

        logger.info(
            "KIS 호가 응답: %s",
            data,
        )

        if data.get("rt_cd") != "0":
            return None, None

        output = data.get("output", {})

        # KIS API 응답 명칭이 변경되더라도
        # 대표적인 후보 필드를 방어적으로 탐색
        bid_candidates = [
            output.get("pask1"),
            output.get("bidp1"),
            output.get("bid1"),
            output.get("pbid1"),
        ]

        ask_candidates = [
            output.get("pask1"),
            output.get("askp1"),
            output.get("ask1"),
            output.get("pask"),
        ]

        bid = None
        ask = None

        for value in bid_candidates:

            if value not in [None, ""]:

                try:

                    v = float(value)

                    if v > 0:
                        bid = v
                        break

                except (ValueError, TypeError):
                    pass

        for value in ask_candidates:

            if value not in [None, ""]:

                try:

                    v = float(value)

                    if v > 0:
                        ask = v
                        break

                except (ValueError, TypeError):
                    pass

        return bid, ask

    except Exception as e:

        logger.exception(
            "호가 조회 예외: %s",
            e,
        )

        return None, None


# =========================================================
# 계좌 잔고 조회
# =========================================================

async def get_balance_snapshot(
    ticker: str,
    exchange: str,
) -> dict:

    """
    해외주식 잔고조회
    TR_ID: TTTS3012R
    """

    token = await get_access_token()

    url = (
        f"{BASE_URL}"
        f"/uapi/overseas-stock/v1/trading/"
        f"inquire-balance"
    )

    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "TTTS3012R",
    }

    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "OVRS_EXCG_CD": exchange,
        "TR_CRCY_CD": "USD",
        "CTX_AREA_FK200": "",
        "CTX_AREA_NK200": "",
    }

    client = get_client()

    response = await client.get(
        url,
        headers=headers,
        params=params,
    )

    logger.info(
        "KIS 잔고 응답: %s",
        response.text,
    )

    if response.status_code != 200:

        raise HTTPException(
            status_code=500,
            detail=f"KIS 잔고조회 HTTP 오류: {response.text}",
        )

    data = response.json()

    if data.get("rt_cd") != "0":
        raise HTTPException(
            status_code=500,
            detail=f"KIS 잔고조회 실패: {data}",
        )

    output1 = data.get("output1", [])

    ticker = ticker.upper()

    for item in output1:

        symbol = str(
            item.get("ovrs_pdno", "")
        ).upper()

        if symbol == ticker:

            avg_price = float(
                item.get("pchs_avg_pric") or 0
            )

            holding_qty = float(
                item.get("ovrs_cblc_qty") or 0
            )

            return {
                "ticker": ticker,
                "avg_price": avg_price,
                "qty": holding_qty,
                "raw": item,
            }

    return {
        "ticker": ticker,
        "avg_price": 0.0,
        "qty": 0.0,
        "raw": None,
    }


# =========================================================
# 해외주식 주문
# =========================================================

async def place_order(
    action: str,
    ticker: str,
    exchange: str,
    price: float,
    qty: int,
) -> dict:

    action = action.lower().strip()

    tr_id = get_order_tr_id(action)

    headers = await get_kis_headers(tr_id)

    url = (
        f"{BASE_URL}"
        f"/uapi/overseas-stock/v1/trading/order"
    )

    body = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "OVRS_EXCG_CD": exchange,
        "PDNO": ticker,
        "ORD_SVR_DVSN_CD": "0",
        "ORD_QTY": str(int(qty)),
        "OVRS_ORD_UNPR": f"{price:.2f}",
        "ORD_DVSN": "00",
    }

    logger.info(
        "KIS 주문 요청 | action=%s ticker=%s exchange=%s "
        "price=%s qty=%s tr_id=%s",
        action,
        ticker,
        exchange,
        price,
        qty,
        tr_id,
    )

    client = get_client()

    response = await client.post(
        url,
        json=body,
        headers=headers,
    )

    logger.info(
        "KIS 주문 HTTP=%s response=%s",
        response.status_code,
        response.text,
    )

    if response.status_code != 200:

        raise HTTPException(
            status_code=500,
            detail=f"KIS 주문 HTTP 오류: {response.text}",
        )

    data = response.json()

    if data.get("rt_cd") != "0":

        raise HTTPException(
            status_code=500,
            detail=(
                f"KIS 주문 실패 | "
                f"{data.get('msg_cd')} | "
                f"{data.get('msg1')}"
            ),
        )

    return data


# =========================================================
# 해외주식 체결내역 조회
# =========================================================

async def inquire_filled_orders(
    ticker: str,
    exchange: str,
) -> list[dict]:

    """
    해외주식 주문체결내역

    TR_ID:
        TTTS3035R
    """

    token = await get_access_token()

    url = (
        f"{BASE_URL}"
        f"/uapi/overseas-stock/v1/trading/"
        f"inquire-ccnl"
    )

    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "TTTS3035R",
    }

    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": ticker,
        "ORD_STRT_DT": time.strftime("%Y%m%d"),
        "ORD_END_DT": time.strftime("%Y%m%d"),
        "SLL_BUY_DVSN": "00",
        "CCLD_NCCS_DVSN": "01",
        "OVRS_EXCG_CD": exchange,
        "SORT_SQN": "DS",
        "ORD_DT": "",
        "ORD_GNO_BRNO": "",
        "ODNO": "",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }

    client = get_client()

    response = await client.get(
        url,
        headers=headers,
        params=params,
    )

    logger.info(
        "KIS 체결조회 응답: %s",
        response.text,
    )

    if response.status_code != 200:
        return []

    data = response.json()

    if data.get("rt_cd") != "0":
        return []

    output1 = data.get("output1", [])

    if isinstance(output1, dict):
        output1 = [output1]

    return output1 or []


# =========================================================
# 체결 데이터 추출
# =========================================================

def extract_fill_info(
    rows: list[dict],
    odno: str,
) -> dict:

    """
    KIS 체결조회 원문에서
    해당 주문번호의 체결수량/체결가를 추출.

    필드명은 KIS 응답을 우선적으로 탐색하며
    예상 필드를 여러 개 지원.
    """

    matched = []

    for row in rows:

        row_odno = str(
            row.get("odno")
            or row.get("ODNO")
            or row.get("orgn_odno")
            or ""
        )

        if row_odno == str(odno):
            matched.append(row)

    if not matched:
        return {
            "filled_qty": 0,
            "avg_fill_price": 0.0,
            "rows": [],
        }

    total_qty = 0
    total_value = 0.0

    for row in matched:

        qty_value = (
            row.get("ft_ccld_qty")
            or row.get("ft_ccld_qty1")
            or row.get("ccld_qty")
            or row.get("exec_qty")
            or row.get("filled_qty")
            or row.get("tot_ccld_qty")
            or 0
        )

        price_value = (
            row.get("ft_ccld_unpr3")
            or row.get("ft_ccld_unpr")
            or row.get("ccld_unpr")
            or row.get("exec_price")
            or row.get("filled_price")
            or 0
        )

        try:
            qty = int(float(qty_value))
        except:
            qty = 0

        try:
            price = float(price_value)
        except:
            price = 0.0

        if qty > 0:

            total_qty += qty
            total_value += qty * price

    avg_fill_price = (
        total_value / total_qty
        if total_qty > 0
        else 0.0
    )

    return {
        "filled_qty": total_qty,
        "avg_fill_price": avg_fill_price,
        "rows": matched,
    }


# =========================================================
# 미체결 주문 조회
# =========================================================

async def inquire_unfilled_orders(
    ticker: str,
    exchange: str,
) -> list[dict]:

    """
    해외주식 미체결내역
    TR_ID: TTTS3018R
    """

    token = await get_access_token()

    url = (
        f"{BASE_URL}"
        f"/uapi/overseas-stock/v1/trading/"
        f"inquire-nccs"
    )

    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "TTTS3018R",
    }

    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "OVRS_EXCG_CD": exchange,
        "SORT_SQN": "DS",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }

    client = get_client()

    response = await client.get(
        url,
        headers=headers,
        params=params,
    )

    logger.info(
        "KIS 미체결 조회 응답: %s",
        response.text,
    )

    if response.status_code != 200:
        return []

    data = response.json()

    if data.get("rt_cd") != "0":
        return []

    output1 = data.get("output1", [])

    if isinstance(output1, dict):
        output1 = [output1]

    result = []

    for item in output1 or []:

        symbol = str(
            item.get("pdno")
            or item.get("ovrs_pdno")
            or ""
        ).upper()

        if symbol == ticker.upper():
            result.append(item)

    return result


# =========================================================
# 정정/취소
# =========================================================

async def cancel_overseas_order(
    odno: str,
    exchange: str,
    qty: int,
) -> Optional[dict]:

    """
    미국 해외주식 정정/취소

    TR_ID:
        TTTT1004U
    """

    token = await get_access_token()

    url = (
        f"{BASE_URL}"
        f"/uapi/overseas-stock/v1/trading/"
        f"order-rvsecncl"
    )

    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "TTTT1004U",
        "custtype": "P",
    }

    # 해외주식 정정취소 API의 실제 세부 필드는
    # KIS 규격에 따라 원주문번호/전송조직번호 등을 요구합니다.
    #
    # 최초 주문 응답에서 받은
    # KRX_FWDG_ORD_ORGNO / ODNO를 사용합니다.
    #
    # 이 함수는 주문 응답의 조직번호를 전달받도록 아래에서 호출합니다.

    raise NotImplementedError(
        "정정/취소는 최초 주문 응답의 "
        "KRX_FWDG_ORD_ORGNO를 포함하여 호출해야 합니다."
    )


# =========================================================
# 매도 주문 가격 결정
# =========================================================

async def get_sell_order_price(
    ticker: str,
    exchange: str,
    fallback_price: float,
) -> float:

    """
    SELL은 TradingView close를 무조건 사용하는 대신
    KIS 현재가를 확인합니다.

    일반 장중 시장가 ORD_DVSN=01은 사용하지 않습니다.

    KIS 해외주식 API에서는 장중 일반 시장가 대신
    지정가 주문을 사용합니다.
    """

    current_price = await get_current_price(
        ticker,
        exchange,
    )

    if current_price and current_price > 0:

        logger.info(
            "SELL 가격 기준 KIS 현재가: %.4f",
            current_price,
        )

        return current_price

    logger.warning(
        "KIS 현재가 조회 실패 → TradingView close 사용: %.4f",
        fallback_price,
    )

    return fallback_price


# =========================================================
# 실제 매도 처리
# =========================================================

async def process_sell(
    ticker: str,
    exchange: str,
    requested_qty: int,
    tradingview_price: float,
) -> dict:

    # -----------------------------------------------------
    # 1. 매도 직전 평단 Snapshot
    # -----------------------------------------------------

    snapshot = await get_balance_snapshot(
        ticker=ticker,
        exchange=exchange,
    )

    before_avg_price = snapshot["avg_price"]
    before_qty = snapshot["qty"]

    sell_qty = min(
        requested_qty,
        int(before_qty),
    )

    if sell_qty <= 0:

        raise HTTPException(
            status_code=400,
            detail=(
                f"{ticker} 매도 가능 수량이 없습니다. "
                f"보유수량={before_qty}"
            ),
        )

    logger.info(
        "매도 전 Snapshot | ticker=%s avg=%.4f qty=%s",
        ticker,
        before_avg_price,
        before_qty,
    )

    # -----------------------------------------------------
    # 2. SELL 가격 결정
    # -----------------------------------------------------

    order_price = await get_sell_order_price(
        ticker=ticker,
        exchange=exchange,
        fallback_price=tradingview_price,
    )

    # -----------------------------------------------------
    # 3. KIS 매도 주문
    # -----------------------------------------------------

    order_result = await place_order(
        action="sell",
        ticker=ticker,
        exchange=exchange,
        price=order_price,
        qty=sell_qty,
    )

    output = order_result.get("output", {})

    odno = output.get("ODNO", "")

    if not odno:

        logger.warning(
            "매도 주문번호 ODNO가 없습니다: %s",
            order_result,
        )

    # -----------------------------------------------------
    # 4. 주문 접수 Telegram
    # -----------------------------------------------------

    await send_telegram_message(
        f"🟡 <b>[매도 주문 접수]</b>\n"
        f"• 종목: {ticker}\n"
        f"• 수량: {sell_qty}주\n"
        f"• 주문가격: {order_price:,.2f} USD\n"
        f"• 주문번호: {odno}\n"
        f"• 상태: 체결 확인 중"
    )

    # -----------------------------------------------------
    # 5. 백그라운드 체결 감시
    # -----------------------------------------------------

    asyncio.create_task(
        monitor_sell_order(
            ticker=ticker,
            exchange=exchange,
            odno=odno,
            requested_qty=sell_qty,
            before_avg_price=before_avg_price,
            order_price=order_price,
        )
    )

    return order_result


# =========================================================
# 매도 체결 모니터
# =========================================================

async def monitor_sell_order(
    ticker: str,
    exchange: str,
    odno: str,
    requested_qty: int,
    before_avg_price: float,
    order_price: float,
):

    logger.info(
        "백그라운드 매도 체결감시 시작 | %s / %s",
        ticker,
        odno,
    )

    started_at = time.time()

    while (
        time.time() - started_at
        < FILL_TIMEOUT_SECONDS
    ):

        try:

            rows = await inquire_filled_orders(
                ticker=ticker,
                exchange=exchange,
            )

            fill_info = extract_fill_info(
                rows=rows,
                odno=odno,
            )

            filled_qty = fill_info["filled_qty"]
            avg_fill_price = fill_info["avg_fill_price"]

            if filled_qty >= requested_qty:

                profit = (
                    avg_fill_price - before_avg_price
                ) * filled_qty

                emoji = "🟢" if profit >= 0 else "🔴"

                await send_telegram_message(
                    f"{emoji} <b>[매도 체결 완료]</b>\n"
                    f"• 종목: {ticker}\n"
                    f"• 체결수량: {filled_qty}주\n"
                    f"• 실제 체결평균가: "
                    f"{avg_fill_price:,.4f} USD\n"
                    f"• 매도 전 평단: "
                    f"{before_avg_price:,.4f} USD\n"
                    f"• 실현손익: "
                    f"{profit:+,.2f} USD\n"
                    f"• 주문번호: {odno}"
                )

                logger.info(
                    "매도 체결 완료 | %s | qty=%s avg=%.4f profit=%.2f",
                    ticker,
                    filled_qty,
                    avg_fill_price,
                    profit,
                )

                return

            if filled_qty > 0:

                logger.info(
                    "부분체결 | %s | %s/%s",
                    ticker,
                    filled_qty,
                    requested_qty,
                )

            await asyncio.sleep(
                FILL_CHECK_INTERVAL
            )

        except Exception as e:

            logger.exception(
                "체결조회 오류 | %s | %s",
                ticker,
                e,
            )

            await asyncio.sleep(
                FILL_CHECK_INTERVAL
            )

    # -----------------------------------------------------
    # 시간 초과
    # -----------------------------------------------------

    logger.warning(
        "매도 체결감시 시간 초과 | %s | %s",
        ticker,
        odno,
    )

    await send_telegram_message(
        f"⚠️ <b>[매도 미체결 주의]</b>\n"
        f"• 종목: {ticker}\n"
        f"• 주문번호: {odno}\n"
        f"• 주문수량: {requested_qty}주\n"
        f"• 주문가격: {order_price:,.2f} USD\n"
        f"• 현재 주문이 즉시 전량 체결되지 않았습니다.\n"
        f"• 중복매도 방지를 위해 자동 재주문은 하지 않았습니다."
    )


# =========================================================
# BUY 처리
# =========================================================

async def process_buy(
    ticker: str,
    exchange: str,
    price: float,
    qty: int,
):

    result = await place_order(
        action="buy",
        ticker=ticker,
        exchange=exchange,
        price=price,
        qty=qty,
    )

    output = result.get("output", {})

    odno = output.get("ODNO", "-")

    await send_telegram_message(
        f"🟢 <b>[매수 주문 접수]</b>\n"
        f"• 종목: {ticker}\n"
        f"• 거래소: {exchange}\n"
        f"• 주문가격: {price:,.2f} USD\n"
        f"• 주문수량: {qty}주\n"
        f"• 주문번호: {odno}\n"
        f"• 상태: 주문 접수"
    )

    return result


# =========================================================
# Webhook
# =========================================================

@app.post("/webhook")
async def tradingview_webhook(
    signal: WebhookSignal,
):

    action = signal.action.strip().lower()
    ticker = signal.ticker.strip().upper()

    logger.info("=" * 60)
    logger.info("TradingView Webhook 수신")
    logger.info("Action  : %s", action)
    logger.info("Ticker  : %s", ticker)
    logger.info("Exchange: %s", signal.exchange)
    logger.info("Price   : %s", signal.price)
    logger.info("Qty     : %s", signal.qty)
    logger.info("=" * 60)

    if signal.qty <= 0:

        raise HTTPException(
            status_code=400,
            detail=f"잘못된 수량: {signal.qty}",
        )

    # -----------------------------------------------------
    # 중복 Webhook 방지
    # -----------------------------------------------------

    signal_key = (
        f"{action}|"
        f"{ticker}|"
        f"{signal.exchange}|"
        f"{signal.price}|"
        f"{signal.qty}"
    )

    now = time.time()

    async with order_lock:

        last_time = recent_signals.get(
            signal_key
        )

        if (
            last_time
            and now - last_time
            < SIGNAL_DEDUP_SECONDS
        ):

            logger.warning(
                "중복 Webhook 차단: %s",
                signal_key,
            )

            return {
                "status": "duplicate_ignored"
            }

        recent_signals[signal_key] = now

    # 오래된 키 정리
    for key in list(recent_signals.keys()):

        if (
            now - recent_signals[key]
            > 60
        ):
            recent_signals.pop(
                key,
                None,
            )

    # -----------------------------------------------------
    # 거래소 결정
    # -----------------------------------------------------

    kis_exchange = get_kis_exchange(
        ticker=ticker,
        tradingview_exchange=signal.exchange,
    )

    # -----------------------------------------------------
    # BUY
    # -----------------------------------------------------

    if action == "buy":

        result = await process_buy(
            ticker=ticker,
            exchange=kis_exchange,
            price=signal.price,
            qty=signal.qty,
        )

        return {
            "status": "success",
            "action": "buy",
            "result": result,
        }

    # -----------------------------------------------------
    # SELL
    # -----------------------------------------------------

    if action == "sell":

        # 매도는 체결조회/Telegram을 백그라운드에서
        # 진행하고 Webhook에는 즉시 응답
        task = asyncio.create_task(
            process_sell(
                ticker=ticker,
                exchange=kis_exchange,
                requested_qty=signal.qty,
                tradingview_price=signal.price,
            )
        )

        # task가 서버 수명 동안 유지되도록
        # 참조를 유지할 필요가 있는 경우를 대비해
        # done callback을 붙임
        def task_done_callback(t):

            try:
                exc = t.exception()

                if exc:
                    logger.error(
                        "백그라운드 SELL 처리 실패: %s",
                        exc,
                    )

                    asyncio.create_task(
                        send_telegram_message(
                            f"🚨 <b>[백그라운드 매도 처리 오류]</b>\n"
                            f"• 종목: {ticker}\n"
                            f"• 내용: {str(exc)}"
                        )
                    )

            except asyncio.CancelledError:
                pass

        task.add_done_callback(
            task_done_callback
        )

        return {
            "status": "accepted",
            "action": "sell",
            "message": "매도 주문 처리 시작",
        }

    raise HTTPException(
        status_code=400,
        detail=f"지원하지 않는 action: {action}",
    )


# =========================================================
# UptimeRobot / Health Check
# =========================================================

@app.api_route(
    "/",
    methods=["GET", "HEAD"],
)
async def health_check():

    return {
        "status": "running",
        "mode": "REAL",
        "service": "TradingView-KIS",
    }