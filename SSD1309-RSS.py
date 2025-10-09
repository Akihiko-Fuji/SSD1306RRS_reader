#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SSD1309/SSD1306 OLED RSSリーダー (I2C/SPI両対応)
ファイル名    : SSD1309_RSS.py
概要          : SSD1309/SSD1306 OLED用 複数RSS対応リーダー
作成者        : Akihiko Fuji
更新日        : 2025/10/9
バージョン    : 1.5
------------------------------------------------
Raspberry Pi + luma.oled環境で動作する日本語対応RSSビューワー。
複数RSSソースを巡回し、記事を自動スクロール表示します。

 - I2C/SPI 接続を USE_SPI 変数で切り替え可能
 - GPIOボタンによる記事送り、ダブルクリックでフィード切替
 - 日本語表示のためフォント（例：JF-Dot）を同一フォルダに設置してください

必要ライブラリ:
    pip3 install luma.oled feedparser pillow RPi.GPIO
"""

import os
import sys
import time
import threading
import signal
import logging
import logging.handlers
import socket
import re
import textwrap
from typing import Dict, List, Any, Optional

import feedparser
from PIL import Image, ImageDraw, ImageFont

# luma.oled
from luma.core.interface.serial import i2c
from luma.oled.device import ssd1309  # ssd1306 を利用する場合は変更

# 液晶解像度設定
WIDTH = 128
HEIGHT = 64

# 送りやフィードに利用するGPIOピン（BCM）
BUTTON_FEED = 18

# RSSフィード 必要に応じて手動で増減させてください
RSS_FEEDS = [
    {"title": "NHKニュース"     , "url": "https://news.web.nhk/n-data/conf/na/rss/cat0.xml",       "color": 1},
    {"title": "NHKニュース 科学", "url": "https://news.web.nhk/n-data/conf/na/rss/cat3.xml",       "color": 1},
    {"title": "NHKニュース 政治", "url": "https://news.web.nhk/n-data/conf/na/rss/cat4.xml",       "color": 1},
    {"title": "NHKニュース 経済", "url": "https://news.web.nhk/n-data/conf/na/rss/cat5.xml",       "color": 1},
    {"title": "NHKニュース 国際", "url": "https://news.web.nhk/n-data/conf/na/rss/cat6.xml",       "color": 1},
]

# 画面表示時間の設定 8:30 - 18:00 のみ利用するとしている
DISPLAY_TIME_START = (8, 30)
DISPLAY_TIME_END =  (18, 0)

# SPI接続時はTrue / I2C接続時はFalse
USE_SPI = False

# ログ設定
def setup_logging():
    logger = logging.getLogger("rss_oled")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("[%(levelname)s] %(asctime)s %(message)s")
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    return logger


class RSSReaderApp:
    # 主要変数と状態を初期化
    def __init__(self):
        # ロガー
        self.log = setup_logging()

        # 表示・描画
        self.display = None

        # フォント
        self.TITLE_FONT = None
        self.FONT = None

        # 設定（変更しやすい値をクラス属性に集約）
        self.RSS_UPDATE_INTERVAL = 1800           # 秒｜RSSを再取得する間隔（30分ごとに最新化）
        self.FEED_SWITCH_INTERVAL = 600           # 秒｜フィード自動切替の間隔（10分で次のフィードへ）
        self.SCROLL_SPEED = 4                     # pixcel/フレーム｜説明文の横スクロール速度（大きいほど速く流れる）
        self.ARTICLE_DISPLAY_TIME = 25.0          # 秒｜短文（スクロール不要）の記事を次へ送るまでの待機時間
        self.PAUSE_AT_START = 3.0                 # 秒｜記事表示直後のスクロール一時停止（読み始めの“間”を作る）
        self.TRANSITION_FRAMES = 15               # フレーム数｜フィード/記事切替アニメの尺（多いほどゆっくり）
        self.GPIO_POLL_INTERVAL = 0.02            # 秒｜GPIOポーリング周期（クリック検出のサンプリング間隔）
        self.MAIN_UPDATE_INTERVAL = 0.1           # 秒｜描画更新周期（CPU負荷と滑らかさのトレードオフ）
        self.DOUBLE_CLICK_INTERVAL = 0.6          # 秒｜ダブルクリック判定の間隔（この時間内の2押しでダブル扱い）

        # 状態（可変）
        self.news_items: Dict[int, List[Dict[str, Any]]] = {}  # 取得済みRSSをフィードindexごとに保持
        self.current_feed_index: int = 0          # 現在表示中のフィードindex
        self.current_item_index: int = 0          # 現在表示中の記事index（当該フィード内）
        self.scroll_position: int = 0             # 説明文のスクロール位置（px）
        self.last_rss_update: float = 0.0         # 最終RSS更新のエポック秒
        self.article_start_time: float = 0.0      # 現記事の表示開始エポック秒（PAUSE判定や経過時間計算に使用）
        self.auto_scroll_paused: bool = True      # Trueの間は説明文スクロールを停止（PAUSE_AT_STARTで解除）
        self.feed_switch_time: float = 0.0        # 直近のフィード切替時刻（中央通知の表示条件に利用）
        self.loading_effect: int = 0              # ローディング演出の残カウンタ（0で非表示）
        self.transition_effect: float = 0.0       # 切替アニメの残フレーム量（>0の間はスライド描画）
        self.transition_direction: int = 1        # 切替方向（+1:右へ／-1:左へ）アニメのオフセット符号に使用

        # スケジューラ／タイマー代替：メインループ内の時刻管理
        self._last_main_update: float = 0.0       # 直近の描画更新実行時刻（MAIN_UPDATE_INTERVAL判定用）
        self._last_feed_switch_check: float = 0.0 # 直近のフィード切替チェック時刻（FEED_SWITCH_INTERVAL判定用）

        # GPIO用
        self._gpio_available = False              # TrueならGPIO使用可能（環境により未接続/未導入の考慮）
        self._stop_event = threading.Event()      # 終了シグナル（スレッド/ループの安全停止に利用）

        # クリック検出（ポーリング方式に統一）
        self._prev_button_state = 1               # 直前のボタン状態（1:未押下, 0:押下）
        self._last_press_time = 0.0               # 直近の押下時刻（単/ダブルクリックの時間間隔判定に使用）
        self._click_count = 0                     # クリック回数カウント（1=シングル, 2=ダブル）

        # ロック（必要最小限）
        self._state_lock = threading.Lock()

# 1) 初期化処理
    # 初期化
    def initialize(self):
        self._init_fonts()
        self._init_gpio()
        self._init_display()
        self._install_signal_handlers()
        self.article_start_time = time.time()
        self.feed_switch_time = time.time() - 10  # 初回通知オフセット
        self._last_main_update = time.time()
        self._last_feed_switch_check = time.time()

    # 日本語フォントの呼び出し
    def _init_fonts(self):
        try:
            font_dir = os.path.dirname(os.path.abspath(__file__))
            title_font_file = os.path.join(font_dir, "JF-Dot-MPlusH10.ttf")
            main_font_file = os.path.join(font_dir, "JF-Dot-MPlusH12.ttf")
            self.TITLE_FONT = ImageFont.truetype(title_font_file, 10)
            self.FONT = ImageFont.truetype(main_font_file, 12)
            self.log.info("Fonts loaded")
        except Exception as e:
            self.log.warning(f"Font loading error: {e} -> using default fonts")
            self.TITLE_FONT = ImageFont.load_default()
            self.FONT = ImageFont.load_default()

    # GPIO初期化およびボタンスレッド起動
    def _init_gpio(self):
        try:
            import RPi.GPIO as GPIO
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(BUTTON_FEED, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self._gpio_available = True

            # ポーリングスレッド起動（統一）
            t = threading.Thread(target=self._gpio_polling_thread, daemon=True)
            t.start()
            self.log.info("GPIO initialized (polling)")
        except Exception as e:
            self._gpio_available = False
            self.log.info(f"GPIO not available, software-only mode: {e}")

    # OLED初期化 (I2C/SPI切替対応)
    def _init_display(self):
        try:
            # SPIモード
            if USE_SPI:
                from luma.core.interface.serial import spi
                serial = spi(device=0, port=0, gpio_DC=24, gpio_RST=25)
                self.display = ssd1309(serial_interface=serial, width=WIDTH, height=HEIGHT) # ssd1306 を利用する場合は変更
                self.log.info("OLED initialized (SPI mode)")

            # I2Cモード
            else:
                from luma.core.interface.serial import i2c
                serial = i2c(port=1, address=0x3C) # アドレスが異なる場合は変更
                self.display = ssd1309(serial_interface=serial, width=WIDTH, height=HEIGHT) # ssd1306 を利用する場合は変更
                self.log.info("OLED initialized (I2C mode)")

            self.display.contrast(0xFF)
            self.display.clear()
        except Exception as e:
            self.log.error(f"OLED initialization failed: {e}")
            raise

    # SIGINT/SIGTERMの終了ハンドラを登録
    def _install_signal_handlers(self):
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

# 2) RSS取得処理
    # RSS取得（リトライ強化）
    def fetch_rss_feed(self, max_retries: int = 3, base_delay: float = 2.0, timeout: float = 10.0) -> bool:
        self.log.info("Fetching RSS feeds...")
        self.loading_effect = 10
        attempt = 0

        while attempt <= max_retries:
            try:
                all_items: List[Dict[str, Any]] = []

                # feedparser は内部でHTTPを行う。timeoutはグローバルに設定できないため、socket のデフォルトタイムアウトを一時的に設定
                for idx, feed_info in enumerate(RSS_FEEDS):
                    self.log.info(f"Fetching: {feed_info['title']}")
                    prev_timeout = socket.getdefaulttimeout()
                    socket.setdefaulttimeout(timeout)
                    try:
                        feed = feedparser.parse(feed_info["url"])
                    finally:
                        socket.setdefaulttimeout(prev_timeout)

                    # HTTPステータス
                    if hasattr(feed, "status") and feed.status != 200:
                        raise ConnectionError(f"HTTP status {feed.status} for {feed_info['title']}")

                    entries = getattr(feed, "entries", [])[:10]
                    feed_items = []
                    for entry in entries:
                        title = getattr(entry, "title", "")
                        desc = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
                        desc = re.sub(r"<[^>]+>", "", desc)
                        published = getattr(entry, "published", "")
                        link = getattr(entry, "link", "")
                        feed_items.append({
                            "title": title,
                            "description": desc,
                            "published": published,
                            "link": link,
                            "feed_title": feed_info["title"],
                            "feed_color": feed_info["color"],
                            "feed_index": idx
                        })
                    self.log.info(f" -> {feed_info['title']}: {len(feed_items)} items")
                    all_items.extend(feed_items)

                if all_items:
                    grouped: Dict[int, List[Dict[str, Any]]] = {i: [] for i in range(len(RSS_FEEDS))}
                    for item in all_items:
                        grouped[item["feed_index"]].append(item)
                    with self._state_lock:
                        self.news_items = grouped
                        self.last_rss_update = time.time()
                    self.log.info(f"Total items: {len(all_items)}")
                    return True
                else:
                    self.log.warning("No items fetched")
                    return False

            # ネットワーク系（タイムアウト、HTTPステータス不良）
            except (socket.timeout, ConnectionError) as e:
                self.log.warning(f"Network error: {e} (attempt {attempt+1}/{max_retries})")

            # IOエラー（DNS解決障害、ソケット問題等も含む可能性）
            except (OSError, IOError) as e:
                self.log.warning(f"I/O error: {e} (attempt {attempt+1}/{max_retries})")
            except Exception as e:
                self.log.error(f"Unexpected error while fetching RSS: {e} (attempt {attempt+1}/{max_retries})")

            attempt += 1
            if attempt <= max_retries:
                delay = base_delay * (2 ** (attempt - 1))  # 指数バックオフ
                time.sleep(delay)

        return False

# 3) 描画・表示制御
    # 画面描画ユーティリティ
    def get_text_width(self, text: str, font: ImageFont.FreeTypeFont) -> int:
        try:
            return int(font.getlength(text))
        except AttributeError:
            try:
                return font.getsize(text)[0]
            except AttributeError:
                dummy_image = Image.new("1", (1, 1))
                dummy_draw = ImageDraw.Draw(dummy_image)
                bbox = dummy_draw.textbbox((0, 0), text, font=font)
                return bbox[2] - bbox[0]

    # 記事本文とタイトルを描画する
    def draw_article_content(self, draw: ImageDraw.ImageDraw, item: Dict[str, Any], base_x: int, base_y: int, highlight_title: bool = False) -> int:
        title = item["title"]
        title_wrapped = textwrap.wrap(title, width=20)
        y_pos = base_y
        for i, line in enumerate(title_wrapped[:2]):
            if highlight_title:
                title_width = self.get_text_width(line, self.FONT)
                draw.rectangle((base_x - 2, y_pos - 1, base_x + title_width + 2, y_pos + 11), fill=1)
                draw.text((base_x, y_pos), line, font=self.FONT, fill=0)
            else:
                draw.text((base_x, y_pos), line, font=self.FONT, fill=1)
            y_pos += 12
        y_pos = base_y + (24 if len(title_wrapped) >= 2 else 12)
        draw.line([(base_x, y_pos + 1), (base_x + WIDTH - 4, y_pos + 1)], fill=1)
        y_pos += 2
        desc_background_height = 14
        draw.rectangle((base_x, y_pos, base_x + WIDTH - 4, y_pos + desc_background_height), fill=0)
        desc = item["description"].replace("\n", " ").strip()
        desc_x = base_x + WIDTH - self.scroll_position
        if self.auto_scroll_paused:
            desc_x = base_x
        draw.text((desc_x, y_pos), desc, font=self.FONT, fill=1)
        return y_pos + desc_background_height

    # フィード切替時の通知（中央反転表示）
    def draw_feed_notification(self, draw: ImageDraw.ImageDraw, feed_name: str):
        draw.rectangle((10, HEIGHT // 2 - 12, WIDTH - 10, HEIGHT // 2 + 12), fill=1)
        text_width = self.get_text_width(feed_name, self.FONT)
        draw.text(((WIDTH - text_width) // 2, HEIGHT // 2 - 6), feed_name, font=self.FONT, fill=0)

    # 現在のRSS記事内容を描画してImageを返す
    def draw_rss_screen(self) -> Image.Image:
        image = Image.new("1", (WIDTH, HEIGHT))
        draw = ImageDraw.Draw(image)
        header_height = 14

        # ヘッダ部分の描画
        draw.rectangle((0, 0, WIDTH, header_height), fill=1)
        current_feed = RSS_FEEDS[self.current_feed_index]["title"]
        draw.text((2, 1), current_feed, font=self.TITLE_FONT, fill=0)
        current_time = time.strftime("%H:%M")
        time_width = self.get_text_width(current_time, self.TITLE_FONT)
        draw.text((WIDTH - time_width - 3, 1), current_time, font=self.TITLE_FONT, fill=0)
        draw.line([(0, header_height), (WIDTH, header_height)], fill=1)
        content_y = header_height + 2

        # 以下、ローディング／トランジション／通常描画
        if self.loading_effect > 0:
            # ローディングバー・点滅テキスト
            self.loading_effect -= 1
            message = "ニュースを読み込み中..."
            if self.loading_effect % 2 == 0:
                msg_width = self.get_text_width(message, self.FONT)
                draw.text(((WIDTH - msg_width) // 2, HEIGHT // 2 - 6), message, font=self.FONT, fill=1)

            bar_count = 20
            segment_width = 6
            for i in range(bar_count):
                segment_x = ((self.loading_effect + i) % (WIDTH // segment_width)) * segment_width
                draw.rectangle((segment_x, HEIGHT - 8, segment_x + segment_width - 2, HEIGHT - 2), fill=1)

        # トランジション中：記事を横スクロールで切替
        elif self.transition_effect > 0:
            self.transition_effect -= 1.5
            progress = self.transition_effect / self.TRANSITION_FRAMES
            offset = int(WIDTH * progress * self.transition_direction)
            if self.news_items and self.current_feed_index in self.news_items and self.news_items[self.current_feed_index]:
                item = self.news_items[self.current_feed_index][self.current_item_index]
                self.draw_article_content(draw, item, 2 + offset, content_y)
                prev_feed_idx = ((self.current_feed_index - 1) % len(RSS_FEEDS)
                                 if self.transition_direction > 0
                                 else (self.current_feed_index + 1) % len(RSS_FEEDS))
                if prev_feed_idx in self.news_items and self.news_items[prev_feed_idx]:
                    prev_item = self.news_items[prev_feed_idx][0]
                    next_x = 2 + WIDTH * (-self.transition_direction) + offset
                    self.draw_article_content(draw, prev_item, next_x, content_y)

        # 通常記事表示
        elif self.news_items and self.current_feed_index in self.news_items and 0 <= self.current_item_index < len(self.news_items[self.current_feed_index]):
            item = self.news_items[self.current_feed_index][self.current_item_index]
            self.draw_article_content(draw, item, 2, content_y)

        # ニュースが存在しない場合のメッセージ
        else:
            message = "ニュースがありません"
            msg_width = self.get_text_width(message, self.FONT)
            draw.text(((WIDTH - msg_width) // 2, HEIGHT // 2 - 6), message, font=self.FONT, fill=1)

        # フィード切替通知
        if time.time() - self.feed_switch_time < 2.0:
            self.draw_feed_notification(draw, RSS_FEEDS[self.current_feed_index]["title"])

        return image

# 4) GPIO処理・制御ロジック
    # 次のRSSフィードへ切替
    def switch_feed(self):
        with self._state_lock:
            self.current_feed_index = (self.current_feed_index + 1) % len(RSS_FEEDS)
            self.current_item_index = 0
            self.scroll_position = 0
            self.article_start_time = time.time()
            self.feed_switch_time = time.time()
            self.auto_scroll_paused = True
            self.transition_effect = self.TRANSITION_FRAMES
            self.transition_direction = -1
        self.log.info(f"Feed switched -> {RSS_FEEDS[self.current_feed_index]['title']}")

    # 次の記事へ
    def move_to_next_article(self):
        with self._state_lock:
            if not self.news_items or self.current_feed_index not in self.news_items or not self.news_items[self.current_feed_index]:
                return
            if self.current_item_index < len(self.news_items[self.current_feed_index]) - 1:
                self.current_item_index += 1
            else:
                self.current_item_index = 0
            self.scroll_position = 0
            self.transition_effect = self.TRANSITION_FRAMES
            self.transition_direction = -1
            self.article_start_time = time.time()
            self.auto_scroll_paused = True

    # 前の記事へ（呼び出し無し）
    def move_to_prev_article(self):
        with self._state_lock:
            if not self.news_items or self.current_feed_index not in self.news_items or not self.news_items[self.current_feed_index]:
                return
            if self.current_item_index > 0:
                self.current_item_index -= 1
            else:
                self.current_item_index = len(self.news_items[self.current_feed_index]) - 1
            self.scroll_position = 0
            self.transition_effect = self.TRANSITION_FRAMES
            self.transition_direction = 1
            self.article_start_time = time.time()
            self.auto_scroll_paused = True

    # 説明文スクロール位置を更新する。
    def update_scroll_position(self):
        """
        自動スクロールが有効な場合のみ self.scroll_position を増加させる。
        """
        # ガード
        if (not self.news_items) or (self.transition_effect > 0) or (self.current_feed_index not in self.news_items):
            return
        if not self.news_items[self.current_feed_index]:
            return

        # 記事と経過時間の取得（ロック下）
        with self._state_lock:
            item = self.news_items[self.current_feed_index][self.current_item_index]
            current_time = time.time()
            elapsed_time = current_time - self.article_start_time

            # 表示開始直後は一時停止（PAUSE_AT_START 秒）
            if self.auto_scroll_paused:
                if elapsed_time >= self.PAUSE_AT_START:
                    self.auto_scroll_paused = False
                return  # 停止中はここで抜ける

            # 説明文の幅を計測
            desc = item["description"].replace("\n", " ").strip()
            desc_width = self.get_text_width(desc, self.FONT) if desc else 0

            # 短文（スクロール不要）は ARTICLE_DISPLAY_TIME 経過で次記事へ
            if desc_width <= (WIDTH - 4):
                if elapsed_time >= self.ARTICLE_DISPLAY_TIME:
                    # ロック外で次記事遷移
                    pass
                else:
                    return

        # ロック外で状態遷移（短文の場合のみ）
        if desc_width <= (WIDTH - 4):
            self.move_to_next_article()
            return

        # 長文スクロールの更新（ロック下で位置のみ進める）
        with self._state_lock:
            self.scroll_position += self.SCROLL_SPEED
            tail_margin_px = 24
            reached_tail = (self.scroll_position > (desc_width + WIDTH + tail_margin_px))

        # 末尾に達したらロック外で次記事へ（※ここが重要：条件成立時のみ呼ぶ）
        if reached_tail:
            self.move_to_next_article()

    # GPIOポーリング（クリック/ダブルクリック処理）
    def _gpio_polling_thread(self):
        try:
            import RPi.GPIO as GPIO
        except Exception:
            return

        self.log.info("GPIO polling thread started")
        while not self._stop_event.is_set():
            try:
                state = GPIO.input(BUTTON_FEED)
                now = time.time()
                if self._prev_button_state == 1 and state == 0:
                    if now - self._last_press_time <= self.DOUBLE_CLICK_INTERVAL:
                        self._click_count += 1
                    else:
                        self._click_count = 1
                    self._last_press_time = now

                if self._click_count > 0 and (now - self._last_press_time) > self.DOUBLE_CLICK_INTERVAL:
                    if self._click_count == 1:
                        self.move_to_next_article()
                        self.log.info("[GPIO] Single click -> next article")
                    else:
                        self.switch_feed()
                        self.log.info("[GPIO] Double click -> switch feed")
                    self._click_count = 0

                self._prev_button_state = state
                time.sleep(self.GPIO_POLL_INTERVAL)
            except Exception as e:
                self.log.warning(f"GPIO polling error: {e}")
                time.sleep(0.1)

    # 時間帯表示制御
    def is_display_time(self) -> bool:
        now = time.localtime()
        now_hm = now.tm_hour * 60 + now.tm_min
        start_hm = DISPLAY_TIME_START[0] * 60 + DISPLAY_TIME_START[1]
        end_hm = DISPLAY_TIME_END[0] * 60 + DISPLAY_TIME_END[1]
        return start_hm <= now_hm < end_hm

    # シグナル／終了処理
    def _signal_handler(self, sig, frame):
        self.log.info("Exiting...")
        self._stop_event.set()
        try:
            import RPi.GPIO as GPIO
            GPIO.cleanup()
        except Exception:
            pass
        if self.display:
            blank = Image.new("1", (WIDTH, HEIGHT), 0)
            try:
                self.display.display(blank)
            except Exception:
                pass
        sys.exit(0)

# 5) メインループ
    # メインループ（タイマー一元管理）
    def run(self):
        # 初回RSSフェッチ（リトライ内蔵）
        if not self.fetch_rss_feed():
            self.log.warning("First RSS fetch failed after retries")

        last_feed_switch_time = time.time()
        last_update_time = time.time()

        while True:
            now = time.time()

            # 描画とスクロール更新
            if now - last_update_time >= self.MAIN_UPDATE_INTERVAL:
                self.update_scroll_position()
                image = self.draw_rss_screen()
                try:
                    self.display.display(image)
                except Exception as e:
                    self.log.warning(f"OLED display error: {e}")
                last_update_time = now

            # フィード自動切替
            if now - last_feed_switch_time >= self.FEED_SWITCH_INTERVAL:
                try:
                    self.switch_feed()
                except Exception as e:
                    self.log.warning(f"Auto feed switch error: {e}")
                finally:
                    last_feed_switch_time = time.time()

            # 表示時間外はスリープ表示
            if not self.is_display_time():
                blank = Image.new("1", (WIDTH, HEIGHT))
                draw = ImageDraw.Draw(blank)
                # 非表示運用に合わせて空文字
                msg = " "
                msg_width = self.get_text_width(msg, self.FONT)
                draw.text(((WIDTH - msg_width) // 2, HEIGHT // 2 - 8), msg, font=self.FONT, fill=1)
                try:
                    self.display.display(blank)
                except Exception:
                    pass
                time.sleep(30)
                # 復帰直後に即更新できるよう基準時刻調整
                last_update_time = time.time()
                continue

            time.sleep(0.01)

# 6) エントリーポイント
def main():
    app = RSSReaderApp()
    try:
        app.initialize()
        app.run()
    except KeyboardInterrupt:
        app._signal_handler(None, None)
    except Exception as e:
        # ここは最上位キャッチ。ログして安全終了
        logging.getLogger("rss_oled").error(f"Fatal error: {e}")
        app._signal_handler(None, None)


if __name__ == "__main__":
    main()




