# -*- coding: utf-8 -*-
"""
Personal Lens Market Research Tool

個人利用の中古カメラレンズ相場調査ツール。
eBay Browse APIで相場を調べ、国内フリマの候補と比較して
参考情報をLINEに通知する、ローカル実行のスクリプト。

判断ロジックの詳細な基準値(除外条件・利益率のしきい値など)は
business_rules.py に分離しており、このリポジトリには含まれていない
(個人の非公開ノウハウのため)。実行するには同ファイルを
自分で用意する必要がある(business_rules.py.example を参照)。

【事前準備】
1. pip install requests beautifulsoup4 selenium webdriver-manager
2. パソコンにMicrosoft Edgeがインストールされていること
3. config.py.example と business_rules.py.example を参考に、
   config.py と business_rules.py を自分で作成する
4. まずは TEST_MODE = True のまま実行して、動作確認する
"""

import base64
import csv
import json
import os
import random
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.edge.service import Service

try:
    from webdriver_manager.microsoft import EdgeChromiumDriverManager
    HAS_WEBDRIVER_MANAGER = True
except ImportError:
    HAS_WEBDRIVER_MANAGER = False

# 機密情報(APIキー等)はconfig.pyに分離している。
# 判断ロジックの基準値(除外キーワード・利益率のライン等)はbusiness_rules.pyに分離している。
# どちらもGitHubには公開せず、自分のパソコンにだけ置く(.gitignoreで除外済み)。
# 初回セットアップ: それぞれの .example ファイルをコピーし、実際の値を入力すること。
import config
import business_rules


# ============================================================
# 設定エリア（ここだけ書き換えればOK）
# ============================================================

EBAY_CLIENT_ID = config.EBAY_CLIENT_ID
EBAY_CLIENT_SECRET = config.EBAY_CLIENT_SECRET

LINE_CHANNEL_ACCESS_TOKEN = config.LINE_CHANNEL_ACCESS_TOKEN

# --- テストモード ---
# True: 候補リストの上から3件だけ処理して、動作確認する(数分で終わる)
# False: 本番の10時間ループで実行する
TEST_MODE = True
TEST_MODE_MAX_LENSES = 100000  # GOが1件見つかるまでは件数で打ち切らない

# GOが1件見つかった時点で自動終了する(精度確認用のテスト実行)
STOP_AFTER_FIRST_GO = True

# --- 1次検索のクエリ ---
# 主要5ブランドに絞って検索する(母数を絞ってAPI呼び出しを抑えるため)。
BROAD_QUERIES = [
    "Nikon MF lens",
    "Canon MF lens",
    "Olympus MF lens",
    "Contax MF lens",
    "Fujifilm MF lens",
]
BROAD_SEARCH_PAGE_SIZE = 200      # eBay APIの1回あたり最大取得件数
BROAD_SEARCH_TOTAL_TARGET = 2000  # 1次検索で集める候補の目標合計件数(ページ送りして集める)

# eBayの「Cameras & Photo > Lenses & Filters」カテゴリID。
# これを指定することで、カメラ本体+レンズのセット出品等が混ざらないようにする。
EBAY_LENS_CATEGORY_ID = "78997"

# --- 仕入れ・利益率の前提 ---
# 具体的な数値(仕入れ上限・利益率のライン等)は business_rules.py に分離している
PURCHASE_BUDGET_CEILING_JPY = business_rules.PURCHASE_BUDGET_CEILING_JPY
EBAY_FEE_RATE = business_rules.EBAY_FEE_RATE
CEILING_PROFIT_RATE = business_rules.CEILING_PROFIT_RATE
MIN_PROFIT_RATE_GO = business_rules.MIN_PROFIT_RATE_GO
SHIPPING_COST_JPY = business_rules.SHIPPING_COST_JPY
USD_JPY_RATE = 155                     # 為替レート。実行前に都度最新値に更新推奨

# --- eBay側リサーチの設定 ---
# Near MINTの最安値からMINTの最高値まで、対象範囲を漏れなく全件確認する。
# (件数上限を設けると相場の中央値がぶれるため)
# ただし暴走防止の安全上限として、1型番あたりこれ以上は取得しない。
MODEL_SEARCH_HARD_CAP = 500
MIN_CLEAN_SAMPLES_REQUIRED = 2         # 相場確定に必要な最低クリーンサンプル数
EBAY_DAILY_CALL_BUDGET = 5000
CALL_BUDGET_SAFETY_MARGIN = 200

# --- メルカリ・ヤフオク側リサーチの設定 ---
# 上位n件だけでなく、表示された候補は全て確認する。
# ただし暴走防止の安全上限としてこれ以上は見ない。
MERCARI_YAHOO_CHECK_HARD_CAP = 50
REQUEST_JITTER_RANGE_SEC = (1, 5)      # メルカリ・ヤフオクへのアクセス間のランダム待機幅

# --- 実行時間の上限(安全装置。GOが見つからず延々回り続けないための保険) ---
MAX_RUNTIME_HOURS = 10

# --- 判断ロジックの基準値(除外キーワード等)は business_rules.py に分離している ---
EXCLUDE_KEYWORDS_EN = business_rules.EXCLUDE_KEYWORDS_EN
EXCLUDE_KEYWORDS_JP = business_rules.EXCLUDE_KEYWORDS_JP
DISCLAIMER_MARKERS_JP = business_rules.DISCLAIMER_MARKERS_JP
EXCLUDE_BUNDLE_KEYWORDS = business_rules.EXCLUDE_BUNDLE_KEYWORDS
CONDITION_KEYWORDS = business_rules.CONDITION_KEYWORDS
TITLE_NOISE_PATTERNS = business_rules.TITLE_NOISE_PATTERNS
GENERIC_FILLER_WORDS = business_rules.GENERIC_FILLER_WORDS
CORE_QUERY_MAX_WORDS = business_rules.CORE_QUERY_MAX_WORDS

