#!/usr/bin/env python3
# -*- coding: utf-8 -*-

###########################################################################
# ファイル名    : SSD1309_RSS.py
# 概要          : SSD1309/SSD1306 OLED用 複数RSS対応リーダー
# 作成者        : Akihiko Fujita
# 更新日        : 2025/7/3
# バージョン    : 1.1
#
# 【コメント】
# 本プログラムはRaspberry PiにI2C接続したSSD1309/SSD1306 OLED上で
# おもに日本語RSSニュースを自動スクロール＆複数ソース切り替えで表示します
#
# 必要ライブラリ導入例:
#   pip3 install luma.oled feedparser pillow RPi.GPIO
#
# 日本語表示のためフォント（例：JF-Dot）を同一フォルダに設置してください
# また、幅・高さ（WIDTH, HEIGHT）はご自分のOLEDサイズに合わせてください
###########################################################################

import time
import os
import sys
import threading
import re
import textwrap
import feedparser
import signal
from PIL import Image, ImageDraw, ImageFont

# luma.oledライブラリ
from luma.core.interface.serial import i2c
from luma.oled.device import ssd1309    # SSD1306を使う場合はここを ssd1306 に変更

# ====【ディスプレイ/ピン・各種設定】===================================
WIDTH = 128                  # OLEDディスプレイ 幅
HEIGHT = 64                  # OLEDディスプレイ 高さ

# GPIOピン番号 (BCM)
BUTTON_NEXT = 17             # 次の記事へ進む
BUTTON_PREV = 27             # 前の記事へ戻る
BUTTON_FEED = 18             # フィードを切り替え

# RSSフィードリスト。お好みで増減可能
RSS_FEEDS = [
    {
        "title": "NHKニュース",
        "url": "https://www.nhk.or.jp/rss/news/cat0.xml",
        "color": 1         # 予備（カラー対応なら使うが機能していない）
    },
    {
        "title": "日経テクノロジー",
        "url": "https://assets.wor.jp/rss/rdf/nikkei/technology.rdf",
        "color": 1
    }
]
RSS_UPDATE_INTERVAL = 300    # RSS再更新間隔[秒]
CURRENT_FEED_INDEX = 0       # 表示対象フィードindex 切替ごとに加算

SCROLL_SPEED = 2             # 説明文スクロール速度
ARTICLE_DISPLAY_TIME = 25    # 記事毎自動進行間隔[秒]
PAUSE_AT_START = 3.0         # 各記事表示開始でスクロール一時停止[秒]
TRANSITION_FRAMES = 15       # フィード・記事切替アニメーションのフレーム数

# ====【グローバル変数（状態管理）】====================================
news_items = {}              # フィードごとに記事リストを保持
current_item_index = 0       # 現在の表示記事インデックス
scroll_position = 0          # スクロール位置
last_rss_update = 0          # 最終RSS更新時刻
article_start_time = 0       # 現在記事の表示開始時刻
auto_scroll_paused = True    # 一時停止フラグ
feed_view_active = False     # フィード切替表示中フラグ
feed_switch_time = 0         # フィード切替発動時刻

# フォント
FONT = None
TITLE_FONT = None
SMALL_FONT = None

# 描画・アニメ用
loading_effect = 0           # ローディング進捗演出
transition_effect = 0        # 記事・フィード切替のアニメframes残数
transition_direction = 1     # アニメスライド方向（+1:右/-1:左）

display = None               # OLED displayインスタンス（luma.oled）

# 画面表示タイマー(例: 8:15から 17:45まで表示) 必要に応じ変更
# 05分など、値の頭に0を入れて時間を展開するとエラーになります注意
DISPLAY_TIME_START = ( 8, 15)  # (hour, minute)
DISPLAY_TIME_END   = (17, 45)  # (hour, minute)

