#!/usr/bin/python3
# -*- coding: utf-8 -*-
# oled_manager.py
###########################################################################
# Filename      :oled_manager.py
# Description   :Prod Track OLED manager
# Author        :Akihiko Fujita
# Update        :2025/10/10
Version = "1.4.5"
############################################################################

import threading
import time
import logging
import os
from typing import Optional, Dict, Any
import json

try:
    from PIL import Image, ImageFont, ImageDraw
except Exception:
    Image = None
    ImageFont = None
    ImageDraw = None

# =============== デフォルト値設定 =======================================
# 表示セクション定義
DISPLAY_SECTION = "display"

# luma.oled に合わせたデフォルト
DEFAULT_DISPLAY_CONFIG = {
    "contrast": 255,  # 0-255: ssd1309 で有効
    "font_size": 12,  # truetype使用時のサイズ
    "font_path": None,  # Noneなら Pillow デフォルトフォント
    "screensaver_enabled": True,
    "screensaver_timeout_sec": 120,  # 必要なら消灯時コントラスト値
    "screensaver_contrast": 0,  # 0で実質ブラックアウト（任意）
    "default_font_rel": None,  # 内蔵フォントパスの相対位置をデフォルト化したい場合 例: "assets/fonts/MyFont.ttf"（未指定なら None）
}

# このディレクトリ上に存在するアニメーションは、ディレクトリ指定がある場合は参照されない画像
DEFAULT_PAIR_ANIM_DIR = "/home/pi/Prodtrac/png_def"

# 参考情報用（display_animation は endswith('.png') 固定なのでこの定数は使わなくてもOK）
PAIR_ANIM_FRAME_GLOB = "frame_apngframe*.png"

# =============== OLED設定パラメータ =======================================
# デバッグモードフラグ
DEBUG_MODE = os.environ.get("OLED_DEBUG", "0").lower() in ("1", "true", "yes")
DEBUG_MODE = True  # ここを明示的にTrue/Falseにすれば環境変数に関係なく固定、コメントアウト時や空白時は環境変数を参照 (通常はFalse)


# 環境変数読み込み関数
def get_env_int(
    name: str,
    default: int,
    min_val: Optional[int] = None,
    max_val: Optional[int] = None,
) -> int:
    """
    環境変数から整数値を安全に取得し、指定された範囲内にあるか検証する
    範囲外の場合は制限値に修正し、数値に変換できない場合はデフォルト値を使用
    """
    try:
        value = int(os.environ.get(name, default))
        if min_val is not None and value < min_val:
            logging.warning(
                f"{name} value {value} below minimum {min_val}, using {min_val}"
            )
            return min_val
        if max_val is not None and value > max_val:
            logging.warning(
                f"{name} value {value} above maximum {max_val}, using {max_val}"
            )
            return max_val
        return value
    except ValueError:
        logging.warning(f"Invalid {name} value, using default {default}")
        return int(default)


# 接続方式: 'i2c' または 'spi' を指定
OLED_CONNECTION_TYPE = os.environ.get("OLED_CONNECTION_TYPE", "i2c")

# I2C設定パラメータ
I2C_PORT = get_env_int("I2C_PORT", 1, 0, 10)  # 通常はRaspberry Piではポート1
I2C_ADDRESS = int(
    os.environ.get("I2C_ADDRESS", "0x3C"), 0
)  # デフォルト0x3C、モデルにより0x3Dもあり sudo i2cdetect -y 1 で確認

# SPI設定パラメータ
SPI_PORT = get_env_int("SPI_PORT", 0, 0, 10)  # 通常は0
SPI_DEVICE = get_env_int("SPI_DEVICE", 0, 0, 10)  # CE0=0, CE1=1
SPI_GPIO_DC = get_env_int("SPI_GPIO_DC", 24, 0, 40)  # D/Cピン
SPI_GPIO_RST = get_env_int("SPI_GPIO_RST", 25, 0, 40)  # リセットピン
SPI_BUS_SPEED = get_env_int("SPI_BUS_SPEED", 8000000, 1000000, 32000000)  # 8MHz