# 「今日すでに処理した型番」は日付ごとにリセットする(日をまたげば再調査してよい)
PROCESSED_LENSES_FILE = f"researched_lens_models_v2_{datetime.now().strftime('%Y%m%d')}.txt"

# eBay相場(中央値)のキャッシュ。こちらは日付でリセットしない(ずっと蓄積する)。
# 一度相場を出した型番は、次回以降このキャッシュを再利用することで、
# 同じ型番のeBay側の再調査(API呼び出し)を省略できるようにする。
# 相場は時系列でも残す(値上がり/値下がりのトレンドを後から見られるように)。
MARKET_PRICE_CACHE_FILE = "ebay_market_price_cache.json"
PRICE_HISTORY_MAX_ENTRIES = 20  # 1型番あたり保存する履歴の最大件数

# 手動で相場・仕入れ実績等を追記するための上書きファイル。
# 自分で「このレンズは〇〇ドルだった」等と手動で確認した情報をここに追記し、
# 起動時にキャッシュへ取り込む。自動調査した結果を上書きしても構わない。
MANUAL_PRICE_FILE = "manual_market_prices.json"

# 何度調べてもクリーンサンプルが集まらない/仕入れ候補が見つからない型番は、
# 「弱い型番」としてクールダウン期間を設け、無駄な再調査を減らす。
WEAK_MODELS_FILE = "weak_models.json"
WEAK_MODEL_FAIL_THRESHOLD = 3      # この回数連続で失敗したらクールダウン対象にする
WEAK_MODEL_COOLDOWN_DAYS = 30      # クールダウン期間(日)

HEADERS_HTTP = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


# ============================================================
# 共通ユーティリティ
# ============================================================


class CallBudget:
    """
    eBay API呼び出し回数を追跡し、予算超過を防ぐ。
    プログラムを1日に何度も起動し直しても正しく累計できるよう、
    「その日の使用回数」をファイルに記録し、起動のたびに読み込む。
    """

    def __init__(self, daily_budget, safety_margin):
        self.limit = daily_budget - safety_margin
        self.usage_file = f"ebay_api_usage_{datetime.now().strftime('%Y%m%d')}.txt"
        self.count = self._load()

    def _load(self):
        if os.path.exists(self.usage_file):
            try:
                with open(self.usage_file, "r", encoding="utf-8") as f:
                    return int(f.read().strip())
            except (ValueError, OSError):
                return 0
        return 0

    def _save(self):
        with open(self.usage_file, "w", encoding="utf-8") as f:
            f.write(str(self.count))

    def use(self, n=1):
        self.count += n
        self._save()

    def remaining(self):
        return self.limit - self.count

    def can_afford(self, n):
        return self.remaining() >= n


class RuntimeBudget:
    """一晩(最大N時間)の実行時間を管理する"""

    def __init__(self, max_hours):
        self.start_time = time.time()
        self.max_seconds = max_hours * 3600

    def elapsed_hours(self):
        return (time.time() - self.start_time) / 3600

    def expired(self):
        return (time.time() - self.start_time) >= self.max_seconds


def jitter_sleep():
    time.sleep(random.uniform(*REQUEST_JITTER_RANGE_SEC))


REQUEST_TIMEOUT_SEC = 15   # 1回の通信が応答するまで待つ最大秒数
MAX_NETWORK_RETRIES = 3    # 通信エラー時の再試行回数
RETRY_WAIT_SEC = 5         # 再試行前の待機秒数