# ================= 初期化 ================================
def initialize():
    """
    日本語フォントやGPIOを初期化する関数。物理ボタンが無い場合も問題なく動作
    """
    global FONT, TITLE_FONT, SMALL_FONT
    try:
        # フォントファイルの絶対パス参照
        font_dir = os.path.dirname(os.path.abspath(__file__))
        title_font_file = os.path.join(font_dir, "JF-Dot-MPlusH10.ttf")
        main_font_file  = os.path.join(font_dir, "JF-Dot-MPlusH12.ttf")
        small_font_file = os.path.join(font_dir, "JF-Dot-k6x8.ttf")

        TITLE_FONT = ImageFont.truetype(title_font_file,10)     # ヘッダー等
        FONT       = ImageFont.truetype(main_font_file, 12)     # 本文等
        SMALL_FONT = ImageFont.truetype(small_font_file, 8)
    except Exception as e:
        print(f"[Font loading error] {e} （Switch to default font use）")
        TITLE_FONT = ImageFont.load_default()
        FONT       = ImageFont.load_default()
        SMALL_FONT = ImageFont.load_default()

    # GPIOの初期化
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(BUTTON_NEXT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(BUTTON_PREV, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(BUTTON_FEED, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        GPIO.add_event_detect(BUTTON_NEXT, GPIO.FALLING,
                              callback=lambda x: handle_button_press("NEXT"), bouncetime=300)
        GPIO.add_event_detect(BUTTON_PREV, GPIO.FALLING,
                              callback=lambda x: handle_button_press("PREV"), bouncetime=300)
        GPIO.add_event_detect(BUTTON_FEED, GPIO.FALLING,
                              callback=lambda x: handle_button_press("FEED"), bouncetime=300)
        print("[GPIO] Button initialization complete")
    except ImportError:
        print("[GPIO] Without RPi.GPIO module → Starts in mode without physical button support")
    except Exception as e:
        print(f"[GPIO] initialization error: {e}")

# ================= RSS取得・パース =======================
def fetch_rss_feed():
    """
    各RSSフィードを取得し、記事リストをnews_itemsに格納する
    """
    global news_items, last_rss_update, loading_effect
    print("RSS feed being acquired...")
    loading_effect = 10   # ローディング演出回数

    try:
        all_items = []
        for feed_info in RSS_FEEDS:
            print(f"「{feed_info['title']}」acquire feeds...")
            feed = feedparser.parse(feed_info["url"])
            # 取得失敗時はスキップ
            if hasattr(feed, 'status') and feed.status != 200:
                print(f"[RSS error] {feed_info['title']}: {feed.status}")
                continue
            feed_items = []
            for entry in feed.entries[:10]:
                title = entry.title
                desc = entry.summary if hasattr(entry, 'summary') else \
                       (entry.description if hasattr(entry, 'description') else "")
                desc = re.sub(r'<[^>]+>', '', desc)
                feed_items.append({
                    'title': title,
                    'description': desc,
                    'published': entry.published if hasattr(entry, 'published') else '',
                    'link': entry.link,
                    'feed_title': feed_info['title'],
                    'feed_color': feed_info['color'],
                    'feed_index': RSS_FEEDS.index(feed_info)
                })
            all_items.extend(feed_items)
            print(f"  → {feed_info['title']} Number of articles: {len(feed_items)}")
        # 全てnews_itemsにまとめる
        if all_items:
            news_items = {}
            for feed_idx in range(len(RSS_FEEDS)):
                news_items[feed_idx] = [item for item in all_items if item['feed_index'] == feed_idx]
            last_rss_update = time.time()
            print(f"--> Total {len(all_items)} acquisition")
            return True
        else:
            print("[RSS] No article")
            return False
    except Exception as e:
        print(f"[RSS] Acquisition error: {e}")
        return False

def update_rss_feed_thread():
    """
    定期的にRSSを自動更新するサブスレッド
    """
    while True:
        fetch_rss_feed()
        time.sleep(RSS_UPDATE_INTERVAL)

# ================== 記事・フィード切替処理 ==================
def switch_feed():
    """
    フィード（RSSソース）を次に切り替える。記事も先頭に
    """
    global CURRENT_FEED_INDEX, current_item_index, scroll_position
    global article_start_time, auto_scroll_paused, feed_switch_time
    global transition_effect, transition_direction

    CURRENT_FEED_INDEX = (CURRENT_FEED_INDEX + 1) % len(RSS_FEEDS)
    current_item_index = 0
    scroll_position = 0
    article_start_time = time.time()
    feed_switch_time = time.time()
    auto_scroll_paused = True
    transition_effect = TRANSITION_FRAMES
    transition_direction = -1
    print(f"[feed changed] now: {RSS_FEEDS[CURRENT_FEED_INDEX]['title']}")

def move_to_next_article():
    """
    次の記事へ移動。記事末尾なら先頭へ戻る
    """
    global current_item_index, scroll_position, transition_effect, transition_direction
    global article_start_time, auto_scroll_paused
    if not news_items or CURRENT_FEED_INDEX not in news_items or not news_items[CURRENT_FEED_INDEX]:
        return
    if current_item_index < len(news_items[CURRENT_FEED_INDEX]) - 1:
        current_item_index += 1
    else:
        current_item_index = 0
    scroll_position = 0
    transition_effect = TRANSITION_FRAMES
    transition_direction = -1
    article_start_time = time.time()
    auto_scroll_paused = True

def move_to_prev_article():
    """
    前の記事へ移動。先頭なら末尾へループ
    """
    global current_item_index, scroll_position, transition_effect, transition_direction
    global article_start_time, auto_scroll_paused
    if not news_items or CURRENT_FEED_INDEX not in news_items or not news_items[CURRENT_FEED_INDEX]:
        return
    if current_item_index > 0:
        current_item_index -= 1
    else:
        current_item_index = len(news_items[CURRENT_FEED_INDEX]) - 1
    scroll_position = 0
    transition_effect = TRANSITION_FRAMES
    transition_direction = 1
    article_start_time = time.time()
    auto_scroll_paused = True

# ================== GPIOボタン操作 =======================
def handle_button_press(button):
    """
    ボタン押下種別に応じて、記事・フィード切替処理を呼び出す
    """
    if button == "NEXT":
        move_to_next_article()
    elif button == "PREV":
        move_to_prev_article()
    elif button == "FEED":
        switch_feed()

# ================== 描画/表示用補助 ======================
def get_text_width(text, font):
    """
    指定フォントでのテキスト幅（ピクセル数）を返す。互換性処理あり
    """
    try:
        return font.getlength(text)
    except AttributeError:
        try:
            return font.getsize(text)[0]
        except AttributeError:
            # Pillow旧バージョン互換
            dummy_image = Image.new("1", (1, 1))
            dummy_draw = ImageDraw.Draw(dummy_image)
            bbox = dummy_draw.textbbox((0, 0), text, font=font)
            return bbox[2] - bbox[0]

def update_scroll_position():
    """
    記事テキストの自動スクロール進行処理。途中PUSHで進行/停止可
    """
    global scroll_position, article_start_time, auto_scroll_paused
    if not news_items or transition_effect > 0 or CURRENT_FEED_INDEX not in news_items:
        return
    if not news_items[CURRENT_FEED_INDEX]:
        return
    item = news_items[CURRENT_FEED_INDEX][current_item_index]
    current_time = time.time()
    elapsed_time = current_time - article_start_time
    if auto_scroll_paused:
        if elapsed_time >= PAUSE_AT_START:
            auto_scroll_paused = False
    else:
        scroll_position += SCROLL_SPEED
        desc = item['description']
        desc_width = get_text_width(desc, FONT)
        # 説明文の全幅＋一画面スクロール or 一定秒で次記事
        if (scroll_position > desc_width + WIDTH) or (elapsed_time >= ARTICLE_DISPLAY_TIME):
            move_to_next_article()

# ================== 描画本体 ========================
def draw_article_content(draw, item, base_x, base_y, highlight_title=False):
    """
    記事のタイトル+説明文ブロックを表示
    """
    title = item['title']
    title_wrapped = textwrap.wrap(title, width=20)
    y_pos = base_y

    # タイトル 2行まで自動折返
    for i, line in enumerate(title_wrapped[:2]):
        if highlight_title:
            title_width = get_text_width(line, FONT)
            draw.rectangle(
                (base_x - 2, y_pos - 1, base_x + title_width + 2, y_pos + 11),
                fill=1
            )
            draw.text((base_x, y_pos), line, font=FONT, fill=0)
        else:
            draw.text((base_x, y_pos), line, font=FONT, fill=1)
        y_pos += 12
    y_pos = base_y + (24 if len(title_wrapped) >= 2 else 12)
    draw.line([(base_x, y_pos +1), (base_x + WIDTH - 4, y_pos +1)], fill=1)
    y_pos += 2 #本文と説明文の間のバーの位置

    # 説明文（横スクロールエリア）
    desc_background_height = 14
    draw.rectangle((base_x, y_pos, base_x + WIDTH - 4, y_pos + desc_background_height), fill=0)
    desc = item['description'].replace('\n', ' ').strip()
    desc_x = base_x + WIDTH - scroll_position
    if auto_scroll_paused:
        desc_x = base_x
    draw.text((desc_x, y_pos), desc, font=FONT, fill=1)
    return y_pos + desc_background_height

def draw_feed_notification(draw, feed_name):
    """
    フィード切替時に中央に大きくフィード名を一時表示
    """
    draw.rectangle((10, HEIGHT//2 - 12, WIDTH - 10, HEIGHT//2 + 12), fill=1)
    text_width = get_text_width(feed_name, FONT)
    draw.text(((WIDTH - text_width) // 2, HEIGHT//2 - 6), feed_name, font=FONT, fill=0)

def draw_rss_screen():
    """
    画面全体を生成して返す描画メイン部
    """
    global loading_effect, transition_effect, feed_switch_time
    image = Image.new("1", (WIDTH, HEIGHT))          # 1bitモノクロ画像
    draw = ImageDraw.Draw(image)
    header_height = 14

    # ヘッダ部
    draw.rectangle((0, 0, WIDTH, header_height), fill=1)
    current_feed = RSS_FEEDS[CURRENT_FEED_INDEX]['title']
    draw.text((2, 1), current_feed, font=TITLE_FONT, fill=0)       # フィード名
    current_time = time.strftime("%H:%M")
    time_width = get_text_width(current_time, TITLE_FONT)
    draw.text((WIDTH - time_width - 3, 1), current_time, font=TITLE_FONT, fill=0)
    draw.line([(0, header_height), (WIDTH, header_height)], fill=1)
    content_y = header_height + 2

    # 各種アニメ演出・記事本体
    if loading_effect > 0:
        loading_effect -= 1
        message = "ニュースを読み込み中..."
        if loading_effect % 2 == 0:
            msg_width = get_text_width(message, FONT)
            draw.text(((WIDTH - msg_width) // 2, HEIGHT // 2 - 6), message, font=FONT, fill=1)

        # 読み出しアニメーション演出（例: 波が流れるようなバー）
        bar_count = 20
        segment_width = 6
        for i in range(bar_count):
            segment_x = ((loading_effect + i) % (WIDTH // segment_width)) * segment_width
            fill_val = 1 if i == 0 else 0  # 先頭だけ塗る場合
            # でも「全部白」でもOK、iごとに遅延して流れる波に見せる
            draw.rectangle(
                (segment_x, HEIGHT - 8, segment_x + segment_width - 2, HEIGHT - 2), fill=1
            )

    # 記事・フィード切替演出（スライド、フェード等）
    elif transition_effect > 0:
        transition_effect -= 1.5 #切替速度、数字が大きいほど切替が早い
        progress = transition_effect / TRANSITION_FRAMES
        offset = int(WIDTH * progress * transition_direction)
        if news_items and CURRENT_FEED_INDEX in news_items and news_items[CURRENT_FEED_INDEX]:
            item = news_items[CURRENT_FEED_INDEX][current_item_index]
            draw_article_content(draw, item, 2 + offset, content_y)
            prev_feed_idx = (CURRENT_FEED_INDEX - 1) % len(RSS_FEEDS) if transition_direction > 0 else (CURRENT_FEED_INDEX + 1) % len(RSS_FEEDS)
            if prev_feed_idx in news_items and news_items[prev_feed_idx]:
                prev_item = news_items[prev_feed_idx][0]
                next_x = 2 + WIDTH * (-transition_direction) + offset
                draw_article_content(draw, prev_item, next_x, content_y)

    # 通常記事表示
    elif news_items and CURRENT_FEED_INDEX in news_items and 0 <= current_item_index < len(news_items[CURRENT_FEED_INDEX]):
        item = news_items[CURRENT_FEED_INDEX][current_item_index]
        draw_article_content(draw, item, 2, content_y)

    # 記事ゼロ時のエラー表示
    else:
        message = "ニュースがありません"
        msg_width = get_text_width(message, FONT)
        draw.text(((WIDTH - msg_width) // 2, HEIGHT // 2 - 6), message, font=FONT, fill=1)

    # フィード切替時の中央通知
    if time.time() - feed_switch_time < 2.0:
        draw_feed_notification(draw, RSS_FEEDS[CURRENT_FEED_INDEX]['title'])

    return image

# ================== 安全な終了処理 ======================
def luma_signal_handler(sig, frame):
    """
    終了時のGPIOクリーンアップとOLED消灯処理
    """
    print("\n[INFO] Exit Program.")
    try:
        import RPi.GPIO as GPIO
        GPIO.cleanup()
    except:
        pass
    if display:
        blank = Image.new("1", (WIDTH, HEIGHT), 0)
        try:
            display.display(blank)
        except:
            pass
    sys.exit(0)

# ================== 表示時間タイマ ======================
def is_display_time():
    """設定した(時,分)で表示タイムウィンドウを判定"""
    now = time.localtime()
    now_hm = now.tm_hour * 60 + now.tm_min
    start_hm = DISPLAY_TIME_START[0] * 60 + DISPLAY_TIME_START[1]
    end_hm   = DISPLAY_TIME_END[0] * 60 + DISPLAY_TIME_END[1]
    return start_hm <= now_hm < end_hm

# ================== メインエントリ ======================
def main():
    global display, article_start_time, feed_switch_time
    initialize()
    if not fetch_rss_feed():
        print("[RSS] First attempt to retrieve failed, retry...")
    article_start_time = time.time()
    feed_switch_time = time.time() - 10       # 初回はすぐfeed通知を消すようオフセット

    # RSS更新スレッド起動
    feed_thread = threading.Thread(target=update_rss_feed_thread, daemon=True)
    feed_thread.start()

    # OLED初期化
    try:
        serial = i2c(port=1, address=0x3C)
        # ssd1309を使う場合。ssd1306の場合は行を修正
        global display
        display = ssd1309(serial_interface=serial, width=WIDTH, height=HEIGHT, rotate=0)
        display.contrast(0xFF)      # 初期化時のみ明るさ最大に
        display.clear()
        print("[OLED] luma.oled/SSD1309 Initialization complete")

    except Exception as e:
        print(f"[OLED] luma.oled/SSD1309 Initialization error: {e}")
        sys.exit(1)

    # 終了時の安全処理(Ctrl+C, kill等)
    signal.signal(signal.SIGINT, luma_signal_handler)
    signal.signal(signal.SIGTERM, luma_signal_handler)

    # メインループ
    last_update_time = time.time()
    update_interval = 0.1    # update周期[秒]

    try:
        while True:
            current_time = time.time()
            if current_time - last_update_time >= update_interval:
                update_scroll_position()        # スクロール進行
                image = draw_rss_screen()       # 画面描画
                display.display(image)
                last_update_time = current_time
            time.sleep(0.01)                    # 高負荷防止

            if not is_display_time():           # タイマー表示機能
                # OLEDをクリア又は「時間外」と表示
                blank = Image.new("1", (WIDTH, HEIGHT))
                draw = ImageDraw.Draw(blank)
                msg = " "                       #表示休止中などの文字をいれると、状態がわかりやすいが非表示
                msg_width = get_text_width(msg, FONT)
                draw.text(((WIDTH - msg_width)//2, HEIGHT//2-8), msg, font=FONT, fill=1)
                display.display(blank)
                time.sleep(30)
                last_update_time = time.time()  # 時計が戻った時即復帰できるよう
                continue

    except KeyboardInterrupt:
        luma_signal_handler(None, None)

    except Exception as e:
        print(f"[fatal error] {e}")
        luma_signal_handler(None, None)

if __name__ == "__main__":
    main()