# ディスプレイパラメータ
OLED_WIDTH = get_env_int("OLED_WIDTH", 128, 1, 256)  # ディスプレイ横ピクセル数
OLED_HEIGHT = get_env_int("OLED_HEIGHT", 64, 1, 128)  # ディスプレイ縦ピクセル数
OLED_FONT_PATH = os.environ.get(
    "OLED_FONT_PATH", "/home/pi/Prodtrac/JF-Dot-MPlusH12.ttf"
)  # フォントファイルパス
OLED_FONT_SIZE = get_env_int("OLED_FONT_SIZE", 12, 8, 24)  # フォントサイズ(pt)
# ==========================================================================

try:
    from luma.core.interface.serial import i2c, spi
    from luma.oled.device import ssd1309

    OLED_AVAILABLE = True
except ImportError:
    OLED_AVAILABLE = False


class OledDisplayManager:
    """
    I2CまたはSPI接続の有機ELディスプレイ制御を担う。
    接続方法は環境変数またはグローバル設定で切り替え可能。
    状態情報や任意メッセージをスレッド安全に表示、スレッド駆動。
    OLEDが無い/初期化失敗時はエラー/再初期化、自動復帰にも対応。
    """

    # OLEDマネージャの初期化とスレッド起動、設定反映を行う
    def __init__(self, connection_type: Optional[str] = None, config: Optional[dict] = None):

        self.lock = threading.Lock()              # 描画データ・状態フラグなどの排他制御用ロック
        self.display_data: Dict[str, Any] = {}    # 画面に表示する最新データ（状態・タイマー・作業者名など）を保持する共有ディクショナリ
        self._stop = (threading.Event())          # display_loop スレッドに停止を指示するためのイベントフラグ
        self.oled_ok = False                      # 現在 OLED デバイスが使用可能かどうか（初期化成功かつ利用中）を示すフラグ
        self.error_msg = (None)                   # 最新のエラーメッセージ（ログ/表示用）。None の場合はエラーなし
        self.need_reinit = False                  # 自動復帰方式のための再初期化要求フラグ（True のとき次サイクルで再初期化を試行）
        self.device = None                        # 実際のデバイスインスタンス（luma のデバイス or Dummy）。未初期化時は None
        self.font = None                          # 描画に使用するフォントオブジェクト（PIL の ImageFont）。未ロード時は None
        self._last_activity_time = (time.time())  # 最終ユーザー操作（または状態更新）時刻。スクリーンセーバー/減光の判定に使用
        self._screensaver = False                 # スクリーンセーバーが現在有効かどうかの状態フラグ
        self._pre_screensaver_blink = None        # スクリーンセーバー突入前のblink状態を退避するフィールド

        # 作業者名の自動スワップ表示に関する管理（スレッド/停止イベント/周期）
        self.worker_swap_thread = (None)          # スワップ処理用スレッドの実体（未起動時は None）
        self.worker_swap_stop_event = None        # スワップスレッド停止用のイベント
        self.worker_swap_interval = 0.8           # スワップの更新間隔（秒）

        self.config = config or {}
        disp_cfg = self.config.get(DISPLAY_SECTION, {}) or {}
        self.display_cfg = {**DEFAULT_DISPLAY_CONFIG, **disp_cfg}

        # 接続タイプ確定
        self.connection_type = connection_type or OLED_CONNECTION_TYPE
        if self.connection_type not in ("i2c", "spi"):
            self.connection_type = "i2c"
            logging.warning("Invalid connection type specified, defaulting to I2C")



        # デバイス初期化後にコントラストを適用（フォントは _init_oled 側で設定済み）
        if OLED_AVAILABLE:
            self._init_oled()
            try:
                if self.device and hasattr(self.device, "contrast"):
                    contrast = int(
                        self.display_cfg.get(
                            "contrast", DEFAULT_DISPLAY_CONFIG["contrast"]
                        )
                    )
                    self.device.contrast(max(0, min(255, contrast)))
            except Exception as e:
                logging.warning(f"Failed to apply contrast: {e}")
        else:
            self.error_msg = "OLED library not found"

        # スレッド起動は有効時のみ
        if self.oled_ok:
            self.thread = threading.Thread(target=self.display_loop, daemon=True)
            self.thread.start()

            if DEBUG_MODE:
                print(f"[DEBUG] OledDisplayManager initialized: {self.debug_info()}")
                logging.debug(f"OledDisplayManager initialized: {self.debug_info()}")

    def __enter__(self):
        """
        コンテキストマネージャーのエントリーポイント
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        コンテキストマネージャーの終了時に呼び出され、リソースをクリーンアップ
        """
        self.stop()

    def _init_oled(self):
        """
        OLEDハード・ライブラリの初期化。
        接続方式に応じて I2C または SPI 経由でデバイスを初期化。
        成功すれば oled_ok=True、失敗なら error_msg をセット。
        - luma.core 2.4.2 / luma.oled 3.14.0 に合わせて SPI の引数名を bus_speed_hz に修正。
        - PIL 不在時は Dummy デバイスに切り替え、後段描画はスキップ/ログのみで継続可能にする。
        """

        # 簡易ダミーデバイス（PIL 不在時やヘッドレス運用向け）
        class DummyOledDevice:
            def __init__(self, width=128, height=64):
                self.width = width
                self.height = height

            def contrast(self, value):
                logging.debug(f"[DummyOLED] contrast({value})")

            def display(self, image=None):
                logging.debug("[DummyOLED] display() called")

            def clear(self):
                logging.debug("[DummyOLED] clear() called")

        try:
            # PIL 不在ならダミーデバイスに切り替え
            if Image is None or ImageFont is None or ImageDraw is None:
                logging.warning(
                    "PIL not available: switching to Dummy OLED device (no image/text rendering)."
                )
                self.device = DummyOledDevice(width=OLED_WIDTH, height=OLED_HEIGHT)
                self.font = None
                self.oled_ok = True
                self.error_msg = None
                if DEBUG_MODE:
                    print(f"[DEBUG] OLED initialized in dummy mode: {self.debug_info() if hasattr(self, 'debug_info') else ''}")
                    logging.debug(
                        f"OLED initialized in dummy mode: {self.debug_info() if hasattr(self, 'debug_info') else ''}"
                    )
                return

            # 通信インターフェース初期化
            if self.connection_type == "i2c":
                logging.info(
                    f"Initializing OLED display via I2C (port={I2C_PORT}, address=0x{I2C_ADDRESS:X})"
                )
                serial_if = i2c(port=I2C_PORT, address=I2C_ADDRESS)
            else:  # 'spi'
                logging.info(
                    f"Initializing OLED display via SPI (port={SPI_PORT}, device={SPI_DEVICE}, speed={SPI_BUS_SPEED}Hz)"
                )
                # luma.core 2.4.2 では bus_speed_hz が正しい引数名
                serial_if = spi(
                    port=SPI_PORT,
                    device=SPI_DEVICE,
                    bus_speed_hz=SPI_BUS_SPEED,
                    # 必要に応じて gpio=... を追加（RST/DC 制御が必要な場合）
                )

            # デバイス生成
            self.device = ssd1309(serial_if, width=OLED_WIDTH, height=OLED_HEIGHT)

            # フォントのロード（PIL が使える前提で到達）
            if OLED_FONT_PATH and os.path.exists(OLED_FONT_PATH):
                try:
                    self.font = ImageFont.truetype(OLED_FONT_PATH, OLED_FONT_SIZE)
                except Exception as fe:
                    logging.warning(
                        f"Failed to load truetype font '{OLED_FONT_PATH}': {fe}. Falling back to default font."
                    )
                    try:
                        self.font = ImageFont.load_default()
                    except Exception as fe2:
                        logging.warning(
                            f"Failed to load default PIL font: {fe2}. Using no font; text rendering disabled."
                        )
                        self.font = None
            else:
                # パス未指定または存在しない場合はデフォルトフォント
                try:
                    self.font = ImageFont.load_default()
                except Exception as fe3:
                    logging.warning(
                        f"Failed to load default PIL font: {fe3}. Using no font; text rendering disabled."
                    )
                    self.font = None

            # 初期化成功
            self.oled_ok = True
            self.error_msg = None

            if DEBUG_MODE and hasattr(self, "debug_info"):
                print(f"[DEBUG] OLED initialized successfully: {self.debug_info()}")
                logging.debug(f"OLED initialized successfully: {self.debug_info()}")

        except TypeError as te:
            # 典型: 引数名の不一致など
            self.oled_ok = False
            self.error_msg = f"OLED init failed (TypeError): {te}"
            logging.error(self.error_msg)
        except Exception as e:
            self.oled_ok = False
            self.error_msg = f"OLED init failed: {e}"
            logging.error(self.error_msg)

    def update(self, **kwargs):
        """
        表示内容データを受け取り、逐次スレッド安全に更新する
        引数: kwargs 任意の状態パラメータ(process_lcd, worker_lcd, 等)
        戻り値: なし
        """
        with self.lock:
            self.display_data.update(kwargs)
            self._last_activity_time = time.time()
            # 入力が来たら直ちにスクリーンセーバー解除
            if self._screensaver:
                self._screensaver = False
                self._exit_screensaver()
                # ★ スクリーンセーバー解除後にblink状態を復元
                if self._pre_screensaver_blink:
                    if isinstance(self.display_data, dict):
                        self.display_data["show_blink"] = True
                self._pre_screensaver_blink = None


    def request_reinit(self, reason: str = ""):
        with self.lock:
            self.need_reinit = True
            if reason:
                self.error_msg = reason

    def show_error(self, lines, duration=None):
        """
        エラー内容をOLEDに表示する。
        :param lines: 1～2行の文字列リスト
        :param duration: 表示継続秒数 (Noneなら固定表示)
        """
        try:
            # PIL 不在時はコンソール出力にフォールバック
            if Image is None or ImageDraw is None:
                print("[OLED ERROR]", " / ".join(lines))
                return

            if self.device is None:
                print("[OLED ERROR]", " / ".join(lines))
                return

            width = getattr(self.device, "width", OLED_WIDTH)
            height = getattr(self.device, "height", OLED_HEIGHT)

            img = Image.new("1", (width, height), 1)  # 1=白
            draw = ImageDraw.Draw(img)

            # フォントフォールバック
            font = self.font
            if font is None:
                try:
                    font = ImageFont.load_default()
                except Exception:
                    font = None

            if font is None:
                logging.warning("No font available for show_error; falling back to console output.")
                print("[OLED ERROR]", " / ".join(lines))
                return

            # 黒文字で描画
            y = 0
            for line in lines:
                # Pillow 将来互換: textbbox で高さ算出（getsizeでも可）
                try:
                    bbox = draw.textbbox((0, 0), line, font=font)
                    line_h = bbox[3] - bbox[1]
                except Exception:
                    line_h = font.getsize(line)[1]
                draw.text((0, y), line, font=font, fill=0)  # 0=黒
                y += line_h

            self.device.display(img)

            if duration:
                time.sleep(duration)
                # 画面クリアは self.clear() ではなく device.clear() にフォールバック
                if hasattr(self.device, "clear"):
                    try:
                        self.device.clear()
                    except Exception as ce:
                        logging.debug(f"device.clear() failed after show_error: {ce}")

        except Exception as e:
            # OLEDが物理的に死んでいる場合など
            print(f"[OLED ERROR fallback] {lines} ({e})")

    def stop(self):
        """
        メインディスプレイスレッドの停止と解放を行い、デバイスをクリーンアップする
        戻り値: なし
        """
        self._stop.set()
        if hasattr(self, "thread") and self.thread.is_alive():
            self.thread.join(timeout=2.0)

        if self.device:
            try:
                if hasattr(self.device, "cleanup"):
                    self.device.cleanup()
                elif hasattr(self.device, "clear"):
                    # 任意: clear だけしておく（必要なら）
                    self.device.clear()
                if DEBUG_MODE:
                    logging.debug("Device cleanup completed successfully")
            except Exception as e:
                logging.warning(f"Error during device cleanup: {e}")

        # ★ デバイスを完全に無効化して display_loop から参照されないようにする
        self.device = None
        self.oled_ok = False

    # OLED全画面でメッセージを一時的に表示し、指定秒後に内容を復元する
    def display_message(self, message: str, duration: float = 0.5):
        """
        OLED全画面でメッセージを一時的に表示し、指定秒後に内容を復元する
        非同期で動作し、呼び出し元をブロックしない
        """
        with self.lock:
            backup = (
                self.display_data.copy() if isinstance(self.display_data, dict) else {}
            )
            self.display_data = {"message": message}
            self.special_display_until = time.time() + duration
            self._last_activity_time = time.time()

            # メッセージ表示はユーザー操作扱いでセーバー解除
            if self._screensaver:
                self._screensaver = False
                self._exit_screensaver()
                # ★ スクリーンセーバー解除後にblink状態を復元
                if self._pre_screensaver_blink:
                    if isinstance(self.display_data, dict):
                        self.display_data["show_blink"] = True
                self._pre_screensaver_blink = None

        def restore_after_timeout():
            time.sleep(duration)
            with self.lock:
                self.display_data = backup
                if hasattr(self, "special_display_until"):
                    del self.special_display_until

        # 非同期実行
        threading.Thread(target=restore_after_timeout, daemon=True).start()


    # バックグラウンドで永久ループし、現在の状態/内容に応じてOLED物理画面を更新し続ける
    def display_loop(self):
        """
        バックグラウンドで永久ループし、現在の状態/内容に応じてOLED物理画面を更新し続ける。

        - エラー時は自動で初期化再試行
        - 通常は画面設計に従い内容分岐描画
        - 'special_display_until' があれば優先的にメッセージを表示
        - show_blink指定時は点滅も表現
        - 無操作で減光、一定時間でスクリーンセーバーに移行。復帰時はコントラストを戻す
        - 周期補正を入れて「1秒ごとにtick」するよう調整
        - DEBUG_MODE 時に処理時間の100回平均をログ出力
        """

        # ===== 調整可能パラメータ =====
        SCREENSAVER_ENABLED = True
        SCREEN_SAVER_TIMEOUT = 120 * 60 # 120分無操作でスクリーンセーバー
        DIMMING_TIMEOUT = 5 * 60        # 5分無操作で減光
        DEFAULT_CONTRAST = 255          # 標準コントラスト
        DIMMING_CONTRAST = 0            # 減光時コントラスト（0で実質OFF）
        INTERVAL = 1.0                  # 更新周期 [秒]
        # =============================

        blink = True
        error_count = 0
        prev_display_data = None
        prev_error_msg = None
        prev_blink = blink

        dimmed = False
        current_contrast = DEFAULT_CONTRAST

        next_tick = time.time()

        while not self._stop.is_set():
            loop_start = time.time()
            try:
                # 自動復帰方式
                if not getattr(self, "need_reinit", False) and not self.oled_ok:
                    self.need_reinit = True

                if getattr(self, "need_reinit", False):
                    try:
                        self._init_oled()
                        if not self.oled_ok:
                            error_count += 1
                            continue
                        error_count = 0
                        self.need_reinit = False
                        # デバイス復帰時はコントラスト初期化
                        if self.device and hasattr(self.device, "contrast"):
                            self.device.contrast(DEFAULT_CONTRAST)
                            current_contrast = DEFAULT_CONTRAST
                    except Exception as e:
                        logging.error(f"OLED loop error: {e}")
                        self.oled_ok = False
                        self.need_reinit = True
                        error_count += 1

                # デバイスが解放されたらループ終了
                if not self.device:
                    break

                # デバイス異常なら描画スキップ
                if not self.oled_ok:
                    continue

                now = time.time()

                # 共有データのスナップショット取得
                with self.lock:
                    d = self.display_data.copy() if isinstance(self.display_data, dict) else {}
                    special_time = getattr(self, "special_display_until", 0)
                    special_active = special_time and now < special_time
                    error_msg = self.error_msg
                    last_activity = self._last_activity_time

                # ===== スクリーンセーバー・減光 =====
                if (
                    SCREENSAVER_ENABLED
                    and SCREEN_SAVER_TIMEOUT > 0
                    and not self._screensaver
                ):
                    if last_activity and (now - last_activity > SCREEN_SAVER_TIMEOUT):
                        # ★ 現在の blink 状態を退避
                        with self.lock:
                            if isinstance(self.display_data, dict):
                                self._pre_screensaver_blink = self.display_data.get("show_blink", False)
                        self._enter_screensaver()
                        self._screensaver = True
                        dimmed = False
                        if DEBUG_MODE:
                            print(f"[DEBUG] OLED screensaver ON")
                            logging.info("OLED screensaver ON")
                        continue

                if self._screensaver:
                    # スクリーンセーバー中は描画せず待機
                    continue

                if self.device and hasattr(self.device, "contrast") and DIMMING_TIMEOUT > 0:
                    if last_activity and (now - last_activity > DIMMING_TIMEOUT):
                        if not dimmed and current_contrast != DIMMING_CONTRAST:
                            self.device.contrast(DIMMING_CONTRAST)
                            current_contrast = DIMMING_CONTRAST
                            if DEBUG_MODE:
                                print(f"[DEBUG] OLED dim ON (contrast={DIMMING_CONTRAST})")
                                logging.info(f"OLED dim ON (contrast={DIMMING_CONTRAST})")
                        dimmed = True
                    else:
                        if dimmed and current_contrast != DEFAULT_CONTRAST:
                            self.device.contrast(DEFAULT_CONTRAST)
                            current_contrast = DEFAULT_CONTRAST
                            if DEBUG_MODE:
                                print(f"[DEBUG] OLED dim OFF (contrast restored)")
                                logging.info("OLED dim OFF (contrast restored)")
                        dimmed = False

                # ===== 内容変化判定 =====
                display_changed = (
                    d != prev_display_data
                    or error_msg != prev_error_msg
                    or special_active
                    or (d.get("show_blink", False) and blink != prev_blink)
                )
                if not display_changed:
                    blink = not blink
                    prev_blink = blink
                    # 内容変化なし → 描画スキップ
                else:
                    # ===== 描画 =====
                    if Image is None or ImageDraw is None:
                        logging.debug("PIL not available; skipping render cycle")
                    else:
                        img = Image.new("1", (OLED_WIDTH, OLED_HEIGHT), 0)
                        draw = ImageDraw.Draw(img)

                        if special_active and "message" in d:
                            draw.text((10, 20), d.get("message", ""), font=self.font, fill=1)
                        elif error_msg:
                            draw.rectangle((0, 0, OLED_WIDTH - 1, OLED_HEIGHT - 1), fill=1)
                            draw.text((3, 22), "ERROR:", font=self.font, fill=0)
                            draw.text((3, 32), str(error_msg)[:15], font=self.font, fill=0)
                            draw.text((3, 42), f"({self.connection_type})", font=self.font, fill=0)
                        else:
                            s = d.get("status", "作業時間")
                            timer = d.get("timer", "00:00")

                            if d.get("show_rework", False):
                                s = "* 手直し"
                                blink_char = "□" if d.get("show_blink", False) and blink else "　"
                            else:
                                blink_char = "■" if d.get("show_blink", False) and blink else "　"

                            draw.text((0, 0), d.get("process_lcd", ""), font=self.font, fill=1)
                            draw.text(
                                (0, 20),
                                f"{(d.get('check_no_lcd') or '      ')} |{d.get('worker_lcd', '')}",
                                font=self.font,
                                fill=1,
                            )
                            draw.text((0, 42), f"{s} {blink_char} {timer}", font=self.font, fill=1)

                        # 画像が生成された場合のみログ・表示を実行
                        if DEBUG_MODE and "img" in locals():
                            # print(f"[DEBUG] device={self.device}, img={type(img)}")
                            logging.debug(f"device={self.device}, img={type(img)}")

                        if self.device and "img" in locals():
                            self.device.display(img)

                # 内容に関わらず、ここで prev_* を更新（try の中でOK）
                prev_display_data = d.copy()
                prev_error_msg = error_msg
                prev_blink = blink
                blink = not blink

                if DEBUG_MODE and error_count % 5 == 0:
                   # print(f"[DEBUG] OLED status: {self.debug_info()}")
                    logging.debug(f"OLED status: {self.debug_info()}")

            except Exception as e:
                logging.exception(f"OLED loop error: {e}")
                self.oled_ok = False
                self.need_reinit = True
                error_count += 1

            # ===== 周期補正付きsleep =====（try-except の外側に配置）
            next_tick += INTERVAL
            sleep_time = next_tick - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                # 遅延が積み重なった場合は即次ループ
                next_tick = time.time()

    # 接続状態
    def get_connection_info(self):
        """
        現在の接続状態情報を返す
        戻り値: dict - 接続タイプ、状態、エラーメッセージ（存在する場合）
        """
        return {
            "connection_type": self.connection_type,
            "status": "connected" if self.oled_ok else "disconnected",
            "error": self.error_msg,
        }

    # 停止時の処理
    def stop_worker_swap(self):
        """
        従来のフォールバック互換API。
        本来は特定ポートのペア表示を止める処理だが、
        stop_all や stop_worker_swap_all がある環境では何もしない。
        """
        try:
            # もし stop_worker_swap_all が実装されているなら、それに丸投げする
            if hasattr(self, "stop_worker_swap_all"):
                self.stop_worker_swap_all()
            else:
                # 旧API呼び出し互換のため、何もせずログに残すだけ
                import logging
                logging.info("OledDisplayManager.stop_worker_swap() called (noop).")
        except Exception as e:
            import logging
            logging.warning(f"OledDisplayManager.stop_worker_swap() failed: {e}")

    # アニメーション処理
    def display_animation(
        self, frames_dir, default_frame_time=0.10, meta_json="frame_times.json"
    ):
        """
        SSD1309で指定ディレクトリの連番PNGアニメーションを表示
        読み出し時に全フレームをメモリへ事前ロードしてから再生する。
        """
        # 1) PNGファイル取得（番号順ソート）
        try:
            entries = os.listdir(frames_dir)
        except Exception as e:
            logging.error(f"Failed to list frames_dir '{frames_dir}': {e}")
            return

        png_files = sorted(
            os.path.join(frames_dir, f) for f in entries if f.lower().endswith(".png")
        )
        if not png_files:
            logging.error(f"No PNG files found in {frames_dir}")
            return

        # 2) メタデータの読み込み
        times_path = os.path.join(frames_dir, meta_json)
        frame_times = None
        if os.path.isfile(times_path):
            try:
                with open(times_path, encoding="utf-8") as fp:
                    frame_times = json.load(fp)
            except Exception as e:
                logging.warning(f"Failed to read {meta_json}: {e}")
        if not isinstance(frame_times, list):
            frame_times = []
        # 長さ調整
        if len(frame_times) < len(png_files):
            frame_times.extend(
                [default_frame_time] * (len(png_files) - len(frame_times))
            )
        elif len(frame_times) > len(png_files):
            frame_times = frame_times[: len(png_files)]

        # 3) 全フレーム事前ロード（必要なら1bitへ変換）
        frames = []
        for idx, png_path in enumerate(png_files):
            try:
                with Image.open(png_path) as im:
                    img = im.convert("1")  # SSD1309向けに1bit
                    frames.append(img.copy())  # クローズ後も使えるようコピー
            except Exception as e:
                logging.error(f"Failed to load frame {png_path}: {e}")
                frames.append(None)
        # 読み込み成功が1枚もなければ終了
        if not any(f is not None for f in frames):
            logging.error("No valid frames could be loaded.")
            return

        # 4) 再生（I/Oなし）
        for idx, img in enumerate(frames):
            if img is None:
                continue
            try:
                if self.device:
                    self.device.display(img)
                time.sleep(frame_times[idx])
            except Exception as e:
                logging.error(f"Failed to display preloaded frame index={idx}: {e}")

        # 5) 必要なら既定画面復帰
        # self.update(...)

    def play_pair_animation(
        self,
        frames_dir: Optional[str] = None,
        frame_time: float = 0.08,
        meta_json: str = "frame_times.json",
    ):
        """
        ペア成立時のアニメーション表示
        - frames_dir: ペア成立アニメPNGディレクトリ。未指定時は設定/既定パスから解決。
        """
        # frames_dir 解決: 引数 > self.config['pair_animation_dir'] > 既定
        cfg_dir = None
        if hasattr(self, "config") and isinstance(self.config, dict):
            cfg_dir = self.config.get("pair_animation_dir")

        candidate = frames_dir or cfg_dir or DEFAULT_PAIR_ANIM_DIR
        anim_dir = os.path.abspath(candidate)

        # 存在確認
        if not os.path.isdir(anim_dir):
            logging.warning(
                f"pair_animation: directory not found: {anim_dir} (specify frames_dir or set config 'pair_animation_dir')"
            )
            return

        # フレーム存在の簡易確認（display_animationでも検証するが、ここでも案内用にチェック可）
        try:
            entries = os.listdir(anim_dir)
        except Exception as e:
            logging.error(f"pair_animation: failed to list directory '{anim_dir}': {e}")
            return
        if not any(f.lower().endswith(".png") for f in entries):
            logging.warning(f"pair_animation: no PNG frames in {anim_dir}")
            return

        # 既存の display_animation を利用（元の挙動を完全踏襲）
        self.display_animation(
            anim_dir, default_frame_time=frame_time, meta_json=meta_json
        )

    # スクリーンセーバー（消灯/低コントラスト）状態へ移行する
    def _enter_screensaver(self):
        if not self.device:
            return
        try:
            cs = int(self.display_cfg.get("screensaver_contrast", 0))
            if hasattr(self.device, "contrast"):
                self.device.contrast(max(0, min(255, cs)))

            # 真っ黒化（PILがある場合は画像で、ない場合はclear()があれば呼ぶ）
            if Image is not None:
                try:
                    img = Image.new("1", (OLED_WIDTH, OLED_HEIGHT), 0)
                    self.device.display(img)
                except Exception as ie:
                    logging.debug(f"Screensaver image draw failed, fallback to clear(): {ie}")
                    if hasattr(self.device, "clear"):
                        self.device.clear()
            else:
                if hasattr(self.device, "clear"):
                    self.device.clear()

            if DEBUG_MODE:
                logging.info("OLED screensaver ON")
        except Exception as e:
            logging.warning(f"Failed to enter screensaver: {e}")

    # スクリーンセーバー状態から復帰し、コントラストを既定値へ戻す
    def _exit_screensaver(self):
        if not self.device:
            return
        try:
            contrast = int(self.display_cfg.get("contrast", 255))
            if hasattr(self.device, "contrast"):
                self.device.contrast(max(0, min(255, contrast)))
            if DEBUG_MODE:
                print(f"[DEBUG] OLED screensaver OFF")
                logging.info("OLED screensaver OFF")
        except Exception as e:
            logging.warning(f"Failed to exit screensaver: {e}")

    def debug_info(self):
        """
        デバッグ情報を辞書形式で返す
        """
        info = {
            "connection_type": self.connection_type,
            "status": "connected" if self.oled_ok else "disconnected",
            "error": self.error_msg,
            "display_data_keys": list(self.display_data.keys())
            if self.display_data
            else None,
            "thread_alive": self.thread.is_alive() if hasattr(self, "thread") else None,
            "device_type": type(self.device).__name__ if self.device else None,
            "font_path": OLED_FONT_PATH,
            "font_exists": os.path.exists(OLED_FONT_PATH),
        }
        return info