def with_retry(func, *args, **kwargs):
    """
    通信エラー(接続切れ・タイムアウト等)が起きた場合、
    RETRY_WAIT_SEC待ってから最大MAX_NETWORK_RETRIES回まで再試行する。
    404等の「正常な応答としてのエラー」はリトライ対象にしない
    (呼び出し元のraise_for_status()でHTTPErrorとして扱われる)。
    それでも失敗したら例外をそのまま呼び出し元に投げる。
    """
    last_error = None
    for attempt in range(1, MAX_NETWORK_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = e
            print(f"    (通信エラー: {e} / {attempt}回目、{RETRY_WAIT_SEC}秒待って再試行します)")
            time.sleep(RETRY_WAIT_SEC)
    raise last_error


def contains_any(text, keywords):
    text_lower = (text or "").lower()
    return any(k.lower() in text_lower for k in keywords)


def is_clean_condition(combined_text_en_or_jp, exclude_keywords):
    """カビ・曇り・動作不良等の除外キーワードが含まれていないか(英語向け・単純一致)"""
    return not contains_any(combined_text_en_or_jp, exclude_keywords)


def find_condition_exclusion_jp(combined_text):
    """
    日本語の商品説明を文単位でチェックし、除外キーワードが
    「実際の不具合の記載」なのか「万が一〜の場合はご連絡くださいのような
    免責文言」なのかを見分ける。
    免責文言と判定された場合はそのキーワードを除外理由として扱わない。
    見つかった実際の不具合キーワードを返す。なければNoneを返す。
    """
    sentences = re.split(r"[。！？\n]", combined_text)
    for sentence in sentences:
        hit = next((k for k in EXCLUDE_KEYWORDS_JP if k in sentence), None)
        if hit and not contains_any(sentence, DISCLAIMER_MARKERS_JP):
            return hit
    return None


def is_clean_condition_jp(combined_text):
    """日本語の商品説明が、免責文言を除いてクリーンかどうか"""
    return find_condition_exclusion_jp(combined_text) is None


# ============================================================
# 型番推定・検索クエリ生成(取り違え防止つき)
# ============================================================


def guess_model_key(title):
    """調査済み型番の重複チェック用キー(小文字・正規化)"""
    text = title.lower()
    text = re.sub(r"\[[^\]]*\]", " ", text)   # [MINT w/ Cap] のような角括弧タグを除去
    text = re.sub(r"\([^)]*\)", " ", text)    # (MINT) のような丸括弧タグも除去
    for pattern in TITLE_NOISE_PATTERNS:
        text = re.sub(pattern, " ", text)
    text = re.sub(r"[^\w\s./+-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_search_query(title):
    """
    eBay再検索用のクエリを作る(型番取り違え防止のため、正確さ優先)。
    「[MINT w/ Cap]」のような角括弧タグ、グレード表記(MINT/Near MINT等)、
    「From JAPAN」等の定型句だけを取り除き、残りの単語は削らずそのまま使う。
    (単語を勝手に間引くと、Wide/Angle等の必要な単語まで消えて検索が薄くなるため)
    """
    text = title
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    for pattern in TITLE_NOISE_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_domestic_search_query(title):
    """
    メルカリ・ヤフオク検索用の短いクエリを作る。
    eBay用のフルタイトルをそのまま日本のマーケットで検索すると、長すぎて
    ほぼヒットしない(0件やエラーになる)ため、ブランド・型番・焦点距離・
    マウント等の「型番を特定するのに必要な単語」だけに絞る。
    世代・マウントを区別する単語(FD, New FD, Ai, Ai-S, SSC, SMC, MD, MC,
    Rokkor, Takumar, M42等)は誤って削らないよう、フィラー語リストには含めない。
    """
    text = build_search_query(title)
    words = [w for w in text.split() if w.lower() not in GENERIC_FILLER_WORDS]
    return " ".join(words[:CORE_QUERY_MAX_WORDS])


def normalize_for_match(text):
    text = text.lower()
    text = text.replace(".", "").replace(",", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def title_matches_keyword(title, keyword):
    """
    検索クエリの単語が全部タイトルに含まれているか確認する(型番取り違え防止)。
    ハイフンの有無等の表記ゆれは吸収するが、それ以外は厳密にチェックする。
    """
    title_norm = normalize_for_match(title)
    keyword_norm = normalize_for_match(keyword)
    for token in keyword_norm.split():
        variants = {token, token.replace("-", ""), token.replace("-", " ")}
        if not any(v in title_norm for v in variants):
            return False
    return True


# ============================================================
# eBay API
# ============================================================


def get_ebay_token(budget):
    credentials = f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}"
    b64 = base64.b64encode(credentials.encode()).decode()
    headers = {
        "Authorization": f"Basic {b64}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }

    def _call():
        resp = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers=headers, data=data, timeout=REQUEST_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return resp

    resp = with_retry(_call)
    budget.use(1)
    return resp.json()["access_token"]


class TokenHolder:
    """
    eBayのアクセストークンは約2時間で失効する。
    失効してから401エラーで気づくのではなく、100分経過した時点で
    自分から新しいトークンを取りに行くことで、長時間実行中の
    401エラー連発を防ぐ。
    """

    TOKEN_LIFETIME_SEC = 100 * 60  # 100分(実際の有効期限2時間より早めに更新)

    def __init__(self, budget):
        self.budget = budget
        self.token = get_ebay_token(budget)
        self.obtained_at = time.time()

    def refresh_if_needed(self):
        if time.time() - self.obtained_at > self.TOKEN_LIFETIME_SEC:
            print("    (eBayトークンの有効期限が近いため更新します)")
            self.token = get_ebay_token(self.budget)
            self.obtained_at = time.time()


def ebay_search_listings(token, budget, query, price_max_usd=None, limit=50, offset=0):
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "q": query, "limit": min(limit, 200), "offset": offset, "sort": "-price",
        "category_ids": EBAY_LENS_CATEGORY_ID,  # 「Lenses & Filters」カテゴリに限定
    }
    if price_max_usd is not None:
        params["filter"] = f"price:[0..{price_max_usd:.2f}],priceCurrency:USD"

    def _call():
        resp = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers=headers, params=params, timeout=REQUEST_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return resp

    resp = with_retry(_call)
    budget.use(1)
    data = resp.json()
    return data.get("itemSummaries", []), data.get("total", 0)


def ebay_get_item_detail(token, budget, item_id):
    headers = {"Authorization": f"Bearer {token}"}

    def _call():
        return requests.get(
            f"https://api.ebay.com/buy/browse/v1/item/{item_id}",
            headers=headers, timeout=REQUEST_TIMEOUT_SEC,
        )

    resp = with_retry(_call)
    budget.use(1)
    if resp.status_code != 200:
        return None
    return resp.json()


# --- 仕入れ上限(円)からeBay検索価格上限(USD)を逆算 ---
# 仕入れ上限(円) → 利益率20%乗せ → 送料加算 → eBay手数料15%を加味して逆算 → USD換算
def calc_ebay_price_ceiling_usd(purchase_budget_jpy):
    with_profit = purchase_budget_jpy * (1 + CEILING_PROFIT_RATE)
    with_shipping = with_profit + SHIPPING_COST_JPY
    ebay_target_sell_jpy = with_shipping / (1 - EBAY_FEE_RATE)
    return ebay_target_sell_jpy / USD_JPY_RATE


def research_lens_model_on_ebay(token, budget, title, verbose=False):
    """
    1つの型番について、eBayで再検索し、MINT/Near MINT・付属品なし・
    状態矛盾記載なしの出品だけを残して、最高値と最安値の中央値(単純平均)を
    そのレンズのeBay相場とする。
    Near MINTの最安値からMINTの最高値まで、対象範囲を漏れなく確認する
    (件数上限を設けると中央値がぶれるため、MODEL_SEARCH_HARD_CAPまでは全件見る)。
    """
    # eBay再検索も、メルカリ・ヤフオクと同じ「短いコア型番」で行う。
    # フルタイトルのままだと、出品者独自の在庫コードや宣伝文句(例:"US STOCK",
    # "Hood PF001239")まで含まれてしまい、ほぼその出品者1件しかヒットしない
    # 状態になってしまうため。
    query = build_domestic_search_query(title)
    if not query:
        return None

    # ページ送りしながら全件取得する
    all_results = []
    offset = 0
    total = 0
    while offset < MODEL_SEARCH_HARD_CAP:
        if not budget.can_afford(1):
            break
        page, total = ebay_search_listings(
            token, budget, query, limit=min(200, MODEL_SEARCH_HARD_CAP - offset), offset=offset
        )
        if not page:
            break
        all_results.extend(page)
        offset += len(page)
        if offset >= total:
            break
    results = all_results
    if verbose:
        print(f"      → eBay再検索ヒット数(全体): {total}件 / 今回取得: {len(results)}件(価格が高い順)")

    before_mismatch = len(results)
    results = [r for r in results if title_matches_keyword(r.get("title", ""), query)]
    if verbose:
        removed = before_mismatch - len(results)
        print(f"      → 型番不一致で除外: {removed}件（残り{len(results)}件）")

    before_bundle = len(results)
    results = [
        r for r in results
        if not contains_any(r.get("title", ""), EXCLUDE_BUNDLE_KEYWORDS)
    ]
    if verbose:
        removed = before_bundle - len(results)
        print(f"      → 付属品/セット品で除外: {removed}件（残り{len(results)}件）")
        print(f"      → 残った候補を全件確認します({len(results)}件):")

    # eBay検索結果(サマリー)の時点でconditionフィールドが
    # "FOR_PARTS_OR_NOT_WORKING"(ジャンク品)のものは、詳細取得するまでもなく
    # 確実に除外対象なので、ここで弾いてAPI呼び出しを1回節約する。
    before_junk = len(results)
    results = [
        r for r in results
        if r.get("condition", "").upper() != "FOR_PARTS_OR_NOT_WORKING"
    ]
    if verbose:
        removed = before_junk - len(results)
        if removed:
            print(f"      → ジャンク品表記で除外(詳細取得スキップ): {removed}件（残り{len(results)}件）")

    clean_prices = []
    checked = 0
    for idx, item in enumerate(results, 1):
        if not budget.can_afford(1):
            if verbose:
                print(f"      → API呼び出し予算が残り少ないため、ここで確認を打ち切ります")
            break
        item_title = item.get("title", "")
        item_price = item.get("price", {}).get("value", "?")

        # MINT/Near MINTという文言の有無は問わない。
        # カビ・曇り・動作不良等の除外キーワードさえなければMINT/Near MINT相当として扱う。
        detail = ebay_get_item_detail(token, budget, item.get("itemId"))
        checked += 1
        if not detail:
            if verbose:
                print(f"        [{idx}] ${item_price} {item_title[:60]}")
                print(f"             → 詳細取得に失敗。スキップ")
            continue

        description = detail.get("shortDescription", "") or ""
        combined = item_title + " " + description

        if verbose:
            print(f"        [{idx}] ${item_price} {item_title[:60]}")

        if not is_clean_condition(combined, EXCLUDE_KEYWORDS_EN):
            hit_word = next((k for k in EXCLUDE_KEYWORDS_EN if k.lower() in combined.lower()), "?")
            if verbose:
                print(f"             → 除外キーワード「{hit_word}」の記載あり。対象外")
            continue

        price_val = detail.get("price", {}).get("value")
        if price_val:
            clean_prices.append(float(price_val))
            if verbose:
                print(f"             → クリーン品として採用（${float(price_val):.2f}）")

    if len(clean_prices) < MIN_CLEAN_SAMPLES_REQUIRED:
        if verbose:
            print(f"      → クリーンサンプル{len(clean_prices)}件（{MIN_CLEAN_SAMPLES_REQUIRED}件未満のため相場確定不可）")
        return {
            "query": query, "total_listings": total,
            "clean_sample_count": len(clean_prices), "market_price_usd": None,
        }

    market_price_usd = (max(clean_prices) + min(clean_prices)) / 2
    if verbose:
        print(f"      → クリーンサンプル{len(clean_prices)}件の価格一覧: " +
              ", ".join(f"${p:.2f}" for p in sorted(clean_prices, reverse=True)))
        print(f"      → 最高値${max(clean_prices):.2f} / 最安値${min(clean_prices):.2f} → "
              f"中央値(相場) = (${max(clean_prices):.2f} + ${min(clean_prices):.2f}) ÷ 2 = ${market_price_usd:.2f}")
    return {
        "query": query, "total_listings": total,
        "clean_sample_count": len(clean_prices),
        "market_price_usd": market_price_usd,
    }


# ============================================================
# ヤフオク検索
# ============================================================


def search_yahoo_auction(keyword):
    url = "https://auctions.yahoo.co.jp/search/search"
    params = {"p": keyword, "va": keyword, "n": 50}

    def _call():
        resp = requests.get(url, params=params, headers=HEADERS_HTTP, timeout=REQUEST_TIMEOUT_SEC)
        resp.raise_for_status()
        return resp

    resp = with_retry(_call)
    jitter_sleep()

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for item in soup.select("li.Product"):
        title_tag = item.select_one("a.Product__titleLink")
        price_tag = item.select_one(".Product__priceValue")
        if not title_tag or not price_tag:
            continue
        title = title_tag.get_text(strip=True)
        price_text = price_tag.get_text(strip=True).replace(",", "").replace("円", "")
        try:
            price = int(price_text)
        except ValueError:
            continue
        link = title_tag.get("href")
        if link:
            results.append({"title": title, "price": price, "url": link, "source": "ヤフオク"})
    # 価格が高い順に並べる
    results.sort(key=lambda r: r["price"], reverse=True)
    return results


def get_yahoo_item_detail(url):
    def _call():
        resp = requests.get(url, headers=HEADERS_HTTP, timeout=REQUEST_TIMEOUT_SEC)
        resp.raise_for_status()
        return resp

    resp = with_retry(_call)
    jitter_sleep()
    soup = BeautifulSoup(resp.text, "html.parser")

    condition_text = ""
    for dt in soup.find_all("dt"):
        if dt.get_text(strip=True) == "商品の状態":
            dd = dt.find_next_sibling("dd")
            if dd:
                condition_text = dd.get_text(strip=True)
            break

    # 即決価格(Buy It Now)の有無を確認する。
    # オークション形式のみで即決価格が無い場合、検索結果の「現在価格」は
    # 入札が少ない/無い状態の暫定額に過ぎず、実際にいくらで落札できるか
    # 保証がないため、これを仕入れ候補価格として使うのは危険。
    buy_now_price = None
    for dt in soup.find_all("dt"):
        label = dt.get_text(strip=True)
        if label in ("即決価格", "即決"):
            dd = dt.find_next_sibling("dd")
            if dd:
                digits = re.sub(r"[^0-9]", "", dd.get_text())
                if digits:
                    buy_now_price = int(digits)
            break

    description_text = ""
    script_tag = soup.find("script", id="__NEXT_DATA__")
    if script_tag and script_tag.string:
        import json
        try:
            data = json.loads(script_tag.string)
            item = (
                data.get("props", {}).get("pageProps", {})
                .get("initialState", {}).get("item", {})
                .get("detail", {}).get("item", {})
            )
            desc_html = item.get("descriptionHtml", "")
            if desc_html:
                description_text = BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True)
            else:
                description_text = " ".join(item.get("description", []))
        except Exception:
            description_text = ""

    return condition_text, description_text, buy_now_price


# ============================================================
# メルカリ検索(Selenium)
# ============================================================


def create_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,2000")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )
    if HAS_WEBDRIVER_MANAGER:
        service = Service(EdgeChromiumDriverManager().install())
        driver = webdriver.Edge(service=service, options=options)
    else:
        driver = webdriver.Edge(options=options)
    # ページ読み込みが固まった際に何時間もフリーズしないよう、上限を設定する
    driver.set_page_load_timeout(30)
    driver.set_script_timeout(30)
    return driver


def search_mercari(driver, keyword):
    url = f"https://jp.mercari.com/search?keyword={keyword}&status=on_sale"
    driver.get(url)
    time.sleep(3)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    results = []
    for item in soup.select("li[data-testid='item-cell']"):
        link_tag = item.select_one("a")
        if not link_tag:
            continue
        href = link_tag.get("href", "")
        if not href:
            continue
        full_url = "https://jp.mercari.com" + href if href.startswith("/") else href
        title = item.get_text(" ", strip=True)
        # CSSクラス名は変わりやすく誤検知の元になるため使わない。
        # 「¥」記号の直後の数字を価格として抜き出す(価格表示は必ず¥から始まるため)。
        price_match = re.search(r"¥\s?([\d,]+)", title)
        price = int(price_match.group(1).replace(",", "")) if price_match else None
        results.append({"title": title[:100], "url": full_url, "price": price, "source": "メルカリ"})
    results = [r for r in results if r["price"] is not None]
    results.sort(key=lambda r: r["price"], reverse=True)
    return results


def get_mercari_item_detail(driver, url):
    driver.get(url)
    time.sleep(3)
    jitter_sleep()
    soup = BeautifulSoup(driver.page_source, "html.parser")

    condition_text = ""
    for el in soup.find_all(["dt", "span", "div"]):
        if el.get_text(strip=True) == "商品の状態":
            sib = el.find_next_sibling()
            if sib:
                condition_text = sib.get_text(strip=True)
            break

    description_tag = soup.find("pre", attrs={"data-testid": "description"})
    description_text = description_tag.get_text(" ", strip=True) if description_tag else ""

    return condition_text, description_text


# ============================================================
# 仕入れ候補探索(メルカリ・ヤフオク共通ロジック)
# ============================================================


def find_purchase_candidates(driver, lens_title, search_query, verbose=False):
    """
    メルカリ・ヤフオクで、レンズ名のみで検索し(MINT等のキーワードは付けない)、
    表示された候補を全て確認する(安全上限MERCARI_YAHOO_CHECK_HARD_CAPまで)。
    除外キーワード(カビ・曇り・動作不良・凹み・バルサム切れ・油染み)が
    書かれていないものを全て仕入れ候補として返す(1件に絞らない)。
    型番取り違え防止のため、タイトルに検索クエリの単語が全部含まれるかも確認する。
    """
    candidates = []

    # --- ヤフオク ---
    try:
        yahoo_results = search_yahoo_auction(search_query)
        check_count = min(MERCARI_YAHOO_CHECK_HARD_CAP, len(yahoo_results))
        if verbose:
            print(f"      → ヤフオク検索結果: {len(yahoo_results)}件中、{check_count}件を確認")
        for idx, r in enumerate(yahoo_results[:MERCARI_YAHOO_CHECK_HARD_CAP], 1):
            if not title_matches_keyword(r["title"], search_query):
                if verbose:
                    print(f"        [ヤフオク{idx}] ¥{r['price']} {r['title'][:50]}")
                    print(f"             → 型番不一致。対象外")
                continue
            condition_text, description_text, buy_now_price = get_yahoo_item_detail(r["url"])
            combined = r["title"] + " " + condition_text + " " + description_text
            if verbose:
                print(f"        [ヤフオク{idx}] ¥{r['price']}(現在価格) {r['title'][:50]}")
                print(f"             状態欄: {condition_text or '(記載なし)'}")

            if buy_now_price is None:
                if verbose:
                    print(f"             → 即決価格なし(オークション形式のみ)。落札額が確定しないため対象外")
                continue

            if not is_clean_condition_jp(combined):
                hit_word = find_condition_exclusion_jp(combined)
                if verbose:
                    print(f"             → 除外キーワード「{hit_word}」の記載あり(免責文言ではない)。対象外")
                continue

            r = dict(r)
            r["price"] = buy_now_price  # 仕入れ価格は「現在価格」ではなく確実な即決価格を使う
            candidates.append(r)
            if verbose:
                print(f"             → 即決価格¥{buy_now_price}で仕入れ候補として採用")
    except Exception as e:
        print(f"    (ヤフオク検索でエラー: {e})")

    # --- メルカリ ---
    try:
        mercari_results = search_mercari(driver, search_query)
        check_count = min(MERCARI_YAHOO_CHECK_HARD_CAP, len(mercari_results))
        if verbose:
            print(f"      → メルカリ検索結果: {len(mercari_results)}件中、{check_count}件を確認")
        for idx, r in enumerate(mercari_results[:MERCARI_YAHOO_CHECK_HARD_CAP], 1):
            if not title_matches_keyword(r["title"], search_query):
                if verbose:
                    print(f"        [メルカリ{idx}] ¥{r['price']} {r['title'][:50]}")
                    print(f"             → 型番不一致。対象外")
                continue
            condition_text, description_text = get_mercari_item_detail(driver, r["url"])
            combined = r["title"] + " " + condition_text + " " + description_text
            if verbose:
                print(f"        [メルカリ{idx}] ¥{r['price']} {r['title'][:50]}")
                print(f"             状態欄: {condition_text or '(記載なし)'}")
            if is_clean_condition_jp(combined):
                candidates.append(r)
                if verbose:
                    print(f"             → 除外キーワードなし。仕入れ候補として採用")
            else:
                hit_word = find_condition_exclusion_jp(combined)
                if verbose:
                    print(f"             → 除外キーワード「{hit_word}」の記載あり(免責文言ではない)。対象外")
    except Exception as e:
        print(f"    (メルカリ検索でエラー: {e})")

    if verbose:
        print(f"      → 仕入れ候補は合計{len(candidates)}件でした")
    return candidates


# ============================================================
# 利益率計算
# ============================================================


def calc_profit_rate(market_price_usd, purchase_price_jpy):
    if not market_price_usd or not purchase_price_jpy or purchase_price_jpy <= 0:
        return None
    sale_price_jpy = market_price_usd * USD_JPY_RATE
    ebay_fee = sale_price_jpy * EBAY_FEE_RATE
    net_received = sale_price_jpy - ebay_fee - SHIPPING_COST_JPY
    profit_jpy = net_received - purchase_price_jpy
    return profit_jpy / purchase_price_jpy


# ============================================================
# 調査済み型番の記録
# ============================================================


def load_processed_models():
    if not os.path.exists(PROCESSED_LENSES_FILE):
        return set()
    with open(PROCESSED_LENSES_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def append_processed_model(model_key):
    with open(PROCESSED_LENSES_FILE, "a", encoding="utf-8") as f:
        f.write(model_key + "\n")


def load_market_price_cache():
    if not os.path.exists(MARKET_PRICE_CACHE_FILE):
        return {}
    try:
        with open(MARKET_PRICE_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_market_price_cache(cache):
    with open(MARKET_PRICE_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def record_market_price(cache, model_key, research):
    """
    eBayで算出した相場をキャッシュに保存する。
    履歴(history)に今回の結果を積み増し、最新のものが検索・利益率計算で
    そのまま使えるよう research 自体もトップレベルに保持する。
    """
    entry = cache.get(model_key, {})
    history = entry.get("history", [])
    history.append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "market_price_usd": research["market_price_usd"],
        "clean_sample_count": research["clean_sample_count"],
        "total_listings": research["total_listings"],
    })
    history = history[-PRICE_HISTORY_MAX_ENTRIES:]

    entry.update(research)
    entry["source"] = "auto"
    entry["history"] = history
    cache[model_key] = entry


def load_manual_price_overrides():
    """
    手動で追記した相場・仕入れ実績等の上書きファイルを読み込み、
    キャッシュに取り込む形に変換する。
    形式: { "型番キー": {"market_price_usd": 500.0, "note": "手動確認済み"} }
    """
    if not os.path.exists(MANUAL_PRICE_FILE):
        return {}
    try:
        with open(MANUAL_PRICE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def merge_manual_overrides(cache, manual_overrides):
    merged_count = 0
    for model_key, manual_entry in manual_overrides.items():
        if "market_price_usd" not in manual_entry:
            continue
        entry = cache.get(model_key, {})
        history = entry.get("history", [])
        history.append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "market_price_usd": manual_entry["market_price_usd"],
            "clean_sample_count": manual_entry.get("clean_sample_count", "手動"),
            "total_listings": manual_entry.get("total_listings", "手動"),
        })
        entry.update({
            "query": manual_entry.get("query", model_key),
            "total_listings": manual_entry.get("total_listings", 0),
            "clean_sample_count": manual_entry.get("clean_sample_count", 0),
            "market_price_usd": manual_entry["market_price_usd"],
            "source": "manual",
            "note": manual_entry.get("note", ""),
            "history": history[-PRICE_HISTORY_MAX_ENTRIES:],
        })
        cache[model_key] = entry
        merged_count += 1
    return merged_count


# ============================================================
# 弱い型番(何度調べても成果が出ない型番)の記録
# ============================================================


def load_weak_models():
    if not os.path.exists(WEAK_MODELS_FILE):
        return {}
    try:
        with open(WEAK_MODELS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_weak_models(weak_models):
    with open(WEAK_MODELS_FILE, "w", encoding="utf-8") as f:
        json.dump(weak_models, f, ensure_ascii=False, indent=2)


def is_in_cooldown(weak_models, model_key):
    entry = weak_models.get(model_key)
    if not entry:
        return False
    if entry.get("fail_count", 0) < WEAK_MODEL_FAIL_THRESHOLD:
        return False
    last_date = datetime.strptime(entry["last_failed_date"], "%Y-%m-%d")
    days_since = (datetime.now() - last_date).days
    return days_since < WEAK_MODEL_COOLDOWN_DAYS


def record_weak_model_result(weak_models, model_key, success):
    if success:
        # 成功したらクールダウン対象から外す
        weak_models.pop(model_key, None)
        return
    entry = weak_models.get(model_key, {"fail_count": 0})
    entry["fail_count"] = entry.get("fail_count", 0) + 1
    entry["last_failed_date"] = datetime.now().strftime("%Y-%m-%d")
    weak_models[model_key] = entry


# ============================================================
# CSV出力
# ============================================================

CSV_FIELDNAMES = [
    "検索キーワード", "eBay目標売値(USD)", "eBay総出品数(需給参考)",
    "eBayクリーン品サンプル数", "仕入れ候補価格(円)", "仕入れ先",
    "仕入れ候補URL", "想定利益率", "判定",
]


def append_csv_row(filename, row, write_header):
    with open(filename, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# ============================================================
# LINE通知
# ============================================================


def send_line_broadcast(message):
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {"messages": [{"type": "text", "text": message}]}
    resp = requests.post(url, headers=headers, json=body)
    resp.raise_for_status()


def send_go_list_to_line(go_rows):
    """GOになったレンズを、利益率が高い順にLINEへ送る(長文はメッセージ分割)"""
    if not go_rows:
        send_line_broadcast("今回の実行でGO判定のレンズはありませんでした。")
        return

    go_rows_sorted = sorted(
        go_rows, key=lambda r: float(r["想定利益率"].rstrip("%")), reverse=True
    )

    lines = [f"【GOリスト】{len(go_rows_sorted)}件（利益率が高い順）\n"]
    for i, r in enumerate(go_rows_sorted, 1):
        lines.append(
            f"{i}. {r['検索キーワード'][:60]}\n"
            f"   利益率: {r['想定利益率']}\n"
            f"   仕入れ候補: ¥{r['仕入れ候補価格(円)']}（{r['仕入れ先']}）\n"
            f"   {r['仕入れ候補URL']}\n"
        )

    message = ""
    for line in lines:
        if len(message) + len(line) > 4500:
            send_line_broadcast(message)
            message = ""
        message += line
    if message:
        send_line_broadcast(message)


# ============================================================
# メイン処理
# ============================================================


def main():
    call_budget = CallBudget(EBAY_DAILY_CALL_BUDGET, CALL_BUDGET_SAFETY_MARGIN)
    runtime_budget = RuntimeBudget(MAX_RUNTIME_HOURS)

    print(f"本日すでに使用したAPI呼び出し: {call_budget.count}回 / 上限{call_budget.limit}回")
    if not call_budget.can_afford(100):
        print(
            "本日のAPI呼び出し予算をほぼ使い切っています。"
            "eBay側の実際の上限にも達している可能性が高いため、"
            "今日はこれ以上実行せず、日付が変わってから再実行してください。"
        )
        return

    print("eBayトークンを取得中...")
    token_holder = TokenHolder(call_budget)
    token = token_holder.token

    price_ceiling_usd = calc_ebay_price_ceiling_usd(PURCHASE_BUDGET_CEILING_JPY)
    print(
        f"仕入れ上限 ¥{PURCHASE_BUDGET_CEILING_JPY:,}（利益率{CEILING_PROFIT_RATE*100:.0f}%・"
        f"送料込み・eBay手数料{EBAY_FEE_RATE*100:.0f}%考慮）→ "
        f"eBay価格上限 ${price_ceiling_usd:.2f} で1次検索します"
    )

    broad_results_by_id = {}
    for query in BROAD_QUERIES:
        print(f"\n1次検索中: 「{query}」（価格が高い順、上限${price_ceiling_usd:.2f}）")
        offset = 0
        total = None
        while offset < BROAD_SEARCH_TOTAL_TARGET:
            try:
                results, total = ebay_search_listings(
                    token, call_budget, query, price_max_usd=price_ceiling_usd,
                    limit=BROAD_SEARCH_PAGE_SIZE, offset=offset,
                )
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    print(
                        "    → eBay側の呼び出し上限(429 Too Many Requests)に達しました。"
                        "今日はここで終了します。"
                    )
                    return
                print(f"    → 1次検索でエラー: {e}。この検索語はここで打ち切ります。")
                break
            if not results:
                break
            for item in results:
                item_id = item.get("itemId")
                if item_id and item_id not in broad_results_by_id:
                    broad_results_by_id[item_id] = item
            offset += BROAD_SEARCH_PAGE_SIZE
            if offset >= total:
                break
        print(f"  ヒット数: {total}件中、{len(broad_results_by_id)}件を取得")

    broad_results = sorted(
        broad_results_by_id.values(),
        key=lambda item: float(item.get("price", {}).get("value", 0)),
        reverse=True,
    )
    print(f"\n1次検索(全{len(BROAD_QUERIES)}パターン)の合計候補: {len(broad_results)}件(重複除去後)")

    processed = load_processed_models()
    print(f"  (本日調査済みの型番: {len(processed)}件はスキップします)")

    market_price_cache = load_market_price_cache()
    manual_overrides = load_manual_price_overrides()
    if manual_overrides:
        merged = merge_manual_overrides(market_price_cache, manual_overrides)
        save_market_price_cache(market_price_cache)
        print(f"  (手動入力の相場データ{merged}件をキャッシュに取り込みました)")
    print(f"  (eBay相場キャッシュ: {len(market_price_cache)}件蓄積済み。一致すればeBay再検索を省略します)")

    weak_models = load_weak_models()
    print(f"  (クールダウン中の型番: {sum(1 for k in weak_models if is_in_cooldown(weak_models, k))}件はスキップします)")

    print("メルカリ用ブラウザを起動中...")
    driver = create_driver()

    filename = f"kameland_result_v2_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    header_written = False
    researched_count = 0
    go_rows = []

    try:
        for item in broad_results:
            if TEST_MODE and researched_count >= TEST_MODE_MAX_LENSES:
                print(f"\n[テストモード] {TEST_MODE_MAX_LENSES}件処理したのでここで終了します。")
                break

            if not TEST_MODE and runtime_budget.expired():
                print(f"\n実行時間の上限({MAX_RUNTIME_HOURS}時間)に達したため終了します。")
                break

            title = item.get("title", "")
            if not title:
                continue

            model_key = guess_model_key(title)
            if model_key in processed:
                continue

            if is_in_cooldown(weak_models, model_key):
                continue

            if not call_budget.can_afford(50):
                print(f"\neBay API呼び出し予算が残り少ないため、ここで打ち切ります(残り{call_budget.remaining()}回)")
                break

            lens_start = time.time()
            researched_count += 1
            print(f"\n[{researched_count}] 深掘り中: {title[:70]}")
            print(f"    経過時間: {runtime_budget.elapsed_hours():.2f}時間")

            processed.add(model_key)
            append_processed_model(model_key)

            try:
                if model_key in market_price_cache:
                    research = market_price_cache[model_key]
                    print(f"    → eBay相場キャッシュ利用（前回算出済み、再検索を省略）")
                else:
                    token_holder.refresh_if_needed()
                    research = research_lens_model_on_ebay(token_holder.token, call_budget, title, verbose=TEST_MODE)
                    if research is None:
                        print("    → 検索クエリを作れず、スキップ")
                        continue
                    if research["market_price_usd"] is not None:
                        # 相場が確定できたものだけキャッシュに保存し、次回以降使い回す
                        record_market_price(market_price_cache, model_key, research)
                        save_market_price_cache(market_price_cache)
                        record_weak_model_result(weak_models, model_key, success=True)
                        save_weak_models(weak_models)
                    else:
                        record_weak_model_result(weak_models, model_key, success=False)
                        save_weak_models(weak_models)

                print(f"    再検索クエリ: {research['query']}")
                print(f"    総出品数: {research['total_listings']}件 / クリーンサンプル: {research['clean_sample_count']}件")

                if research["market_price_usd"] is None:
                    print("    → eBay相場が確定できないため見送り")
                    jitter_sleep()
                    continue

                market_price_usd = research["market_price_usd"]
                print(f"    eBay相場(中央値): ${market_price_usd:.2f}")

                domestic_query = research["query"]  # eBay再検索と同じ短いコア型番を使う
                print(f"    国内検索クエリ(メルカリ・ヤフオク用): {domestic_query}")
                candidates = find_purchase_candidates(driver, title, domestic_query, verbose=TEST_MODE)

                if not candidates:
                    print("    → メルカリ・ヤフオクに仕入れ候補が見つからず見送り")
                    record_weak_model_result(weak_models, model_key, success=False)
                    save_weak_models(weak_models)
                    row = {
                        "検索キーワード": title,
                        "eBay目標売値(USD)": round(market_price_usd, 2),
                        "eBay総出品数(需給参考)": research["total_listings"],
                        "eBayクリーン品サンプル数": research["clean_sample_count"],
                        "仕入れ候補価格(円)": "", "仕入れ先": "", "仕入れ候補URL": "",
                        "想定利益率": "", "判定": "見送り",
                    }
                    append_csv_row(filename, row, not header_written)
                    header_written = True
                else:
                    record_weak_model_result(weak_models, model_key, success=True)
                    save_weak_models(weak_models)
                    # 利益率15%以上をクリアした候補は、1種類のレンズから複数件でも
                    # 全てGOリストに入れる(1件に絞らない)
                    any_go = False
                    for candidate in candidates:
                        profit_rate = calc_profit_rate(market_price_usd, candidate["price"])
                        if TEST_MODE:
                            sale_price_jpy = market_price_usd * USD_JPY_RATE
                            ebay_fee = sale_price_jpy * EBAY_FEE_RATE
                            net_received = sale_price_jpy - ebay_fee - SHIPPING_COST_JPY
                            print(f"    --- 利益率計算({candidate['source']} ¥{candidate['price']}) ---")
                            print(f"    eBay相場: ${market_price_usd:.2f} × 為替{USD_JPY_RATE} = ¥{sale_price_jpy:,.0f}")
                            print(f"    eBay手数料15%: -¥{ebay_fee:,.0f} / 送料: -¥{SHIPPING_COST_JPY:,}")
                            print(f"    手取り: ¥{net_received:,.0f}")
                            print(f"    利益: ¥{net_received - candidate['price']:,.0f} → 利益率: {profit_rate*100:.1f}%")

                        row = {
                            "検索キーワード": title,
                            "eBay目標売値(USD)": round(market_price_usd, 2),
                            "eBay総出品数(需給参考)": research["total_listings"],
                            "eBayクリーン品サンプル数": research["clean_sample_count"],
                            "仕入れ候補価格(円)": candidate["price"],
                            "仕入れ先": candidate["source"],
                            "仕入れ候補URL": candidate["url"],
                            "想定利益率": f"{profit_rate * 100:.1f}%" if profit_rate is not None else "不明",
                            "判定": "見送り",
                        }

                        if profit_rate is not None and profit_rate >= MIN_PROFIT_RATE_GO:
                            row["判定"] = "GO"
                            go_rows.append(row)
                            any_go = True
                            print(f"    → GO！利益率{profit_rate * 100:.1f}%（{candidate['source']} ¥{candidate['price']}）")
                        else:
                            rate_display = f"{profit_rate * 100:.1f}%" if profit_rate is not None else "不明"
                            print(f"    → {candidate['source']} ¥{candidate['price']}: 利益率{rate_display}のため見送り")

                        append_csv_row(filename, row, not header_written)
                        header_written = True

            except Exception as e:
                # 通信エラー等、何が起きてもこのレンズだけ諦めて次に進む。
                # 10時間の作業全体が1件のエラーで止まってしまわないようにするため。
                print(f"    → エラーが発生しましたが処理を継続します: {type(e).__name__}: {e}")
                jitter_sleep()
                continue

            elapsed = time.time() - lens_start
            print(f"    (このレンズの処理時間: {elapsed:.0f}秒)")

            if STOP_AFTER_FIRST_GO and len(go_rows) >= 1:
                print(f"\nGOが1件見つかったので、精度確認のためここで終了します。")
                break

    finally:
        driver.quit()

    print("\n=== 完了 ===")
    print(f"深掘りしたレンズ: {researched_count}種類（うちGO: {len(go_rows)}種類）")
    print(f"使用したAPI呼び出し: 約{call_budget.count}回 / 予算{call_budget.limit}回")
    print(f"結果を {filename} に保存しました")

    print("\nLINEにGOリストを送信中...")
    send_go_list_to_line(go_rows)
    print("送信完了")


if __name__ == "__main__":
    main()