# OLEDが使えない場合のダミークラス（テスト・デバッグ用）
class DummyLCD:
    """
    OLED未接続もしくはライブラリ未導入環境用のダミーLCD。
    画面の代わりにprintで動作内容を通知。
    OledDisplayManagerと同じインターフェースを提供し、互換性を確保。
    """

    # ダミーLCDの初期化
    def __init__(
        self, connection_type: Optional[str] = None, config: Optional[dict] = None
    ):
        self.connection_type = connection_type or OLED_CONNECTION_TYPE
        self.display_data = {}
        self.error_msg = None
        self.stopped = False
        self._screensaver = False
        self._last_activity_time = time.time()
        self.config = config or {}
        self.display_cfg = {
            **DEFAULT_DISPLAY_CONFIG,
            **(self.config.get(DISPLAY_SECTION, {}) or {}),
        }

        logging.info(f"[DummyLCD] Initialized with {self.connection_type} mode")
        if DEBUG_MODE:
            print(
                f"[DummyLCD] DEBUG MODE ON - initialized with {self.connection_type} mode"
            )

    # コンテキストマネージャーのサポート
    def __enter__(self):
        return self

    # コンテキストマネージャー終了時のクリーンアップ
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    # 表示内容データを受け取り、画面の代わりにログに出力
    def update(self, **kwargs):
        self.display_data.update(kwargs)
        self._last_activity_time = time.time()
        if self._screensaver:
            self._screensaver = False
            self._exit_screensaver()
        if DEBUG_MODE:
            print(f"[LCD] Update: {kwargs}")
        else:
            logging.debug(f"[LCD] Update: {kwargs}")

    # エラーメッセージをログに出力
    def show_error(self, msg: str):
        self.error_msg = msg
        logging.error(f"[LCD][ERR] {msg}")
        if DEBUG_MODE:
            print(f"[LCD][ERR] {msg}")

    # 停止処理（リソース解放はないがインターフェース互換性のため）
    def stop(self):
        self.stopped = True
        if DEBUG_MODE:
            print("[LCD] Stopped")
        logging.debug("[LCD] Stopped")

