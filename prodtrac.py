#!/usr/bin/python3
# -*- coding: utf-8 -*-
# prodtac.py
###########################################################################
# Filename      :prodtrac.py
# Description   :Prod Track (Product Tracing)
# Author        :Akihiko Fujita
# Update        :2025/10/10
############################################################################

# 標準ライブラリ
import logging
import os
import re
import signal
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Optional, Tuple

# サードパーティのライブラリ
import configparser
import serial
from oracledb.exceptions import DatabaseError
from sqlalchemy import CHAR, Column, Date, DateTime, Integer, Numeric, String, create_engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

# 2025/7 OLED画面表示対応用ローカルライブラリ
from oled_manager import DummyLCD, OLED_AVAILABLE, OledDisplayManager

# グローバルなデータベース接続を管理するための変数
connection        = None
thread_local      = threading.local()

# 各辞書などを初期化
port_lock         = threading.Lock()
current_state     = {} # port(str) -> dict(state)
pair_state        = {} # port(str) -> dict(pair info)
connection_status = {} # 
settings_map      = {} # 必要なら port(str) -> settings(dict)

# バージョン
Version = "0.9.98"

# OracleDBの接続設定:接続先の変更が無いので、ハードコーディング
username       = "seisan_syslog_v1"
password       = "Toso01Manager"
host           = "scan09.toso.co.jp"
port           = "1521"
service_name   = "togodb"
connection_url = f"oracle+oracledb://{username}:{password}@{host}:{port}/?service_name={service_name}"

# エンジンとセッションの作成、NullPoolを用いてクエリごとに独立したセッションとして扱う
engine = create_engine(connection_url, poolclass=NullPool)

# セッションファクトリを一度だけ定義
SessionFactory = sessionmaker(bind=engine)

# lcd呼び出しのためのグローバル変数、処理はメインルーチンにあります
lcd = None

# LCDタイマー管理用グローバル変数、処理はタイマースレッドルーチンにあります
lcd_timer_threads     = {}  # port -> threading.Thread
lcd_timer_stop_events = {}  # port -> threading.Event
lcd_last_snapshots    = {}  # port -> snapshot dict
lcd_timer_generations = {}  # 追加: portごとの世代ID

# Baseクラスを使用してテーブルを定義Baseクラスの宣言（すべてのORMクラスがこのベースを継承）
Base = declarative_base()

# グローバルロガーの設定
logger = logging.getLogger(__name__)

# デバッグモードフラグ（必要に応じて変更）デバッグモードは処理、デバッグデータは扱う値のトレースです
DEBUG_MODE = True  # デバッグモードを True:有効にする / False:無効にする 画面上の情報量が変わります
DEBUG_DATA = False  # デバッグデータを True:有効にする / False:無効にする 画面上の情報量が変わります

if DEBUG_MODE:
    print(
        "\033[38;2;0;128;255m"
        "\n"
        ">>>>> DEBUG MODE ONLINE <<<<<\n"
        " Running in diagnostics mode.\n"
        " All logs will be verbose\n"
        " Get hack the Prodtrac!\n"
        ">>>>>>>>>>>>>>><<<<<<<<<<<<<<\033[0m"
    )

if DEBUG_DATA:
    print(
        "\033[38;2;255;0;255m"
        "\n"
        "=== DEBUG DATA MODE ONLINE ======================\n"
        " Collecting structured diagnostics for data paths.\n"
        " Verbose tracing is enabled (DB/QR/State/Commit).\n"
        " Use repr() to detect hidden characters in inputs.\n"
        "==================================================\033[0m"
    )

# SSD1309表示ポート（いずれか1つ）
DISPLAY_SECTION = "PortSettings1"  # 画面に今出す設定セクション。変えたければここを"PortSettings1"または"PortSettings2"に書き換える

# GUIモードフラグ（必要に応じて変更、Raspberry Piで動かすのならFalse）
SPLASH_SCREEN = False  # スプラッシュスクリーンを True:有効にする / False:無効にする

# 停止イベントをグローバル変数として宣言
threads = []
stop_event = threading.Event()
threads_lock = threading.Lock()

# OLEDマネージャ
oled_manager = OledDisplayManager()

# 作業状態、LCD表示を考慮しスペースを入れ全角5桁揃えで統一
STATUS_WORKING =    "作業中　　"
STATUS_WAITING =    "待機中　　"
STATUS_ENDED =      "作業終了　"
STATUS_RETRY =      "再接続中　"

# 手直し状態、LCD表示を考慮しスペースを入れ全角4桁揃えで統一 (文字先頭に*が表示付与される為、上より1文字少ない)
status_mapping = {
    "rew_own_fix":  "手直し　",
    "rew_material": "材料不良",
    "rew_process":  "加工不良",
    "rew_equipm":   "設備不良",
    "rework":       "手戻手直",
}

# テーブル定義
class Production(Base):
    __tablename__ = "t_prod_trac_input"  # テーブル名の指定

    tracking_seq = Column(Integer, primary_key=True, autoincrement=True)   # 追跡シーケンス（自動インクリメント、主キー）
    worker_cd = Column(String(10))             # 従業員コード         （最大10文字）
    process_cd = Column(CHAR(5))               # 工程コード           （固定5文字）
    status = Column(String(32))                # ステータス           （最大32文字）
    start_dt = Column(DateTime)                # 作業開始日時         （DateTime形式）
    end_dt = Column(DateTime)                  # 作業終了日時         （DateTime形式）
    work_time_sec = Column(Integer)            # 作業時間             （秒単位）
    qr_cd = Column(String(400))                # QRコードデータ       （最大300文字→400文字に拡張）
# これより下は着完システムと同一のカラムを設定
    seisan_tehai_no = Column(String(12))       # 生産手配No.          （最大12文字）
    seisan_tehai_sub_no = Column(String(3))    # 生産手配No.連番      （最大3文字）
    juchu_no = Column(String(11))              # 受注No.              （最大11文字）
    check_no = Column(String(13))              # チェックNo.          （最大13文字）
    daisu_no = Column(String(12))              # 台数No.              （最大12文字）
    kyoten_cd = Column(String(6))              # 拠点コード           （最大6文字）
    seisakusho_fuka_cd = Column(String(6))     # 製作所負荷コード     （最大6文字）
    seisakusho_mae_cd = Column(String(6))      # 製作所前工程コード   （最大6文字）
    seisakusho_ato_cd = Column(String(6))      # 製作所後工程コード   （最大6文字）
    shohingun_cd = Column(String(1))           # 商品群コード         （最大1文字）
    seisanbi = Column(String(8))               # 生産日               （最大8文字：yyyyMMdd形式）
    seisanbi_dt = Column(DateTime)             # 生産日               （DateTime形式）
    seisan_check_sub_no = Column(String(3))    # 生産チェックサブNo.  （最大3文字）
    shukkabi = Column(String(8))               # 出荷日               （最大8文字：yyyyMMdd形式）
    shukka_basho = Column(String(2))           # 出荷場所             （最大2文字）
    hontai_kbn = Column(String(1))             # 本体区分             （最大1文字）
# これより下はProd Trac向けに追加したカラム
    hinmei = Column(String(64))                # 品名                 （最大27文字）
    width = Column(String(5))                  # 製品幅               （最大5文字）
    height = Column(String(5))                 # 製品丈               （最大5文字）
    honseki_cd = Column(String(4))             # 本籍品番             （最大4文字）
    model_cd = Column(String(2))               # モデルコード         （最大2文字）
    db_bunrui_cd = Column(String(3))           # DB分類コード         （最大3文字）
# これより下はシステムでは展開せず、生産手配No.をキーに他DBより持ってくる予定
    worker_name = Column(String(64))           # 従業員名             （最大64文字）
    process_name = Column(String(64))          # 工程名               （最大64文字）
    tehai_suryo = Column(Numeric(12, 3))       # 手配数量             （最大12桁、小数点以下3桁）
    kansan_mae = Column(Numeric(9, 6))         # 換算前               （最大9桁、小数点以下6桁）
    kansan_ato = Column(Numeric(9, 6))         # 換算後               （最大9桁、小数点以下6桁）
    shoshizai_cd = Column(String(8))           # 商資材コード         （最大8文字）
    betchu_cd = Column(String(20))             # 別注コード           （最大20文字）
    total_meter = Column(Numeric(6, 0))        # 合計メートル         （最大6桁、整数）
    fuka_kanzan = Column(Numeric(9, 6))        # 負荷換算             （最大9桁、小数点以下6桁）
    kansan_ato_hjn = Column(Numeric(9, 6))     # 負荷換算後           （最大9桁、小数点以下6桁）
    kansan_mae_hjn = Column(Numeric(9, 6))     # 負荷換算前           （最大9桁、小数点以下6桁）

# 作業者マスタ定義
class WorkerMaster(Base):
    __tablename__ = 'worker_master'
    worker_cd     = Column(String(10), primary_key=True)  # 従業員コード、主キー
    worker_name   = Column(String(64))        # 氏名
    worker_kana   = Column(String(64))        # カナ
    worker_lcd    = Column(String(8))         # 画面表示用 氏名略称（最大8文字まで）
    is_valid      = Column(CHAR(1))           # 
    dept_cd       = Column(String(10))        # 
    employment_cls= Column(CHAR(2))           # 
    birth_date    = Column(Date)              # 
    remarks       = Column(String(255))       # 
    created_at    = Column(DateTime)          # 
    updated_at    = Column(DateTime)          # 

# 工程マスタ定義
class ProcessMaster(Base):
    __tablename__ = 'process_master'
    process_cd      = Column(CHAR(5), primary_key=True)  # 工程コード、主キー
    process_name    = Column(String(64))      # 工程名の正式名
    process_lcd     = Column(String(14))      # 画面表示用 工程名略称（最大14文字まで）
    mae_ato         = Column(String(5))       # 工程前後(MAE または ATO)
    is_active       = Column(CHAR(1))         # 
    std_time_min_val= Column(Numeric(5,2))    # 
    std_time_min_expr = Column(String(255))   # 
    std_time_dcr    = Column(String(255))     # 
    remarks         = Column(String(255))     # 
    created_at      = Column(DateTime)        # 
    updated_at      = Column(DateTime)        # 

# 間接作業マスタ定義
class IndirectWorkMaster(Base):
    __tablename__ = 'indirect_work_master'
    work_code   = Column(CHAR(3), primary_key=True)   # 間接作業コード（QRから取得、主キー）
    record_name = Column(String(32), nullable=False)  # レコード登録用の間接作業名（statusに展開される値、日本語も可）
    lcd_label   = Column(String(6),  nullable=False)  # LCD表示用ラベル（全角3文字／半角6文字まで）
    category    = Column(String(32), nullable=False)  # カテゴリ（業務分類）
    work_name   = Column(String(255))                 # 作業内容（概要説明）


# フォールバック専用ロガー保存先ディレクトリ
os.makedirs("qr_fallback", exist_ok=True)

fallback_logger = logging.getLogger("qr_fallback")
if not fallback_logger.handlers:  # 二重設定防止
    handler = RotatingFileHandler(
        "qr_fallback/fallback_log.txt",
        maxBytes=1 * 1024 * 1024,  # 1MB
        backupCount=5,             # 最大5世代
        encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)s, %(message)s", "%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)
    fallback_logger.addHandler(handler)
    fallback_logger.setLevel(logging.INFO)

# セッションを管理するコンテキストマネージャ。再試行ロジックを追加。
@contextmanager
def session_scope(max_retries=5):
    """
    引数:   max_retries (int): 最大再試行回数
    戻り値: session: SQLAlchemyセッションオブジェクト。
    例外:   SQLAlchemyError: 最大再試行回数を超えた場合に送出。
    """

    for attempt in range(max_retries):
        try:
            # セッションを作成
            session = SessionFactory()
            break  # 成功したらループを抜ける

        except Exception as e:
            logging.error(f"Failed to create session (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2**attempt)  # 繰り返し処理の際、段階的に感覚を長くする
            else:
                raise  # 最後の試行で失敗した場合は例外を再スロー

    try:
        # セッションを呼び出し元に提供、処理が成功した場合はコミット
        yield session
        logging.info("Session commit")
        session.commit()

    # 例外が発生した場合はロールバック、上位の例外処理に通知
    except Exception as e:
        session.rollback()
        logging.error(f"Session rollback due to: {e}")
        raise

    finally:
        logging.info("Session close")
        session.close()


# データベースの初期化
def init_db():
    """
    機能:   データベースの初期化を行います。
    引数:   なし
    戻り値: なし
    例外:   SQLAlchemyError: データベースの初期化中にエラーが発生した場合。
    """

    try:
        # テーブルの作成
        Base.metadata.create_all(engine)
        print("OracleDB database initialized successfully.")

    except SQLAlchemyError as e:
        print(f"SQLAlchemy error occurred: {e}")
        logging.error(
            f"SQLAlchemy error occurred while initializing the OracleDB database: {e}"
        )
        show_fatal_error(oled_manager, "E01")
        sys.exit(1)

    except Exception as e:
        print(f"Unexpected error: {e}")
        logging.error(
            f"An unexpected error occurred while initializing the OracleDB database: {e}"
        )
        show_fatal_error(oled_manager, "E01")
        sys.exit(1)

# current_stateの初期化
def init_current_state(port: str, lcd=None, worker_cd=None, process_cd=None, worker_lcd="", process_lcd=""):
    """
    指定ポートの current_state を初期化する。
    Args:
        port (str): シリアルポート名（例: /dev/ttyACM0）
        lcd: LCD インスタンス（存在すれば渡す）
        worker_cd (str): 初期の作業者コード
        process_cd (str): 初期の工程コード
        worker_lcd (str): DBから解決済みの表示名
        process_lcd (str): DBから解決済みの表示名
    """
    current_state[port] = {
        "status": STATUS_WAITING,     # 作業状態（待機中／作業中／終了／再接続中）
        "worker_cd": None,            # 必須：1人目の作業者コード
        "worker2_cd": None,           # 任意：2人目の作業者コード（ペア時のみ）
        "process_cd": None,           # 工程コード
        "qr_cd": None,                # 現在の加工指示書QR
        "worker_lcd": "",             # LCD表示用の作業者名（1人目）
        "worker2_lcd": "",            # LCD表示用の作業者名（2人目）
        "process_lcd": "",            # LCD表示用の工程名
        "check_no": None,             # 検査番号（DB保存用）
        "check_no_lcd": "      ",     # LCD表示用（全角揃え）
        "start_time": None,           # 作業開始時刻
        "lcd": lcd,                   # LCDインスタンス
        "timer": "00:00",             # 表示中のタイマー
        "rework_status": None,       # 手戻り手直し処理をワンショットで記録する用
    }
    init_pair_state_for_port(port)

    logging.debug(f"[init_current_state] Initialized port={port}, worker_cd={worker_cd}, process_cd={process_cd}")
    return current_state[port]


# ペアモードの初期化
def init_pair_state_for_port(port):
    """
    ペア作業モードの補助情報を初期化する。
    （作業者コード自体は current_state に保持する）
    """
    pair_state[port] = {
        "pair_mode": False,          # ペア作業モード中かどうか
        "last_worker_ts": None,      # 最後に作業者QRを読んだ時刻
        "waiting_second_qr": False,  # 2人目待機状態フラグ（将来拡張用）
        "recent_workers": [],        # 直近のQR履歴（最大3件）
    }


# 設定ファイルの中身の整合性チェック
def validate_config(config, sections):
    """
    機能:   設定ファイルの検証を行います。
    引数:   config (dict): 設定情報を格納した辞書。
            sections (list): 検証するセクションのリスト。
    戻り値: bool: 設定が有効な場合はTrue、それ以外はFalse。
    例外:   ValueError: 設定が無効な場合に発生。
    """
    try:
        for section in sections:
            if section not in config:
                raise configparser.NoSectionError(section)
            for option in ["port", "baudrate", "bytesize", "parity", "stopbits", "timeout"]:
                if option not in config[section]:
                    raise configparser.NoOptionError(option, section)

        return True  # OKの場合はTrueを返す

    except Exception as e:
        logging.exception("Config error")
        show_fatal_error(oled_manager, "E02")
        sys.exit(1)


# シリアル機器単位でバリデーションチェック
def select_valid_port_sections(config, port_map):
    """
    機能:   PortSettings 単位で validate_config を適用し、不正セクションを除外する。
    戻り値: valid_map: {port: section} のみ（有効なセクションのみ）
            invalid_sections: [(section, error)] のリスト（ログ用）
    """
    valid_map = {}
    invalid_sections = []

    for port, section in port_map.items():
        try:
            # 単一セクションだけを対象に検証
            validate_config(config, [section])
            # 追加の軽微な検証（数値・選択肢チェックなど任意）
            _validate_and_normalize_port_options(config, section)
            valid_map[port] = section
        except Exception as e:
            invalid_sections.append((section, e))

    return valid_map, invalid_sections


# 通信処理条件のバリデーションチェック
def _validate_and_normalize_port_options(config, section):
    """
    機能:   追加の妥当性チェック（必要に応じて拡張）。
            数値項目が正の整数か & parity/bytesize/stopbits の値が想定範囲か
            不正なら例外を送出して上位に処理を委ねる。
    """
    s = config[section]

    # ボーレート、通信速度
    br = int(s.get("baudrate"))
    if br <= 0:
        raise ValueError(f"Invalid baudrate in {section}: {br}")

    # バイトサイズ、転送量
    valid_bytesize = {5, 6, 7, 8}
    bs = int(s.get("bytesize"))
    if bs not in valid_bytesize:
        raise ValueError(
            f"Invalid bytesize in {section}: {bs} (valid: {sorted(valid_bytesize)})"
        )

    # パリティビット
    valid_parity = {"N", "E", "O", "M", "S"}  # None, Even, Odd, Mark, Space
    parity = s.get("parity").upper()
    if parity not in valid_parity:
        raise ValueError(
            f"Invalid parity in {section}: {parity} (valid: {sorted(valid_parity)})"
        )

    # ストップビット 1.5は実機で見たこと無いので展開しない。
    valid_stopbits = {1, 2}
    sb = int(float(s.get("stopbits")))  # "1", "1.0" の表現ゆれ対策
    if sb not in valid_stopbits:
        raise ValueError(
            f"Invalid stopbits in {section}: {s.get('stopbits')} (valid: 1 or 2)"
        )

    # タイムアウト
    to_val = float(s.get("timeout"))
    if to_val < 0:
        raise ValueError(f"Invalid timeout in {section}: {to_val} (must be >= 0)")


# シリアル機器の個別検証して有効なものだけ残す
def handle_port_validation_and_continue(config, port_map, oled_manager=None):
    """
    PortSettings 単位で検証し、有効なものだけ残す。
    1台も残らなければ致命的エラーとして安全に終了する。
    戻り値: valid_map: {port: section}
    """
    valid_map, invalids = select_valid_port_sections(config, port_map)

    # 不正セクションのログ
    for section, err in invalids:
        logging.error(f"Invalid PortSettings section excluded: {section} -> {err}")
        if DEBUG_MODE:
            import traceback
            traceback.print_exc()

    # 1台も有効でない場合は、安全に終了
    if not valid_map:
        logging.critical("No valid serial port configuration found. Exiting safely.")
        show_fatal_error(oled_manager, "E02")  # LCD にエラー出力
        sys.exit(1)  # 安全に終了

    else:
        # 有効・無効の概要を情報ログ
        logging.info(f"Valid ports: {list(valid_map.keys())}")
        if invalids:
            logging.warning(f"Ignored invalid sections: {[s for s, _ in invalids]}")

    return valid_map


# LCDへの表示ルール DB上に未登録だった際の処理
def _format_missing_label(code, mode):
    if mode == "empty":
        return ""
    if mode == "raw":
        return str(code) if code is not None else ""
    if mode == "prefixed":
        # "未:<code>" 形式
        return f"未:{code}" if code is not None else "未:"
    # 既定 or "label"
    return "未登録"


# LCDへの作業者表示
def get_worker_lcd(session, worker_cd, mode=None, default_mode="label"):
    """
    引数:   mode: None/label/empty/raw/prefixed
            default_mode: mode が None の場合に使用する既定
    戻り値: ヒット時は worker_lcd、未登録時は mode に応じた表示
    """
    use_mode = mode or default_mode
    w = session.query(WorkerMaster).filter_by(worker_cd=worker_cd).first()
    value = (
        w.worker_lcd
        if w and getattr(w, "worker_lcd", None)
        else _format_missing_label(worker_cd, use_mode)
    )

    if DEBUG_MODE:
        print(
            f"[DEBUG][get_worker_lcd] worker_cd={worker_cd}, 検索結果={w}, return='{value}', mode={use_mode}"
        )
    return value


# LCDへの工程表示
def get_process_lcd(session, process_cd, mode=None, default_mode="label"):
    """
    引数:   mode: None/label/empty/raw/prefixed
            default_mode: mode が None の場合に使用する既定
    戻り値: ヒット時は process_lcd、未登録時は mode に応じた表示
    """
    use_mode = mode or default_mode
    p = session.query(ProcessMaster).filter_by(process_cd=process_cd).first()
    value = (
        p.process_lcd
        if p and getattr(p, "process_lcd", None)
        else _format_missing_label(process_cd, use_mode)
    )

    if DEBUG_MODE:
        print(
            f"[DEBUG][get_process_lcd] process_cd={process_cd}, 検索結果={p}, return='{value}', mode={use_mode}"
        )
    return value


# シリアルポートを開く
def open_serial_port(settings, *, retry_enabled=True):
    """
    シリアルポートを開く（retry_enabled=Trueなら最大5回のリトライ、Falseならリトライなし）
    """
    port = settings.get("port")
    if not port:
        raise ValueError("settings['port'] is required")

    attempts = 2 if retry_enabled else 1  # ←表示対象以外なら 1 回だけ
    with port_lock:
        for attempt in range(attempts):
            try:
                if not os.path.exists(port):
                    raise serial.SerialException(f"Specified serial device {port} not found.")

                ser = serial.Serial(
                    port=port,
                    baudrate=int(settings["baudrate"]),
                    bytesize=int(settings["bytesize"]),
                    parity=settings["parity"],
                    stopbits=int(settings["stopbits"]),
                    timeout=int(settings.get("timeout", 1)),
                    write_timeout=int(settings.get("write_timeout", 0)),
                    inter_byte_timeout=float(settings.get("inter_byte_timeout", 0.5)),
                )
                logging.info(f"Successfully connected to {port}")
                return ser

            except ValueError as e:
                logging.error(f"Invalid serial settings for {port}: {e}")
                raise

            except serial.SerialException as e:
                logging.warning(
                    f"Error opening {port} (attempt {attempt + 1}/{attempts}): {e}"
                )
                if attempt < attempts - 1:
                    time.sleep(2**attempt)
                    continue
                raise

            except Exception as e:
                logging.error(f"Unexpected error while opening {port}: {e}")
                raise


# 再接続処理
def reconnect(port, display_lcd=None, worker_cd=None, process_cd=None, qr_cd=None, *, reason=None, retry=0, max_retry=3, backoff_sec=1.0, **kwargs,):
    """
    シリアルデバイスの再接続と、必要に応じてLCD更新を行う。
    呼び出し元から余分な引数が渡されても無視するので安全。
    今回のシリアル通信は相互通信ではなく、機器からの一方通行のため機器の生死応答が困難
    特にBluetoothのSPPの場合、切断してもポートが閉じないため呼び出しが掛からない
    有線シリアルの場合、ポートが閉じるため機能する可能性もあるが、ここまでやっても復帰は難しいと思われる
    """
    # 状態辞書を安全に初期化
    if port not in current_state:
        init_current_state(port)

    # 初期値を上書き
    current_state[port].update({
        "worker_cd": worker_cd or current_state[port].get("worker_cd"),
        "process_cd": process_cd or current_state[port].get("process_cd"),
        "qr_cd": qr_cd or current_state[port].get("qr_cd"),
        "lcd": display_lcd or current_state[port].get("lcd"),
    })

    # display_lcd が未指定なら状態から補完
    if display_lcd is None:
        display_lcd = current_state[port].get("lcd")

    # 作業情報の補完
    worker_cd = worker_cd or current_state[port].get("worker_cd")
    process_cd = process_cd or current_state[port].get("process_cd")
    qr_cd = qr_cd or current_state[port].get("qr_cd")

    if reason:
        logging.info(
            f"[reconnect] port={port}, reason={reason}, retry={retry}/{max_retry}"
        )

    try:
        # 設定を決定（current_state 優先、無ければ settings_map）
        settings = current_state[port].get("settings") or settings_map.get(port)
        if settings is None:
            raise ValueError(f"No serial settings found for port={port}")

        # 既存ハンドルを安全にクローズ
        old_ser = current_state[port].get("serial")
        if old_ser is not None:
            try:
                if getattr(old_ser, "is_open", False):
                    old_ser.close()
            except Exception as e_close:
                logging.warning(f"[reconnect] failed to close old handle on {port}: {e_close}")
            finally:
                current_state[port]["serial"] = None

        # 新しい接続を開く
        ser = open_serial_port(settings)
        current_state[port]["serial"] = ser

        # LCD更新（失敗しても止めない）
        if display_lcd is not None and current_state[port].get("worker_cd") and current_state[port].get("process_cd"):
            try:
                with session_scope() as session:
                    # ペアかどうかで表示する worker_cd を決定
                    if current_state[port].get("worker2_cd"):
                        workers = [current_state[port]["worker_cd"], current_state[port]["worker2_cd"]]
                    else:
                        workers = current_state[port]["worker_cd"]

                    _lcd_update_full(
                        session=session,
                        qr_cd=current_state[port]["qr_cd"],
                        lcd=display_lcd,
                        status_str=STATUS_RETRY,
                        show_rework=False,
                        port=port,
                    )
            except Exception as e:
                logging.warning(f"[reconnect] LCD update failed: {e}")

        logging.info(f"[reconnect] success: port={port}")
        return True

    except ValueError as e:
        logging.error(f"[reconnect] Value error on port={port}: {e}")
        return False

    except serial.SerialException as e:
        logging.error(f"[reconnect] Serial connection error on port={port}: {e}")

        # E07: シリアル未検出エラーをLCDに表示
        show_temp_error(oled_manager, "E07")

        if retry < max_retry:
            time.sleep(backoff_sec)
            return reconnect(
                port=port,
                display_lcd=display_lcd,
                worker_cd=worker_cd,
                process_cd=process_cd,
                qr_cd=qr_cd,
                reason=reason,
                retry=retry + 1,
                max_retry=max_retry,
                backoff_sec=min(backoff_sec * 2, 30.0),
            )
        else:
            # 最大リトライ失敗 → 致命エラーを出しっぱなし表示
            logging.critical(f"[reconnect] max retry exceeded on port={port}")
            show_fatal_error(oled_manager, "E07", hold=True)
            return False

    except Exception as e:
        logging.exception(f"[reconnect] unexpected error on port={port}: {e}")
        return False


# シリアルポートからデータを読み取る
def read_from_port(ser, port, last_qr_codes, stop_event, config, section, lcd, pair_state, oled_manager=None):
    """
    シリアルポートからデータを読み取るスレッド。
    引数順は main_program のスレッド生成と一致させること。
    - ser: シリアルインスタンス
    - port: 『/dev/ttyACM0』のようなポート文字列
    - last_qr_codes: dict
    - stop_event: threading.Event
    - config: ConfigParser または dict
    - section: このポートに対応するセクション名
    - lcd: フォールバックインスタンス
    - pair_state: ペアモード状態用の dict
    - oled_manager: オプションのOLEDマネージャー
    """

    # pair_state がない場合は初期化
    if port not in pair_state:
        init_pair_state_for_port(port)

    if ser is None:
        logging.warning(f"[{port}] serial is None; skipping thread")
        return

    buffer = ""

    while not stop_event.is_set():
        # 接続状態でなければ短時間待機
        if not connection_status.get(port, False):
            if stop_event.wait(timeout=0.1):
                break
            continue

        try:
            n = getattr(ser, "in_waiting", 0)
            if n == 0:
                if stop_event.wait(timeout=0.05):
                    break
                continue

            data_chunk = ser.read(n).decode("shift_jis", errors="ignore")
            buffer += data_chunk

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip("\r").strip()
                if not line:
                    continue

                # current_state が未初期化なら init する
                if port not in current_state:
                    init_current_state(port, lcd)

                # LCD は current_state 優先、なければ引数 lcd
                lcd_obj = current_state[port].get("lcd") or lcd
                current_state[port].update({"lcd": lcd_obj})

                if lcd_obj:
                    try:
                        lcd_obj.display_message(line)
                    except Exception as e:
                        logging.warning(f"[{port}] lcd.display_message failed: {e}")

                # DB セッションを開いて処理（session_scope を使う既存の方式に合わせる）
                with session_scope() as session:
                    try:
                        process_data(
                            data=data_chunk, line=line, session=session,
                            port=port, last_qr_codes=last_qr_codes,
                            config=config, section=section,
                            lcd=lcd_obj, pair_state=pair_state,
                            oled_manager=oled_manager,
                        )
                    except Exception as e:
                        logging.exception(f"[{port}] process_data failed: {e}")

        except Exception as e:
            logging.exception(f"[{port}] read loop unexpected error: {e}")
            if stop_event.wait(timeout=0.5):
                break


# QRコードの桁数を判定する
def extract_field(qr_code, start, length, field_name=None):
    """
    機能:   QRコードから特定のフィールドを抽出します。
    引数:   qr_code (str): QRコードの文字列。
            start (int): 抽出を開始する位置（0オリジン）。
            length (int): 抽出する長さ。
            field_name (str, optional): フィールド名（あればデバッグ用に出力）
    戻り値: str or None: 抽出されたフィールド。または失敗時None。
    """

    end = start + length

    if len(qr_code) >= end:
        result = qr_code[start:end]

        if DEBUG_DATA and len(qr_code) >= 300:
            print(
                f"[DEBUG][extract_field] Extracting {field_name or ''} substring from {start} to {end}: '{result}'"
            )

        return result
    else:
        # 抽出失敗時、どのfieldでどんなデータ長不足だったかをprint
        if DEBUG_DATA:
            print(
                f"[ERROR] Failed to extract field '{field_name or ''}' (from {start} to {end}). "
                f"QR code length={len(qr_code)}, content='{qr_code}'"
            )

        return None


# QRコードから全フィールドを抽出する
def extract_info(qr_code: str) -> Optional[dict]:
    """
    機能:   QRコードから必要な情報を抽出します。
    引数:   qr_code (str): QRコードの文字列。
    戻り値: dict: 抽出された情報の辞書。validate_extracted_data が False の場合は空辞書。
    例外:   ValueError: 情報の抽出に失敗した場合。
    """
    # 位置定義（仕様に合わせて調整）
    field_mappings = {
        'seisan_tehai_no': (0, 12),       # 生産手配No.
        'seisan_tehai_sub_no': (12, 3),   # 生産手配No.連番
        'juchu_no': (81, 11),             # 受注No.
        'check_no': [(45, 5), (20, 6)],   # チェックNo.（複数箇所から抽出）
        'daisu_no': (27, 7),              # 台数No.
        'kyoten_cd': (39, 6),             # 拠点コード
        'seisakusho_fuka_cd': (45, 6),    # 製作所負荷工程コード
        'seisakusho_mae_cd': (69, 6),     # 製作所前工程コード
        'seisakusho_ato_cd': (45, 6),     # 製作所後工程コード
        'shohingun_cd': (51, 1),          # 商品群コード
        'seisanbi': (52, 6),              # 生産日（yymmdd）
        'seisan_check_sub_no': (58, 3),   # 生産チェックNo.連番
        'shukkabi': (61, 6),              # 出荷日
        'shukka_basho': (67, 2),          # 出荷場所
        'hontai_kbn': (92, 1),            # 本体区分
        'hinmei': (105, 23),              # 品名
        'width': (127, 5),                # 製品幅
        'height': (132, 5),               # 製品丈
        'honseki_cd': (152, 4),           # 本籍品番
        'model_cd': (125, 2),             # モデル
        'db_bunrui_cd': (256, 3),         # DB分類コード
    }

    extracted_data = {}
    valid = True

    for key, value in field_mappings.items():
        if isinstance(value, list):
            substrings = [extract_field(qr_code, v[0], v[1], field_name=key) for v in value]
            field_value = "".join(filter(None, substrings)) if substrings else ""
            expect_len = sum(v[1] for v in value)
        else:
            field_value = extract_field(qr_code, value[0], value[1], field_name=key)
            expect_len = value[1]

        extracted_data[key] = field_value
        got_len = len(field_value) if field_value else 0
        if got_len != expect_len:
            valid = False
            if DEBUG_MODE:
                print(f"[WARN][extract_info] field='{key}' 桁数不一致 got={got_len} expect={expect_len}")

    # 生産日 seisanbi yymmdd → 日付変換
    seisanbi = extracted_data.get("seisanbi") or ""
    if seisanbi:
        try:
            extracted_data["seisanbi_dt"] = datetime.strptime(seisanbi, "%y%m%d").date()
        except Exception as e:
            valid = False
            extracted_data["seisanbi_dt"] = None
            if DEBUG_MODE:
                print(f"[WARN][extract_info] seisanbi 日付変換失敗: {e}")
    else:
        extracted_data["seisanbi_dt"] = None

    # QRコードをそのまま格納（異常系も含めてDBに残すため）
    extracted_data["qr_cd"] = qr_code

    # フォーマット異常 or バリデーションエラー時
    if not valid or not validate_extracted_data(extracted_data):
        if DEBUG_MODE:
            print(f"[DEBUG][extract_info] format/validation error → handle_error_qr(E05)")
        handle_error_qr("E05", qr_code)
        return None

    if DEBUG_MODE:
        print(f"[DEBUG][extract_info OK] {extracted_data}")

    return extracted_data


# QRコードのデータがDBのテーブル型と一致しない場合、書き込みエラーが発生する為、QRの先頭64文字を用いて有効か判定をおこなう
def validate_extracted_data(data):
    """
    機能:   抽出したデータの検証を行います。
    引数:   data (dict): 検証するデータを格納した辞書。
    戻り値: bool: データが有効である場合はTrue、それ以外はFalse。
    仕様:   qr_cd 長さが32以下（機能QR）は詳細バリデーションをスキップして True を返す
            それ以外（生産系QR）はフィールド長チェックを実施
    """
    # data 体裁と qr_cd 存在チェック
    if not (isinstance(data, dict) and "qr_cd" in data):
        if DEBUG_DATA:
            print(
                "[DEBUG][validate_extracted_data] invalid payload (no qr_cd or not dict)"
            )

        return False

    qr_text = str(data["qr_cd"]) if data["qr_cd"] is not None else ""
    if len(qr_text) <= 32:
        # 機能QRは詳細バリデーションを行わずに True（通過）とする
        if DEBUG_DATA:
            print(
                "[DEBUG][validate_extracted_data] functional QR (<=32 chars), skipping detail validation -> True"
            )
        return True

    if DEBUG_DATA:
        print(
            f"[DEBUG][validate_extracted_data] production-like QR, data keys={list(data.keys())}"
        )

    # 各フィールドの最大長を定義（DB型に合わせる）
    field_constraints = {
        'worker_cd': 10,                    # 作業者コード
        'process_cd': 5,                    # 工程コード
        'seisan_tehai_no': 12,              # 生産手配No.
        'seisan_tehai_sub_no': 3,           # 生産手配No.連番
        'juchu_no': 11,                     # 受注No.
        'check_no': 13,                     # チェックNo.（複数箇所から抽出）
        'daisu_no': 7,                      # 台数No.
        'kyoten_cd': 6,                     # 拠点コード
        'seisakusho_fuka_cd': 6,            # 製作所負荷工程コード
        'seisakusho_mae_cd': 6,             # 製作所前工程コード
        'seisakusho_ato_cd': 6,             # 製作所後工程コード
        'shohingun_cd': 1,                  # 商品群コード
        'seisanbi': 6,                      # 生産日（yymmdd）
        'seisan_check_sub_no': 3,           # 生産チェックNo.連番
        'shukkabi': 6,                      # 出荷日
        'shukka_basho': 2,                  # 出荷場所
        'hontai_kbn': 1,                    # 本体区分
        'hinmei': 23,                       # 品名
        'width': 5,                         # 製品幅
        'height': 5,                        # 製品丈
        'honseki_cd': 4,                    # 本籍品番
        'model_cd': 2,                      # モデル
        'db_bunrui_cd': 3,                  # DB分類コード
        'qr_cd': 400,                       # QRコード全体
    }

    # 各フィールドに対してバリデーションを実施
    for field, max_length in field_constraints.items():
        value = data.get(field, "")
        sval = "" if value is None else str(value)
        if len(sval) > max_length:
            if DEBUG_MODE:
                print(f"[DEBUG][validate] 入力: {data}")
                print(
                    f"[DEBUG][validate_extracted_data] field {field} value={sval} length={len(sval)} > {max_length}"
                )
            return False

    return True

# データをデータベースに挿入します
def insert_data(session: Optional[Session], worker_cd, process_cd, status, start_dt, qr_cd, extracted_info, commit=True,):
    """
    機能:   データをデータベースに挿入します。
    引数:   session (Optional[Session]): SQLAlchemyのセッションオブジェクト。Noneの場合は内部でsession_scope()を開く。
            commit (bool): 外部セッションが無い場合のみ有効。外部セッションがある場合は呼び出し側で管理してください。
    """
    # extracted_info の安全な成形
    extracted_info = {
        "seisan_tehai_no":     extracted_info.get("seisan_tehai_no", None),
        "seisan_tehai_sub_no": extracted_info.get("seisan_tehai_sub_no", None),
        "juchu_no":            extracted_info.get("juchu_no", None),
        "check_no":            extracted_info.get("check_no", None),
        "daisu_no":            extracted_info.get("daisu_no", None),
        "kyoten_cd":           extracted_info.get("kyoten_cd", None),
        "seisakusho_fuka_cd":  extracted_info.get("seisakusho_fuka_cd", None),
        "seisakusho_mae_cd":   extracted_info.get("seisakusho_mae_cd", None),
        "seisakusho_ato_cd":   extracted_info.get("seisakusho_ato_cd", None),
        "shohingun_cd":        extracted_info.get("shohingun_cd", None),
        "seisanbi":            extracted_info.get("seisanbi", None),
        "seisanbi_dt":         extracted_info.get("seisanbi_dt", None),
        "seisan_check_sub_no": extracted_info.get("seisan_check_sub_no", None),
        "shukkabi":            extracted_info.get("shukkabi", None),
        "shukka_basho":        extracted_info.get("shukka_basho", None),
        "hontai_kbn":          extracted_info.get("hontai_kbn", None),
        "hinmei":              extracted_info.get("hinmei", None),
        "width":               extracted_info.get("width", None),
        "height":              extracted_info.get("height", None),
        "honseki_cd":          extracted_info.get("honseki_cd", None),
        "model_cd":            extracted_info.get("model_cd", None),
        "db_bunrui_cd":        extracted_info.get("db_bunrui_cd", None),
        "worker_name":         extracted_info.get("worker_name", None),
        "process_name":        extracted_info.get("process_name", None),
        "tehai_suryo":         extracted_info.get("tehai_suryo", None),
        "kansan_mae":          extracted_info.get("kansan_mae", None),
        "kansan_ato":          extracted_info.get("kansan_ato", None),
        "shoshizai_cd":        extracted_info.get("shoshizai_cd", None),
        "betchu_cd":           extracted_info.get("betchu_cd", None),
        "total_meter":         extracted_info.get("total_meter", None),
        "fuka_kanzan":         extracted_info.get("fuka_kanzan", None),
        "kansan_ato_hjn":      extracted_info.get("kansan_ato_hjn", None),
        "kansan_mae_hjn":      extracted_info.get("kansan_mae_hjn", None),
    }

    if session is not None:
        # 外部セッション: commitは呼び出し側に任せる
        if DEBUG_MODE:
            print("[TRACE][_insert_data]session is not None")
        try:
            # raise Exception("Simulated DB failure for fallback test") # テスト用強制エラー発生用（確認後は必ず削除 or コメントアウト）

            new_record = Production(
                worker_cd=worker_cd,
                process_cd=process_cd,
                status=status,
                start_dt=start_dt,
                qr_cd=qr_cd,
                **extracted_info,
            )
            session.add(new_record)
            session.flush()  # flush成功時のみ出力
            print("\033[96mData has been inserted.\033[0m")
            logging.debug("[DB][insert] flushed via external session")
            if DEBUG_MODE:
                print("[DB][insert] flushed via external session")

        except IntegrityError as e:
            logging.error(f"IntegrityError for QR {qr_cd}, Worker {worker_cd}, Process {process_cd}: {e}")
            if DEBUG_MODE:
                print(f"IntegrityError for QR {qr_cd}, Worker {worker_cd}, Process {process_cd}: {e}")
            session.rollback()
            raise
        except SQLAlchemyError as e:
            logging.error(f"SQLAlchemyError: {e}")
            if DEBUG_MODE:
                print(f"SQLAlchemyError: {e}")
            session.rollback()
            raise
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            if DEBUG_MODE:
                print(f"Unexpected error: {e}")
            session.rollback()
            raise
        finally:
            logging.debug("Insert operation complete.")
        return True

    else:
        # 内部セッション: 自前で session_scope を開く
        if DEBUG_MODE:
            print("[TRACE][_insert_data]session is None")
        with session_scope() as s:
            try:
                new_record = Production(
                    worker_cd=worker_cd,
                    process_cd=process_cd,
                    status=status,
                    start_dt=start_dt,
                    qr_cd=qr_cd,
                    **extracted_info,
                )
                s.add(new_record)
                s.flush()
                print("\033[96mData has been inserted.\033[0m")  # flush成功時のみ出力

                if commit:
                    logging.debug("[DB][insert] internal commit enabled")
                    if DEBUG_MODE:
                        print("[DB][insert] internal commit enabled")
                    # commitはsession_scopeの__exit__に任せる
                else:
                    logging.debug("[DB][insert] internal commit disabled (note: session_scope may still commit on success)")
                    if DEBUG_MODE:
                        print("[DB][insert] internal commit disabled (note: session_scope may still commit on success)")

            except IntegrityError as e:
                logging.error(f"IntegrityError for QR {qr_cd}, Worker {worker_cd}, Process {process_cd}: {e}")
                if DEBUG_MODE:
                    print(f"IntegrityError for QR {qr_cd}, Worker {worker_cd}, Process {process_cd}: {e}")
                raise
            except SQLAlchemyError as e:
                logging.error(f"SQLAlchemyError: {e}")
                if DEBUG_MODE:
                    print(f"SQLAlchemyError: {e}")
                raise
            except Exception as e:
                logging.error(f"Unexpected error: {e}")
                if DEBUG_MODE:
                    print(f"Unexpected error: {e}")
                session.rollback()

                try:
                    log_qr_fallback(qr_cd=qr_cd, port=port, status="DB_ERROR", context="insert_data", error=e)
                except Exception as le:
                    logging.warning(f"[log_qr_fallback wrapper] failed: {le}")

                raise
            finally:
                logging.debug("Insert operation complete.")
        return True


# insert_dataに準じて2人目レコードを1人目と同一内容+worker_cdのみ置換してレコード挿入する
def insert_production_record_second(session: Session, worker_cd_first: str, worker_cd_second: str,
                                    process_cd: str, status: str, start_dt: datetime, qr_cd: str,
                                     extracted_info: dict, *, commit: bool = False, enable_exists_check: bool = False,) -> bool:
    """
    ペアモード用: 1人目と同じ内容で worker_cd だけ置換して 2人目レコードを追加する。
    """
    if not worker_cd_second:
        logging.warning("Second worker_cd is empty. Skip second insert.")
        return False
    if worker_cd_first == worker_cd_second:
        logging.warning("Second worker_cd is same as first. Skip second insert.")
        return False

    # 任意: 重複チェック
    if enable_exists_check:
        exists = (
            session.query(Production.tracking_seq)
            .filter(
                Production.worker_cd == worker_cd_second,
                Production.process_cd == process_cd,
                Production.qr_cd == qr_cd,
                Production.start_dt == start_dt,
                Production.end_dt.is_(None),
            )
            .first()
        )
        if exists:
            logging.info(f"[PAIR] Second record already exists for {worker_cd_second}. Skip.")
            return False

    try:
        insert_data(
            session=session,
            worker_cd=worker_cd_second,
            process_cd=process_cd,
            status=status,
            start_dt=start_dt,
            qr_cd=qr_cd,
            extracted_info=extracted_info,
            commit=commit,
        )
        logging.info(f"[PAIR] Inserted second record worker_cd={worker_cd_second}, qr={qr_cd}")
        return True
    except Exception as e:
        logging.error(f"[PAIR] Failed to insert second record: {e}")
        session.rollback()
        log_qr_fallback(qr_cd, port, status="DB_ERROR", context="insert_production_record_second", error=e)
        return False


# QRコードに基づく作業記録を終了し、current_state の worker_cd / process_cd を反映する
def update_previous_qr_code_end_time(prev_qr_code, port, session, *, lookback_days=2):
    """
    機能:
        QRコードに基づく未終了の作業記録（最新1件）を終了し、
        current_state[port] に格納されている worker_cd / process_cd を
        DBに反映する（status は変更しない）。

    引数:
        prev_qr_code (str): 終了対象のQRコード
        port (str): ポート名 (/dev/ttyACM0 など)
        session: SQLAlchemy セッション
        lookback_days (int): 検索対象の遡り日数（デフォルト2日）

    戻り値:
        int: 更新件数（1: 更新あり, 0: 対象なし）

    注意:
        rollback は呼び出し側で管理すること
    """

    now = datetime.now()
    range_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=lookback_days - 1)
    range_end   = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    # --- 未終了レコードを検索 ---
    record = (
        session.query(Production)
        .filter(
            Production.qr_cd == prev_qr_code,
            Production.end_dt.is_(None),
            Production.start_dt >= range_start,
            Production.start_dt < range_end,
        )
        .order_by(Production.start_dt.desc())
        .first()
    )

    if not record:
        logging.warning(
            f"[DB][work_end] no open record for qr={prev_qr_code} "
            f"within [{range_start} - {range_end}); skip close."
        )
        return 0

    # --- 終了処理 ---
    record.end_dt = now
    try:
        if record.start_dt:
            record.work_time_sec = int((now - record.start_dt).total_seconds())
        else:
            record.work_time_sec = None
    except Exception as e:
        logging.warning(f"[DB][work_end] calc work_time_sec failed for qr={prev_qr_code}: {e}")
        record.work_time_sec = None

    # --- current_state から worker_cd / process_cd を反映 ---
    state = current_state.get(port, {}) or {}
    worker_cd  = state.get("worker_cd")
    process_cd = state.get("process_cd")

    if worker_cd and record.worker_cd != worker_cd:
        logging.info(
            f"[DB][work_end] worker_cd {record.worker_cd} -> {worker_cd} for qr={prev_qr_code}"
        )
        record.worker_cd = worker_cd

    if process_cd and record.process_cd != process_cd:
        logging.info(
            f"[DB][work_end] process_cd {record.process_cd} -> {process_cd} for qr={prev_qr_code}"
        )
        record.process_cd = process_cd

    # --- DB反映 ---
    session.flush()
    logging.debug(
        f"[DB][work_end] updated qr={prev_qr_code}, "
        f"end_dt={record.end_dt}, secs={record.work_time_sec}"
    )
    return 1

# チェックナンバーの整形
def make_check_no_lcd(check_no):
    """
    LCD表示用に check_no を整形する。
    - 通常の check_no は先頭5文字を除外して 6〜11文字を表示
    - 短い値（6文字以下）はそのまま返す（スペースで潰さない）
    - None の場合はスペース6個
    """
    if not check_no:
        return " " * 6
    if len(check_no) <= 6:
        return check_no       # 間接作業ラベルなど整形せずに表示可能な値はそのまま
    if len(check_no) >= 11:
        return check_no[5:11]
#    return check_no.ljust(6)  # 余白で調整
    return


# タイマースレッド内でLCD表示部を毎秒更新する
def lcd_timer_updater(display_lcd, display_port, gen, stop_event=None):
    """デッドロック回避版のタイマー更新（ペアモード時は終了後も交互表示する）
    改修点:
      - stop_event を監視して即時停止（交互表示の原因となる旧スレッドを確実に終了）
      - 稼働カウンタは current_state[port]['start_time'] を基準に計算
        （スレッド再生成時も経過を継続表示）
    """
    last_warn_ts = 0.0
    warn_interval_sec = 5.0
    # フォールバック用のスレッド開始時刻（state側の start_time が欠損時のみ使用）
    start_ts_fallback = datetime.now()
    last_timer_str = None
    last_display_worker = None
    last_mode = None  # ソロ/ペアの切替検知用

    time.sleep(0.1)  # 初期待機

    while True:
        # ★ 即時停止判定（旧スレッドの早期終了の要）
        if stop_event is not None and stop_event.is_set():
            break

        # 世代不一致なら終了
        current_gen = lcd_timer_generations.get(display_port)
        if current_gen != gen:
            break

        try:
            # 一度だけ状態を取得してローカル変数にコピー
            with port_lock:
                state_snapshot = (current_state.get(display_port) or {}).copy()
                pair_snapshot = (pair_state.get(display_port) or {}).copy()

            if not state_snapshot:
                now_ts = time.time()
                if now_ts - last_warn_ts >= warn_interval_sec:
                    logging.warning(f"No current_state entry for port {display_port}. Waiting for initialization...")
                    last_warn_ts = now_ts
                # 停止リクエストに素早く反応するため wait を使う
                if stop_event is not None:
                    stop_event.wait(0.5)
                else:
                    time.sleep(0.5)
                continue

            # LCD取得
            lcd_obj = display_lcd or state_snapshot.get("lcd")
            if not lcd_obj:
                now_ts = time.time()
                if now_ts - last_warn_ts >= warn_interval_sec:
                    logging.warning(f"No LCD instance available for port {display_port}. Waiting for assignment...")
                    last_warn_ts = now_ts
                if stop_event is not None:
                    stop_event.wait(0.5)
                else:
                    time.sleep(0.5)
                continue

            # ===== 経過時間計算 =====
            # 作業中なら「開始時刻からの経過」を表示、作業中でなければ state 側の固定値を表示
            if state_snapshot.get("status") == STATUS_WORKING:
                st = state_snapshot.get("start_time") or start_ts_fallback
                try:
                    elapsed_seconds = max(0, int((datetime.now() - st).total_seconds()))
                except Exception:
                    elapsed_seconds = max(0, int((datetime.now() - start_ts_fallback).total_seconds()))
                mm, ss = divmod(elapsed_seconds, 60)
                timer_str = f"{mm:02}:{ss:02}"
            else:
                # 作業中でなければ現状態をそのまま
                timer_str = state_snapshot.get("timer", "00:00")

            # ===== 作業者表示（ペアなら交互表示）=====
            if pair_snapshot.get("pair_mode") and state_snapshot.get("worker2_lcd"):
                display_worker = _pick_pair_display_name({
                    "first_worker_name": state_snapshot.get("worker_lcd", ""),
                    "second_worker_name": state_snapshot.get("worker2_lcd", ""),
                })
            else:
                display_worker = state_snapshot.get("worker_lcd", "")

            # 変更時のみ描画
            if timer_str != last_timer_str or display_worker != last_display_worker or last_mode != pair_snapshot.get("pair_mode"):
                last_timer_str = timer_str
                last_display_worker = display_worker
                last_mode = pair_snapshot.get("pair_mode")

                try:
                    _lcd_update_full(
                        session=None,
                        qr_cd=state_snapshot.get("qr_cd"),
                        lcd=lcd_obj,
                        status_str=state_snapshot.get("status", STATUS_WAITING),
                        timer_str=timer_str,
                        # ★ 空文字は渡さない（Noneなら _lcd_update_full 側が current_state を優先）
                        worker_lcd_override=(display_worker or None),
                        process_lcd_override=(state_snapshot.get("process_lcd") or None),
                        # ★ check_no_lcd を正しい引数名で渡す
                        check_no_lcd_override=(state_snapshot.get("check_no_lcd") or None),
                        port=display_port,
                        is_working=(state_snapshot.get("status") == STATUS_WORKING),
                    )
                except Exception as e:
                    logging.warning(f"LCD update failed in timer (port={display_port}): {e}")

            # ★ 停止指示を即座に検知できるよう wait を使用
            if stop_event is not None:
                stop_event.wait(1.0)  # 通常1秒tick、停止時は即時break
            else:
                time.sleep(1.0)

        except Exception as e:
            logging.error(f"Error in lcd_timer_updater (port={display_port}): {e}")
            if stop_event is not None:
                stop_event.wait(0.5)
            else:
                time.sleep(0.5)


# lcd.update への引数差異を吸収するセーフアダプタ、_lcd_update_full からは常にこのアダプタ経由で呼び出す
def _safe_lcd_update(lcd, **payload):
    """
    デバイス差異を吸収して lcd.update を安全に呼び出す。
    - 既知のキー名のズレをエイリアス変換で吸収
    - **kwargs 受理ならフィルタせずに通す
    - DEBUG_MODE のとき最終的に渡す kwargs をログ
    """
    if lcd is None:
        return

    # check_no → check_no_lcd の統一（下位互換）
    if "check_no" in payload and "check_no_lcd" not in payload:
        payload["check_no_lcd"] = payload.pop("check_no")

    # ★ デフォルト補完はしない（未指定フィールドは既存表示を保持するため）
    # defaults = {...} は廃止
    merged = {k: v for k, v in payload.items() if v is not None}

    # 受理可能キーセットの取得 **kwargs を受けるなら受理制限しない
    accepted = None
    var_kw = False
    try:
        import inspect
        sig = inspect.signature(lcd.update)
        accepted = set()
        for name, p in sig.parameters.items():
            if name == "self": 
                continue
            if p.kind == p.VAR_KEYWORD:  # **kwargs
                var_kw = True
            if p.kind != p.POSITIONAL_ONLY:
                accepted.add(name)

    # 失敗時は受理可能キー不明 → エイリアス変換だけして全通し
    except Exception:
        accepted = None
        var_kw = True

    # **kwargs 受理なら元キーのままでOK
    def map_key(k: str) -> str:
        if var_kw or accepted is None:
            return k
        # 受理可能なキーにマップ
        for cand in alias_map.get(k, [k]):
            if cand in accepted:
                return cand
        # マップできない → 元キーのまま（後でフィルタに落ちる）
        return k

    # キー名のマッピングを実施
    mapped = {}
    for k, v in merged.items():
        newk = map_key(k)
        mapped[newk] = v

    # **kwargs 受理なら、そのまま
    if var_kw or accepted is None:
        final_kwargs = mapped
    else:
        # 厳格：受理可能キーのみに限定
        final_kwargs = {k: v for k, v in mapped.items() if k in accepted}

    if 'DEBUG_MODE' in globals() and DEBUG_MODE:
        try:
            logging.debug(f"[_safe_lcd_update] passing kwargs -> {final_kwargs}")
#            print(f"[_safe_lcd_update] passing kwargs -> {final_kwargs}") # 画面更新が発生する度に表示される
        except Exception:
            pass

    try:
        lcd.update(**final_kwargs)

    except TypeError as te:
        # さらに縮退（最小公約数）
        minimal_keys = ("worker_lcd", "process_lcd", "status", "timer")
        # 最小セットもマッピングしてから投げる
        minimal = {map_key(k): final_kwargs.get(map_key(k), merged.get(k, defaults.get(k, "")))
                   for k in minimal_keys}
        lcd.update(**minimal)


# 画面更新処理　作業者・工程・QR等から情報を抽出するフル処理
def _lcd_update_full(session: Optional[Session], qr_cd: Optional[str], lcd, *, status_str: str, port: str, timer_str: Optional[str] = None,
     worker_lcd_override: Optional[str] = None, process_lcd_override: Optional[str] = None, check_no_override: Optional[str] = None,
     check_no_lcd_override: Optional[str] = None, show_rework: bool = False, is_working: bool = False, show_blink: bool = False,):

    """
    timer_str / worker_lcd_override / process_lcd_override / check_no_override で上書き可。
    session が None の場合は current_state を優先して表示値を決定する。
    """

    # ★ 空文字のオーバーライドは「指定なし」と同義に扱う（current_state優先）
    if worker_lcd_override == "":
        worker_lcd_override = None
    if process_lcd_override == "":
        process_lcd_override = None
    if check_no_override == "":
        check_no_override = None
    if check_no_lcd_override == "":
        check_no_lcd_override = None

    # --- LCDが無ければ処理スキップ ---
    if lcd is None:
        logging.warning(f"LCD is None for port {port}, skipping display update")
        return

    # --- current_state の初期化 ---
    if port and port not in current_state:
        logging.warning(f"Port {port} not in current_state, initializing")
        init_current_state(port, lcd)

    state = current_state.get(port, {}) if port is not None else {}

    worker_cd = state.get("worker_cd")
    worker2_cd = state.get("worker2_cd")
    process_cd = state.get("process_cd")

    # --- 作業者LCD表示名 ---
    if worker_lcd_override is not None:
        worker_lcd_to_display = worker_lcd_override
    elif worker2_cd:
        # ペアモード表示の場合は交互表示を利用
        worker_lcd_to_display = _pick_pair_display_name(state)
    elif session is not None and worker_cd:
        try:
            worker_lcd_to_display = get_worker_lcd(session, worker_cd)
        except Exception:
            worker_lcd_to_display = state.get("worker_lcd", "")
    else:
        worker_lcd_to_display = state.get("worker_lcd", "")

    # --- 工程LCD表示名 ---
    if process_lcd_override is not None:
        process_lcd_to_display = process_lcd_override
    elif session is not None and process_cd:
        try:
            process_lcd_to_display = get_process_lcd(session, process_cd)
        except Exception:
            process_lcd_to_display = state.get("process_lcd", "")
    else:
        process_lcd_to_display = state.get("process_lcd", "")

    # --- チェックNo LCD ---
    if check_no_lcd_override is not None:
        check_no_lcd = check_no_lcd_override
    elif check_no_override is not None:
        check_no_lcd = make_check_no_lcd(check_no_override)
    elif "check_no" in state:
        check_no_lcd = make_check_no_lcd(state["check_no"])
    else:
        check_no_lcd = state.get("check_no_lcd", "      ")

    # stateに反映
    if port is not None:
        current_state[port]["check_no_lcd"] = check_no_lcd

    # --- 稼働判定 ---
    effective_is_working = (
        is_working if is_working is not None else (status_str == STATUS_WORKING)
    )

    # --- タイマ文字列 ---
    calculated_timer_str = timer_str
    if calculated_timer_str is None:
        calculated_timer_str = state.get("timer", "00:00")
        if port is not None and effective_is_working:
            st = state.get("start_time")
            if st:
                try:
                    elapsed = (datetime.now() - st).total_seconds()
                    mm, ss = divmod(int(elapsed), 60)
                    calculated_timer_str = f"{mm:02}:{ss:02}"
                except Exception:
                    calculated_timer_str = state.get("timer", "00:00")

    # --- LCD表示更新 ---
    try:
        # 表示値の最終決定 → _safe_lcd_update(...) 呼び出し
        _safe_lcd_update(
            lcd,
            status=status_str,
            timer=timer_str or "00:00",
            worker_lcd=(worker_lcd_override if worker_lcd_override is not None else current_worker_lcd),
            process_lcd=(process_lcd_override if process_lcd_override is not None else current_process_lcd),
            check_no_lcd=check_no_lcd,  # ← 統一
            show_rework=show_rework,
            show_blink=show_blink if show_blink is not None else is_working,
        )


    except Exception as e:
        logging.warning(f"_lcd_update_full failed: {e}")


# LCD用タイマー開始・各種表示キャッシュ構築
def start_lcd_timer(port, qr_cd, display_lcd, assume_new_start=False):
    """
    ポート別LCDタイマー開始。常に呼び出し時点を 00:00 として開始する。
    DB の start_dt は参照しない。

    改修点:
      - 新起動前に旧スレッドを確実に停止: 旧Event.set() → join(timeout=1.2) → del
      - 新Eventを起動前に生成し、lcd_timer_updater へ渡す（即時停止可）
      - 稼働カウンタは state['start_time'] を基準に継続
    """

    # --- current_state 初期化 ---
    if port not in current_state:
        init_current_state(port, display_lcd)

    # 現在の worker_cd / process_cd を取得
    worker_cd = current_state[port].get("worker_cd")
    process_cd = current_state[port].get("process_cd")

    # 世代IDインクリメント（旧スレッドに終了合図：保険）
    gen = lcd_timer_generations.get(port, 0) + 1
    lcd_timer_generations[port] = gen

    # LCDフォールバック
    if not display_lcd:
        display_lcd = current_state[port].get("lcd")

    # --- 旧スレッドの確実停止 ---
    prev_ev = lcd_timer_stop_events.get(port)
    prev_th = lcd_timer_threads.get(port)
    try:
        if prev_ev and not prev_ev.is_set():
            prev_ev.set()
    except Exception as e:
        logging.warning(f"[{port}] failed to set previous stop event: {e}")

    if prev_th and prev_th.is_alive():
        try:
            prev_th.join(timeout=1.2)  # 1秒tickでも確実に抜ける
            if prev_th.is_alive():
                logging.warning(f"[{port}] previous lcd timer thread did not stop within timeout.")
        except Exception:
            pass

    # 旧リソースのクリーニング（念のため）
    try:
        lcd_timer_threads.pop(port, None)
        # prev_ev は上書きされるため削除
        lcd_timer_stop_events.pop(port, None)
    except Exception:
        pass

    # --- LCDキャッシュ用データ取得 ---
    with session_scope() as session:
        try:
            worker_lcd_val = get_worker_lcd(session, worker_cd) if worker_cd else ""
        except Exception:
            worker_lcd_val = current_state[port].get("worker_lcd", "")
        try:
            process_lcd_val = get_process_lcd(session, process_cd) if process_cd else ""
        except Exception:
            process_lcd_val = current_state[port].get("process_lcd", "")

    # QR解析（チェックNo）
    try:
        if qr_cd.startswith("ID:"):
            # 間接QRの場合 → handle_indirect_qr がセットした check_no_lcd を尊重
            check_no = ""
            # 既に current_state に残っている値があれば維持
            check_no_lcd_val = current_state[port].get("check_no_lcd", "間接　")
        else:
            exinfo = extract_info(qr_cd)
            check_no = exinfo.get("check_no", "")
            check_no_lcd_val = make_check_no_lcd(check_no)
    except Exception as e:
        logging.error(f"Error extracting info from QR {qr_cd}: {e}")
        check_no = ""
        # fallback: 直前の値を保持
        check_no_lcd_val = current_state[port].get("check_no_lcd", "")

    # タイマーは常に「今」から
    start_time = datetime.now()
    current_state[port].update({
        "qr_cd": qr_cd,
        "worker_lcd": worker_lcd_val,
        "worker2_lcd": current_state[port].get("worker2_lcd", ""),
        "process_lcd": process_lcd_val,
        "check_no": check_no,
        "check_no_lcd": check_no_lcd_val,
        "start_time": start_time,
        "lcd": display_lcd,
        "timer": "00:00",
        "status": STATUS_WORKING,
    })

    # --- 新しい stop イベントを生成（★起動前に作る） ---
    ev = threading.Event()
    lcd_timer_stop_events[port] = ev

    # 周期更新スレッドを起動（★stop_event を渡す）
    th = threading.Thread(
        target=lcd_timer_updater,
        args=(display_lcd, port, gen, ev),
        name=f"LCDTimer-{port}",
        daemon=True,
    )
    lcd_timer_threads[port] = th
    th.start()


# LCD表示用タイマースレッド停止処理、ポート別処理（世代ID併用・後方互換版）
def stop_lcd_timer(port: str, display_lcd=None):
    """
    機能: 指定ポートのLCDタイマーのみ停止し、必要なら最終描画を行う。
    優先データ源: snapshot > display_lcd引数 > current_state
    """

    if port is None:
        logging.warning(
            "stop_lcd_timer called without port. No action taken. "
            "Use stop_all_lcd_timers() for global stop."
        )
        return

    # スレッド終了シグナル
    try:
        lcd_timer_generations[port] = lcd_timer_generations.get(port, 0) + 1
    except Exception:
        pass

    ev = lcd_timer_stop_events.get(port)
    th = lcd_timer_threads.get(port)

    if ev and not ev.is_set():
        try:
            ev.set()
        except Exception as e:
            logging.error(f"[{port}] failed to set stop event: {e}")

    if th and th.is_alive():
        th.join(timeout=1.0)
        if th.is_alive():
            logging.warning(f"[{port}] lcd timer thread did not stop within timeout.")

    # スナップショット or 現在状態を参照
    snapshot = lcd_last_snapshots.get(port) or {}
    cs = current_state.get(port, {}) or {}

    lcd_obj = snapshot.get("lcd") or display_lcd or cs.get("lcd")

    # 各種フィールドを決定
    worker_cd   = snapshot.get("worker_cd", cs.get("worker_cd", ""))
    process_cd  = snapshot.get("process_cd", cs.get("process_cd", ""))
    qr_cd       = snapshot.get("qr_cd", cs.get("qr_cd", ""))
    worker_lcd  = snapshot.get("worker_lcd", cs.get("worker_lcd", ""))
    process_lcd = snapshot.get("process_lcd", cs.get("process_lcd", ""))
    check_no    = snapshot.get("check_no", cs.get("check_no", ""))
    check_no_lcd = snapshot.get("check_no_lcd", cs.get("check_no_lcd", ""))

    # --- timer を終了時点で確定 ---
    final_timer_str = "00:00"
    st = snapshot.get("start_time") or cs.get("start_time")
    if st:
        try:
            elapsed = (datetime.now() - st).total_seconds()
            mm, ss = divmod(int(elapsed), 60)
            final_timer_str = f"{mm:02}:{ss:02}"
        except Exception as e:
            logging.error(f"[{port}] failed to compute final timer: {e}")

    # current_state を終了状態に更新
    try:
        if port in current_state:
            current_state[port].update({
                "status": STATUS_ENDED,
                "timer": final_timer_str,   # ← 終了時点の値を保持
                "start_time": None,
                "show_blink": False,
            })
        else:
            init_current_state(port, lcd_obj)
            current_state[port].update({
                "worker_cd": worker_cd,
                "process_cd": process_cd,
                "qr_cd": qr_cd,
                "worker_lcd": worker_lcd,
                "process_lcd": process_lcd,
                "check_no": check_no,
                "check_no_lcd": check_no_lcd,
                "status": STATUS_ENDED,
                "timer": final_timer_str,
                "start_time": None,
                "show_blink": False,
            })
    except Exception as e:
        logging.error(f"[{port}] failed to mark current_state ended: {e}")

    # LCD 最終描画
    if lcd_obj:
        try:
            _lcd_update_full(
                session=None,
                qr_cd=qr_cd,
                lcd=lcd_obj,
                status_str=STATUS_ENDED,
                show_rework=False,
                timer_str=final_timer_str,
                worker_lcd_override=worker_lcd,
                process_lcd_override=process_lcd,
                check_no_override=check_no_lcd,
                port=port,
                is_working=False,
                show_blink=False,
            )
        except Exception as e:
            logging.error(f"[{port}] _lcd_update_full error on stop: {e}")

    # 後処理（辞書から削除）
    try:
        lcd_timer_threads.pop(port, None)
        lcd_timer_stop_events.pop(port, None)
        lcd_last_snapshots.pop(port, None)
        lcd_timer_generations.pop(port, None)
    except Exception as e:
        logging.error(f"[{port}] cleanup on stop failed: {e}")


# プログラム終了時を考慮したLCD表示用タイマースレッドの全体停止処理
def stop_all_lcd_timers(display_lcd_map=None, final_render=True):
    """
    機能:            全ポートのLCDタイマーを停止する。アプリ終了時などに使用。
    display_lcd_map: 任意。{port: lcd} マップを渡すと、そのlcdで最終描画を試みる。
    final_render:    Trueなら最終描画を行う。
    """
    # 関連している可能性のある全ポートを集約
    ports = set()
    ports.update(lcd_timer_threads.keys())
    ports.update(lcd_timer_stop_events.keys())
    ports.update(lcd_last_snapshots.keys())
    ports.update(current_state.keys())

    for p in list(ports):
        ev = lcd_timer_stop_events.get(p)
        th = lcd_timer_threads.get(p)

        if ev and not ev.is_set():
            try:
                ev.set()
            except Exception as e:
                logging.error(f"[{p}] failed to set stop event: {e}")

        if th and th.is_alive():
            th.join(timeout=1.0)
            if th.is_alive():
                logging.warning(f"[{p}] lcd timer thread did not stop within timeout.")

        if final_render:
            cs = current_state.get(p, {})
            lcd_obj = (
                (display_lcd_map or {}).get(p) if display_lcd_map else cs.get("lcd")
            )

            # タイマ文字列算出
            final_timer_str = "00:00"
            st = cs.get("start_time")
            if st:
                try:
                    elapsed = max(0, (datetime.now() - st).total_seconds())
                    mm, ss = divmod(int(elapsed), 60)
                    final_timer_str = f"{mm:02}:{ss:02}"
                except Exception as e:
                    logging.error(f"[{port}] failed to compute final timer: {e}")

            if lcd_obj:
                try:
                    _lcd_update_full(
                        session=None,
                        qr_cd=cs.get("qr_cd", ""),
                        lcd=lcd_obj,
                        status_str=STATUS_ENDED,
                        show_rework=False,
                        timer_str=final_timer_str,
                        worker_lcd_override=cs.get("worker_lcd", ""),
                        process_lcd_override=cs.get("process_lcd", ""),
                        check_no_override=cs.get("check_no_lcd", ""),
                        port=p,
                        is_working=False,
                        show_blink=False, 
                    )
                except Exception as e:
                    logging.error(f"[{p}] _lcd_update_full error on stop_all: {e}")

        # クリーニング
        try:
            if p in lcd_timer_threads:
                del lcd_timer_threads[p]
            if p in lcd_timer_stop_events:
                del lcd_timer_stop_events[p]
            if p in lcd_last_snapshots:
                del lcd_last_snapshots[p]
        except Exception as e:
            logging.error(f"[{p}] cleanup on stop_all failed: {e}")


# 2秒ごとに交互表示（ペアモード時）
def _pick_pair_display_name(state: dict, now_mono: Optional[float] = None) -> Optional[str]:
    fw = state.get("worker_lcd")
    sw = state.get("worker2_lcd")
    if not sw:  # ペアでなければソロ表示
        return fw
    t = int((now_mono or time.monotonic()) // 2)
    return fw if (t % 2) == 0 else sw


# ログ設定（最初に1回だけ呼び出し）
def setup_qr_fallback_logger():
    """フォールバック専用ロガーの初期設定"""
    logger = logging.getLogger("qr_fallback")
#   logger.error("✅ qr_fallback logger initialized successfully")

    if not logger.handlers:
        handler = RotatingFileHandler(
            "qr_fallback.log",
            mode="a",
            maxBytes=1_000_000,  # 1MB
            backupCount=5,
            encoding="utf-8"
        )
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.ERROR)
    return logger


# DB書込失敗時に呼び出す関数
def log_qr_fallback(*, qr_cd, port, status, context="", error=None):
    """
    DB書込失敗時にフォールバックとして QR を監査ログに追記する

    Args:
        qr_cd (str): QRコードのフル値
        port (str): ポート名
        status (str): エラーや処理状態 (例: "DB_ERROR")
        context (str): どの関数/処理で失敗したか（任意）
        error (Exception): 発生した例外（任意）
    """
    print(f"[TRACE]log_qr_fallback] fallback are occurred.")
    
    try:
        logger = logging.getLogger("qr_fallback")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        err_str = f", err={error}" if error else ""
        logger.error(f"{ts}, {context}, {status}, port={port}, qr={qr_cd}{err_str}")
    except Exception as e:
        # 最悪ここで失敗してもプログラムは止めない
        logging.warning(f"[log_qr_fallback] failed to log fallback entry: {e}")


# 通常の終了処理が抜けた場合の安全弁
def close_open_record_fallback(qr_cd, session, port=None, process_cd=None, lcd=None):
    """
    指定QRコードの未終了レコードを強制終了する
    - 通常の終了処理が抜けた場合の安全弁
    - worker_cd が不明な場合でも qr_cd 基準で終了
    - 終了処理なのでLCDは必ず STATUS_ENDED を表示する
    """
    rows_total = 0

    if DEBUG_MODE:
        print(f"[TRACE][close_open_record_fallback] qr_cd={qr_cd}, port={port}")

    try:
        prod = (
            session.query(Production)
            .filter(Production.qr_cd == qr_cd, Production.end_dt.is_(None))
            .order_by(Production.start_dt.desc())
            .first()
        )

        if prod:
            # 本来のフォールバック成立パターン
            rows_total = update_previous_qr_code_end_time(qr_cd, port, session)
            session.commit()
            logging.info(f"[DB][close_open_record_fallback] closed qr={qr_cd}, rows={rows_total}")

            if lcd and port:
                try:
                    _lcd_update_full(
                        session=session,
                        qr_cd=qr_cd,
                        lcd=lcd,
                        status_str=STATUS_ENDED,  # 固定
                        show_rework=False,
                        port=port,
                    )
                except Exception as e:
                    logging.warning(f"[close_open_record_fallback] LCD update failed: {e}")
        else:
            # レコードなし＝異常呼び出し（開始時に呼ばれた可能性）
            ts = datetime.now().strftime("%Y%m%d%H%M%S")
            log_path = f"qr_fallback/fallback_{ts}.txt"
            try:
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(f"qr_cd={qr_cd}\nport={port}\nstate={current_state.get(port)}\n")
                logging.warning(f"[close_open_record_fallback] no open record for qr={qr_cd}, log saved {log_path}")
            except Exception as e:
                logging.warning(f"[close_open_record_fallback] failed to save debug log: {e}")

    except Exception as e:
        session.rollback()
        logging.error(f"[close_open_record_fallback] rollback due to: {e}")
        log_qr_fallback(qr_cd=qr_cd, port=port, status="DB_ERROR", context="close_open_record_fallback", error=e,)
        log_qr_fallback(qr_cd=qr_cd, port=port, status="DB_ERROR", context="handle_switch_qr_end", error=e,)

        # ★ フォールバック発生LCD通知
        if lcd:
            show_temp_error(lcd, "E08")

        raise

    return rows_total


# 1. 終了処理QRコード END*END*END (end qr) or 前回と同一QRコード (same qr) [QRコード処理]
def handle_end_or_same_qr(qr_cd, session, port, last_qr_codes, lcd, pair_state, oled_manager):
    """
    終了処理を一元化する:
    - END*END*END のQRを読み取った場合
    - 直前のQRコードと同一のQRを再度読み取った場合

    戻り値:
        True  … 終了処理を行った（新規レコードは作成しない）
        False … この関数では処理しない（後続の分岐へ）
    """
    if DEBUG_MODE:
        print(f"[TRACE][end_or_same] qr_cd={qr_cd}, prev_q={last_qr_codes.get(port)}")

    prev_q = last_qr_codes.get(port)
    if not prev_q:
        logging.info(f"[DB][work_end] no prev_q for port={port}; skip close.")
        return False

    # --- トリガー条件 ---
    if qr_cd in ("END*END*END", prev_q):
        if DEBUG_MODE:
            print(f"[TRACE][end_or_same] match -> processing")
    else:
        if DEBUG_MODE:
            print(f"[TRACE][end_or_same] not a match")
        return False

    state = pair_state[port]

    # --- LCDタイマー停止と状態更新 ---
    stop_lcd_timer(port, display_lcd=lcd)
    current_state[port]["status"] = STATUS_ENDED

    if DEBUG_MODE:
        print(f"[DB][work_end] stop_lcd_timer "
              f"{current_state[port].get('worker_cd')}, {current_state[port].get('worker2_cd')}")

    if oled_manager and hasattr(oled_manager, "stop_worker_swap"):
        oled_manager.stop_worker_swap()

    # --- DB上の作業終了処理 ---
    try:
        rows1 = rows2 = 0
        if pair_state.get(port, {}).get("pair_mode"):
            # ペア作業モード: 両方の作業者を終了
            try:
                if current_state[port].get("worker_cd"):
                    rows1 = update_previous_qr_code_end_time(prev_q, port, session)
                if current_state[port].get("worker2_cd"):
                    rows2 = update_previous_qr_code_end_time(prev_q, port, session)
                session.commit()
                logging.info(f"[DB][work_end][pair] commit OK port={port}, rows1={rows1}, rows2={rows2}")
            except Exception as e:
                session.rollback()
                log_qr_fallback(qr_cd, port, status="DB_ERROR", context="handle_end_or_same_qr_pair", error=e)
                logging.error(f"[DB][work_end][pair] rollback: {e}")
                raise

        else:
            # ソロ作業モード
            if current_state[port].get("worker_cd"):
                rows1 = update_previous_qr_code_end_time(prev_q, port, session)
                if rows1 == 0:
                    # fallback: 終了できなかった場合に補完処理
                    rows_fallback = close_open_record_fallback(session, port, prev_q, lcd)
                    logging.info(f"[DB][work_end][solo][fallback] port={port}, rows={rows_fallback}")

                session.commit()
                logging.info(f"[DB][work_end][solo] commit OK port={port}, rows={rows1}")
            else:
                logging.warning(f"[DB][work_end][solo] worker_cd not found for port={port}")

    except Exception as e:
        session.rollback()
        log_qr_fallback(qr_cd, port, status="DB_ERROR", context="handle_end_or_same_qr", error=e)
        logging.error(f"[DB][work_end] commit failed -> rollback: {e}")
        raise

    # --- LCD終了表示更新 ---
    try:
        _lcd_update_full(
            session=session,
            qr_cd=qr_cd,
            lcd=lcd,
            status_str=STATUS_ENDED,
            show_rework=False,
            port=port,
        )
    except Exception as e:
        logging.warning(f"[DB][work_end] lcd update failed: {e}")

    return True


# 2. ステータスQRコード(rework etc..)     [QRコード処理]
def handle_status_qr(qr_cd, session, port, last_qr_codes, lcd, pair_state):
    """
    ステータスQRを処理する
    条件:
    - QRコードが status_mapping に一致した場合
    動作:
    - 作業中: 現在の open record に status を反映
    - 作業終了済み: open record が無ければ rework_status に保存し、次回の insert で反映
    - LCDには「* 手直し」などを一時表示（ワンショット）
    動かさないこと:
    - LCDはユーザー通知で一時的に書き換えるが、current_state["status"] 自体は変更しない
    """

    try:
        # マッピングされた日本語ラベルを取得（なければそのまま）
        status_label = status_mapping.get(qr_cd, qr_cd)

        # 最新の未終了レコードを取得
        latest_record = (
            session.query(Production)
            .filter_by(
                worker_cd=current_state[port].get("worker_cd"),
                process_cd=current_state[port].get("process_cd"),
                end_dt=None,
            )
            .order_by(Production.start_dt.desc())
            .first()
        )

        # 作業中レコードに直接反映
        if latest_record and current_state[port].get("status") == STATUS_WORKING:
            latest_record.status = status_label
            session.commit()
            current_state[port]["rework_status"] = None
            logging.info(f"[{port}] Status updated to {status_label} (direct)")

        # 作業終了済み or open record 不在 → rework_status に保存
        else:
            current_state[port]["rework_status"] = status_label
            logging.info(f"[{port}] Pending status set to {status_label}")

        # --- LCD 表示更新 ---
        _lcd_update_full(
            session=session,
            qr_cd=current_state[port].get("qr_cd") or qr_cd,
            lcd=lcd,
            status_str=f"* {status_label}",
            show_rework=True,
            port=port,
            is_working=(current_state.get(port, {}).get("status") == STATUS_WORKING),
        )

        # 処理分岐確認用
        if DEBUG_MODE:
            print(f"[TRACE][handle_status_qr] {status_label}")

        return True

    except Exception as e:
        session.rollback()
        log_qr_fallback(qr_cd, port, status="DB_ERROR", context="handle_status_qr", error=e)
        logging.error(f"[{port}] handle_status_qr failed: {e}")
        return False


# 3. 工程QRコード (Pxxxx)                 [QRコード処理]
def handle_process_qr(qr_cd, session, port, last_qr_codes, lcd, pair_state, oled_manager=None):
    """
    工程コードQRを処理する
    条件:
    - QRコードが "Pxxxx" の形式であった場合
    動作:
    - current_state["process_cd"] と LCD 表示用 process_lcd を更新
    - LCDを即時更新（status は変更しない）
    動かさないこと:
    - 加工指示書QR専用の履歴管理であるlast_qr_codes は更新しない
    """

    if port not in current_state:
        init_current_state(port, lcd)

    # 新しい工程コードをセット
    current_state[port]["process_cd"] = qr_cd
    current_state[port]["qr_cd"] = qr_cd

    # DBから工程名を解決
    try:
        process_lcd_val = get_process_lcd(session, qr_cd)
        if not process_lcd_val:
            process_lcd_val = "未登録"
    except Exception as e:
        logging.error(f"[{port}] get_process_lcd failed for {qr_cd}: {e}")
        process_lcd_val = "取得失敗"

    current_state[port]["process_lcd"] = process_lcd_val

    # --- 共通表示データを変数に固定 ---
    display_worker   = current_state[port].get("worker_lcd", "")
    display_process  = process_lcd_val
    display_status   = current_state[port].get("status") or STATUS_WAITING
    display_checkno  = current_state[port].get("check_no_lcd", "")
    display_timer    = current_state[port].get("timer", "00:00")

    # --- LCD 更新処理 ---
    try:
        _lcd_update_full(
            session=session,
            qr_cd=current_state[port].get("qr_cd") or qr_cd,
            lcd=lcd,
            status_str=display_status,
            worker_lcd_override=display_worker,
            process_lcd_override=display_process,
            check_no_override=display_checkno,
            port=port,
            is_working=(display_status == STATUS_WORKING),
        )
        logging.info(
            f"[process_qr] Updated port={port}, process_cd={qr_cd}, process_lcd={display_process}"
        )

        # --- OLED 更新処理 ---
        if oled_manager:
            oled_manager.update(
                qr_cd=current_state[port].get("qr_cd") or qr_cd,
                worker_lcd=display_worker,
                process_lcd=display_process,
                check_no=display_checkno,
                status=display_status,
                timer=display_timer,
                show_rework=False,
                show_blink=(display_status == STATUS_WORKING),
            )

    except Exception as e:
        logging.warning(f"[process_qr] lcd/oled update failed: {e}")


# 4. 作業者コード (WCDxxxxxx)             [QRコード処理]
def handle_worker_qr(qr_cd, session, port, last_qr_codes, lcd, pair_state, oled_manager):
    """
    作業者コードQRを処理する
    条件:
    - QRコードが "WCDxxxxxx" の形式であった場合
    動作:
    - current_state["worker_cd"], worker2_cd を更新（ペアモード管理を含む）
    - DBから作業者名を解決し LCD を更新
    - ペアモードなら "+" 表示する。またはカウント進行中は名称の交互表示
    動かさないこと:
    - last_qr_codes を更新しない
    - status を変更しない
    """

    m = re.match(r"^WCD(\d+)$", qr_cd)
    if not m:
        return

    worker_cd = m.group(1)
    now = datetime.now()
    PAIR_WINDOW_SECONDS = 5

    # --- ペア状態の初期化 ---
    state = pair_state.setdefault(port, {})
    if "recent_workers" not in state:
        init_pair_state_for_port(port)
        state = pair_state[port]

    # --- QR履歴の管理（last_qr_codes には触らない） ---
    if state.get("last_worker_ts") and (now - state["last_worker_ts"]).total_seconds() > PAIR_WINDOW_SECONDS:
        state["recent_workers"] = []  # 5秒以上経過 → リセット

    state["recent_workers"].append(worker_cd)
    state["recent_workers"] = state["recent_workers"][-3:]
    state["last_worker_ts"] = now

    # --- モード判定と current_state 更新 ---
    if not state["pair_mode"]:
        if len(state["recent_workers"]) == 1:
            # ソロ継続
            state["pair_mode"] = False
            current_state[port]["worker_cd"] = state["recent_workers"][0]
            current_state[port]["worker2_cd"] = None
            logging.info(f"[{port}][pair] solo start/switch -> {worker_cd}")

        elif len(state["recent_workers"]) == 2:
            # ペア昇格
            state["pair_mode"] = True
            current_state[port]["worker_cd"] = state["recent_workers"][0]
            current_state[port]["worker2_cd"] = state["recent_workers"][1]
            logging.info(f"[{port}][pair] pair ON: {current_state[port]['worker_cd']} + {current_state[port]['worker2_cd']}")
            try:
                if oled_manager and hasattr(oled_manager, "play_pair_animation"):
                    oled_manager.play_pair_animation()
            except Exception as e:
                logging.warning(f"[{port}] play_pair_animation failed: {e}")

        elif len(state["recent_workers"]) == 3:
            # ペア昇格（1人目＋3人目）
            state["pair_mode"] = True
            current_state[port]["worker_cd"] = state["recent_workers"][0]
            current_state[port]["worker2_cd"] = state["recent_workers"][2]
            logging.info(f"[{port}][pair] pair switch: {current_state[port]['worker_cd']} + {current_state[port]['worker2_cd']}")

    else:
        # ペアモード時
        if len(state["recent_workers"]) == 1:
            # ソロ降格
            state["pair_mode"] = False
            current_state[port]["worker_cd"] = state["recent_workers"][0]
            current_state[port]["worker2_cd"] = None
            logging.info(f"[{port}][pair] pair -> solo: {worker_cd}")

        elif len(state["recent_workers"]) == 2:
            # ペア継続
            state["pair_mode"] = True
            current_state[port]["worker_cd"] = state["recent_workers"][0]
            current_state[port]["worker2_cd"] = state["recent_workers"][1]
            logging.info(f"[{port}][pair] continue pair: {current_state[port]['worker_cd']} + {current_state[port]['worker2_cd']}")

    # --- LCD表示用ラベルをDBから取得 ---
    try:
        current_state[port]["worker_lcd"] = get_worker_lcd(session, current_state[port]["worker_cd"])
    except Exception:
        current_state[port]["worker_lcd"] = "未登録"

    if current_state[port].get("worker2_cd"):
        try:
            current_state[port]["worker2_lcd"] = get_worker_lcd(session, current_state[port]["worker2_cd"])
            if DEBUG_MODE:
                print(f"[debug][pair]{current_state[port].get('worker_lcd')}, {current_state[port].get('worker2_lcd')}")
        except Exception:
            current_state[port]["worker2_lcd"] = "未登録"
    else:
        current_state[port]["worker2_lcd"] = ""

    # --- 表示データ構築 ---
    if state.get("pair_mode") and current_state[port].get("worker2_cd"):
        display_worker = f"{current_state[port]['worker_lcd']}+"
    else:
        display_worker = current_state[port]["worker_lcd"]

    display_status  = current_state[port].get("status") or STATUS_WAITING
    display_process = current_state[port].get("process_lcd", "")
    display_checkno = current_state[port].get("check_no_lcd", "")
    display_timer   = current_state[port].get("timer", "00:00")

    # --- LCD更新 ---
    try:
        _lcd_update_full(
            session=session,
            qr_cd=current_state[port].get("qr_cd"),
            lcd=lcd,
            status_str=display_status,
            worker_lcd_override=display_worker,
            process_lcd_override=display_process,
            check_no_override=display_checkno,
            port=port,
            is_working=(display_status == STATUS_WORKING),
        )

        if oled_manager:
            oled_manager.update(
                qr_cd=current_state[port].get("qr_cd"),
                worker_lcd=display_worker,
                process_lcd=display_process,
                check_no=display_checkno,
                status=display_status,
                timer=display_timer,
                show_rework=False,
                show_blink=(display_status == STATUS_WORKING),
            )

    except Exception as e:
        logging.warning(f"[{port}] LCD update after worker QR failed: {e}")

# 5. 間接作業 QR (ID:xxx-yyyy)の場合      [QRコード処理]
def handle_indirect_qr(qr_cd, session, port, last_qr_codes, config, section, lcd, pair_state, oled_manager):
    """
    間接作業QRを処理する
    条件:
    - QRコードが "ID:xxx-yyyy" 形式で始まる場合
    動作:
    - rework_status は無視
    - 間接作業マスタを参照し、存在すれば status=RECORD_NAME / LCD=LCD_LABEL
    - 存在しなければ status="間接作業" / LCD="間接　"
    - factory_code は QRコードから取得、無ければ config.ini の factory_cd を利用
    """

    last_qr_codes[port] = qr_cd

    if not qr_cd.startswith("ID:"):
        return False

    try:
        # --- QRコード分解 ---
        try:
            parts = qr_cd.split(":")[1].split("-")
            indirect_code = parts[0]  # A01など
            factory_code = parts[1] if len(parts) > 1 else None
        except Exception:
            indirect_code, factory_code = None, None

        # --- 間接作業マスタ参照 ---
        master_row = None
        if indirect_code:
            master_row = (
                session.query(IndirectWorkMaster)
                .filter(IndirectWorkMaster.work_code == indirect_code)
                .first()
            )

        # OracleDBでchar型から値を取得すると余計な余白が生成されるため、型整形
        if master_row:
            status_val = master_row.record_name
            # DB値を整形：右端スペース削除 → 最大6文字 → 6文字固定
            raw_label = (master_row.lcd_label or "").rstrip()
            lcd_label = raw_label[:6].ljust(6, " ")
        else:
            status_val = "間接作業"
            lcd_label  = "間接　".ljust(6, " ")


        # --- factory_code 補完 ---
        if not factory_code:
            if isinstance(config, dict):
                factory_code = config.get(section, {}).get("factory_cd")
            else:
                factory_code = config.get(section, "factory_cd", fallback=None)

        extracted_info = {
            "seisakusho_fuka_cd": factory_code,
            "seisakusho_mae_cd": factory_code,
            "seisakusho_ato_cd": factory_code,
        }

        # --- worker_cd / process_cd 補完 ---
        if not current_state[port].get("worker_cd"):
            worker_cd = (config.get(section, {}).get("worker_cd", "000000")
                         if isinstance(config, dict)
                         else config.get(section, "worker_cd", fallback="000000"))
            current_state[port]["worker_cd"] = worker_cd

        if not current_state[port].get("process_cd"):
            process_cd = (config.get(section, {}).get("process_cd", "PX000")
                          if isinstance(config, dict)
                          else config.get(section, "process_cd", fallback="PX000"))
            current_state[port]["process_cd"] = process_cd

        # --- DB 挿入 ---

        # 1人目
        insert_data(
            session,
            current_state[port]["worker_cd"],
            current_state[port]["process_cd"],
            status_val,
            datetime.now(),
            qr_cd,
            extracted_info,
            commit=False,
        )

        # 2人目（ペアモード時のみ）
        if pair_state.get(port, {}).get("pair_mode") and current_state[port].get("worker2_cd"):
            insert_production_record_second(
                session,
                worker_cd_first=current_state[port]["worker_cd"],
                worker_cd_second=current_state[port]["worker2_cd"],
                process_cd=current_state[port]["process_cd"],
                status=status_val,
                start_dt=datetime.now(),
                qr_cd=qr_cd,
                extracted_info=extracted_info,
                commit=False,
            )

        session.commit()
        logging.info(f"[INDIRECT] inserted indirect work {status_val} from {qr_cd}")

        # --- LCD 表示更新 ---
        if DEBUG_MODE:
            print(f"[TRACE][indirect_qr] lcd_label={lcd_label}")
        _lcd_update_full(
            session=session,
            qr_cd=qr_cd,
            lcd=lcd,
            status_str=STATUS_WORKING,        # 状態は常に「作業中」で固定
            check_no_lcd_override=lcd_label,  # 間接作業マスタのラベルをチェックNo欄に表示
            show_rework=False,
            port=port,
        )

        start_lcd_timer(port, qr_cd, lcd)

        return True

    except Exception as e:
        logging.warning(f"[INDIRECT] failed to handle {qr_cd}: {e}")

        # 異常系でも status 固定
        insert_data(
            session,
            current_state[port].get("worker_cd"),
            current_state[port].get("process_cd"),
            "間接作業",
            datetime.now(),
            qr_cd,
            {},
            commit=False,
        )
        try:
            session.commit()
        except Exception:
            session.rollback()
            log_qr_fallback(qr_cd, port, status="DB_ERROR", context="handle_indirect_qr", error=e)
            raise

        return True


# 6. 新しいQRコードが来た場合 (switch qr) [QRコード処理]
def handle_switch_qr(qr_cd, session, port, last_qr_codes, lcd, pair_state, oled_manager):
    """
    前回QRとは異なる新しい加工指示書QRを処理する
    条件:
    - extract_info に成功し、かつ last_qr_codes に前回QRがある場合
    動作:
    - 前回レコードを終了処理
    - 新しいレコードを insert
    - LCD更新とタイマー開始
    """
    prev_q = last_qr_codes.get(port)
    if not prev_q:
        return

    state = pair_state[port]

    # --- [1] 前回QR終了処理 ---
    stop_lcd_timer(port=port, display_lcd=lcd)
    if DEBUG_MODE:
        print(f"[DB][work_end][switch-QR] stop_lcd_timer "
              f"{current_state[port].get('worker_cd')}, {current_state[port].get('worker2_cd')}")

    if oled_manager and hasattr(oled_manager, "stop_worker_swap"):
        oled_manager.stop_worker_swap()

    try:
        rows1 = rows2 = 0
        if state.get("pair_mode"):
            # ペア作業モード: 両方の作業者を終了
            if current_state[port].get("worker_cd"):
                rows1 = update_previous_qr_code_end_time(prev_q, port, session)

            if current_state[port].get("worker2_cd"):
                rows2 = update_previous_qr_code_end_time(prev_q, port, session)

            if (rows1 + rows2) == 0:
                # fallback: どちらも終了できなかった場合に補完処理
                rows_fallback = close_open_record_fallback(session, port, prev_q, lcd)
                logging.info(f"[DB][work_end][switch-QR][pair][fallback] port={port}, rows={rows_fallback}")

            session.commit()
            logging.info(f"[DB][work_end][switch-QR][pair] commit OK port={port}, rows1={rows1}, rows2={rows2}")

        else:
            # ソロ作業モード
            if current_state[port].get("worker_cd"):
                rows1 = update_previous_qr_code_end_time(prev_q, port, session)
                if rows1 == 0:
                    # fallback: 終了できなかった場合に補完処理
                    rows_fallback = close_open_record_fallback(session, port, prev_q, lcd)
                    logging.info(f"[DB][work_end][switch-QR][solo][fallback] port={port}, rows={rows_fallback}")

                session.commit()
                logging.info(f"[DB][work_end][switch-QR][solo] commit OK port={port}, rows={rows1}")
            else:
                logging.warning(f"[DB][work_end][switch-QR][solo] worker_cd not found for port={port}")

    except Exception as e:
        session.rollback()
        log_qr_fallback(qr_cd, port, status="DB_ERROR", context="handle_switch_qr_end", error=e)
        logging.error(f"[DB][work_end][switch-QR] commit failed -> rollback: {e}")
        raise

    # --- [2] 新しいQR開始準備 ---
    try:
        extracted_info = extract_info(qr_cd)
        if extracted_info:
            # rework_status があれば反映して消費
            status_val = current_state[port].get("rework_status") or "operation"
            if current_state[port].get("rework_status"):
                current_state[port]["rework_status"] = None

            # 1人目
            insert_data(
                session,
                current_state[port]["worker_cd"],
                current_state[port]["process_cd"],
                status_val,
                datetime.now(),
                qr_cd,
                extracted_info,
                commit=False,
            )

            # 2人目（ペアモード時のみ）
            if pair_state.get(port, {}).get("pair_mode") and current_state[port].get("worker2_cd"):
                insert_production_record_second(
                    session,
                    worker_cd_first=current_state[port]["worker_cd"],
                    worker_cd_second=current_state[port]["worker2_cd"],
                    process_cd=current_state[port]["process_cd"],
                    status=status_val,
                    start_dt=datetime.now(),
                    qr_cd=qr_cd,
                    extracted_info=extracted_info,
                    commit=False,
                )

            session.commit()
            logging.info(f"[DB][switch-QR] inserted new record for {qr_cd}")

    except Exception as e:
        session.rollback()
        log_qr_fallback(qr_cd, port, status="DB_ERROR", context="handle_switch_qr_start", error=e)
        logging.error(f"[DB][switch-QR] insert new record failed -> rollback: {e}")
        raise

    # LCD更新
    try:
        _lcd_update_full(
            session=session,
            qr_cd=qr_cd,
            lcd=lcd,
            status_str=STATUS_WORKING,
            show_rework=False,
            port=port,
        )
    except Exception as e:
        logging.warning(f"[DB][switch-QR] lcd update failed: {e}")

    # タイマー開始
    start_lcd_timer(port, qr_cd, lcd)

    # prev_q 更新
    last_qr_codes[port] = qr_cd


# 7. 初回QRの標準加工指示書QRコード処理   [QRコード処理]
def handle_standard_qr(qr_cd, session, port, last_qr_codes, lcd, pair_state):
    """
    標準の加工指示書QRを処理する
    条件:
    - extract_info に成功した場合

    動作:
    - current_state[port]["rework_status"] があれば優先的に status として使用し、ワンショットで消費
    - なければ "operation" を status として使用
    - 新しいレコードを insert し、last_qr_codes を更新
    - LCDに「作業中」表示を反映
    - タイマー起動
    """

    extracted_info = extract_info(qr_cd)
    start_dt = datetime.now() 

    # 抽出失敗（対応外/間違いフォーマット）なら無視・警告のみ
    if not extracted_info:
        print("未対応QR/フォーマット異常なQR: 無視・警告のみ")
        if lcd:
            _lcd_update_full(
                session=session,
                qr_cd=qr_cd,
                lcd=lcd,
                status_str="未対応QR",
                show_rework=False,
                port=port,
            )
        stop_lcd_timer(port=port, display_lcd=lcd)
        return

    # --- rework_status を優先使用（ワンショット消費） ---
    status_val = current_state[port].pop("rework_status", None) or "operation"

    logging.info(
        f"[standard_qr] Inserting data: worker_cd={current_state[port].get('worker_cd')}, "
        f"process_cd={current_state[port].get('process_cd')}, qr_cd={qr_cd}, status={status_val}"
    )

    if DEBUG_MODE:
        print(f"[TRACE][handle_standard_qr] rework_status:{current_state[port].get('rework_status')}")

    # DB挿入
    insert_data(
        session,
        current_state[port]["worker_cd"],
        current_state[port]["process_cd"],
        status_val,
        start_dt,
        qr_cd,
        extracted_info,
        commit=False,
    )

    if pair_state.get(port, {}).get("pair_mode") and current_state[port].get("worker2_cd"):
        insert_production_record_second(
            session=session,
            worker_cd_first=current_state[port]["worker_cd"],
            worker_cd_second=current_state[port]["worker2_cd"],
            process_cd=current_state[port]["process_cd"],
            status=status_val,
            start_dt=start_dt,
            qr_cd=qr_cd,
            extracted_info=extracted_info,
            commit=False,
        )

    try:
        session.commit()
        logging.info(f"[DB][insert] commit OK -> QR={qr_cd}, status={status_val}")

    except Exception as e:
        session.rollback()
        log_qr_fallback(qr_cd, port, status="DB_ERROR", context="handle_standerd_qr", error=e)
        logging.error(f"[DB][insert] commit failed -> rollback: {e}")
        raise

    # LCD更新
    try:
        _lcd_update_full(
            session=session,
            qr_cd=qr_cd,
            lcd=lcd,
            status_str=STATUS_WORKING,
            show_rework=False,
            port=port,
        )
    except Exception as e:
        logging.warning(f"[standard_qr] lcd update failed: {e}")

    # タイマー起動
    start_lcd_timer(port, qr_cd, lcd)

    # QR履歴を更新
    last_qr_codes[port] = qr_cd

    return True


# 8. 未対応QRコードの場合の安全弁         [QRコード処理]
def handle_error_qr(qr_cd, session, port, last_qr_codes, lcd, pair_state):
    """
    未対応QRを記録する（最後の安全弁）
    条件:
    - どの既存のQR形式にもマッチしなかった場合
    動作:
    - qr_cd は 400byte に収まるように事前整形
    - status は "E05:QR error" 固定
    - LCD は固定表示＋一時エラー表示を両方行う
    動かさないこと:
    - rework_status を利用しない
    - 通常の QR フローには影響しない
    """
    def safe_truncate_sjis(s: str, max_bytes: int) -> str:
        return s.encode("shift_jis", errors="ignore")[:max_bytes].decode("shift_jis", errors="ignore")

    qr_cd_raw = qr_cd
    qr_cd = safe_truncate_sjis(qr_cd, 400)
    status_val = "E05:QR error"

    # 1. フォールバック用ログ保存
    try:
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        log_path = f"qr_fallback/error{ts}.txt"
        with open(log_path, "wb") as f:
            if isinstance(qr_cd_raw, str):
                f.write(qr_cd_raw.encode("shift_jis", errors="replace"))
            else:
                f.write(qr_cd_raw)
    except Exception as e:
        logging.warning(f"[handle_error_qr] failed to save raw log: {e}")

    # 2. DB記録 & LCD更新
    try:
        prod = Production(
            worker_cd=current_state[port].get("worker_cd"),
            process_cd=current_state[port].get("process_cd"),
            qr_cd=qr_cd,
            status=status_val,
            start_dt=datetime.now(),
            extracted_info={},  # 展開なし
        )
        session.add(prod)
        session.flush()

        # LCD 固定表示
        _lcd_update_full(
            session=session,
            worker_cd=current_state[port].get("worker_cd"),
            process_cd=current_state[port].get("process_cd"),
            qr_cd=qr_cd,
            lcd=lcd,
            status_str=status_val,
            show_rework=False,
            port=port,
        )

        # 一時エラー通知
        if lcd:
            show_temp_error(lcd, "E05")

    except Exception as e:
        logging.error(f"[handle_error_qr] failed: {e}")
        session.rollback()
        log_qr_fallback(qr_cd=qr_cd, port=port, status="DB_ERROR", context="handle_error_qr", error=e)

        # ★ フォールバック発生LCD通知
        if lcd:
            show_temp_error(lcd, "E08")

        raise


# シリアルポートから受け取ったデータを処理する ----------------------------------------------------------------------------------------------
def process_data(data, line, session, port, last_qr_codes, config, section, lcd, pair_state, oled_manager=None):
    """
    機能:   データの処理を行い、必要に応じてQRコードを解析し、データベースに挿入します。
    引数:   data (dict): 処理するデータを格納した辞書。
            line (str): 取り込んだ行データ。
            session (Session): SQLAlchemyのセッションオブジェクト。
            port (str): シリアルポートの識別子。
            last_qr_codes (list): 最後に処理したQRコードのリスト。
            config (dict): 設定情報を格納した辞書。
            section (str): 処理を行うセクションの識別子。
    戻り値: bool: データ処理が成功した場合はTrue、それ以外はFalse。
    例外:   ValueError: 無効なデータが渡された場合。
            SQLAlchemyError: データベース操作中にエラーが発生した場合。
    """

    # --- ポートの初期化処理 ---
    if port not in current_state:
        init_current_state(port, lcd)

    if port not in pair_state:
        init_pair_state_for_port(port)

#    state = pair_state.get(port)
#    if state is None:
#        return

    qr_cd = line

    # Configからworker_cd, process_cdを取得
    if isinstance(config, dict):
        section_config = config.get(section, {}) or {}
        process_cd = current_state[port].get("process_cd") or section_config.get("process_cd", "default_process_cd")
        worker_cd = current_state[port].get("worker_cd") or section_config.get("worker_cd", "default_worker_cd")
    else:
        process_cd = current_state[port].get("process_cd") or config.get(section, "process_cd", fallback="default_process_cd")
        worker_cd = current_state[port].get("worker_cd") or config.get(section, "worker_cd", fallback="default_worker_cd")

    if DEBUG_MODE:
        print(f"[debug] worker_cd={worker_cd}, process_cd={process_cd}, port={port}")

    # ----- QRタイプごとの分岐 -----------------------------------------------------------------------

    # 1. 終了処理QRコード END*END*END (end qr) or 前回と同一QRコード (same qr) [QRコード処理]
    if handle_end_or_same_qr(qr_cd, session, port, last_qr_codes, lcd, pair_state, oled_manager):
        return True

    # 2. ステータスQRコード(rework etc..)     [QRコード処理]
    elif qr_cd in status_mapping:
        return handle_status_qr(qr_cd, session, port, last_qr_codes, lcd, pair_state)

    # 3. 工程QRコード (Pxxxx)                 [QRコード処理]
    elif re.match(r"^P[A-Z0-9]{4}$", qr_cd):
        return handle_process_qr(qr_cd, session, port, last_qr_codes, lcd, pair_state, oled_manager)

    # 4. 作業者コード (WCDxxxxxx)             [QRコード処理]
    elif re.match(r"^WCD(\d+)$", qr_cd):
        return handle_worker_qr(qr_cd, session, port, last_qr_codes, lcd, pair_state, oled_manager)

    # 5. 間接作業 QR (ID:xxx-yyyy)の場合      [QRコード処理]
    elif qr_cd.startswith("ID:"):
        return handle_indirect_qr(qr_cd, session, port, last_qr_codes, config, section, lcd, pair_state, oled_manager)

    # 6.7. 標準加工指示書QR（extract_info 成功）
    elif extract_info(qr_cd):
        if last_qr_codes.get(port):
    # 6. 新しいQRコードが来た場合 (switch qr) [QRコード処理]
            return handle_switch_qr(qr_cd, session, port, last_qr_codes, lcd, pair_state, oled_manager)
        else:
    # 7. 初回QRの標準加工指示書QRコード処理   [QRコード処理]
            return handle_standard_qr(qr_cd, session, port, last_qr_codes, lcd, pair_state)

    # 8. 未対応QRコードの場合の安全弁         [QRコード処理]
    else:
        logging.warning(f"[process_data] Unknown QR format: {qr_cd}")
        return handle_error_qr(qr_cd, session, port, last_qr_codes, lcd, pair_state)

# シグナルハンドラーを設定
def signal_handler(sig, frame):
    global threads  # グローバルスコープのthreadsを使用
    print("Stopping...")
    logging.info("Received termination signal. Stopping all threads.")
    stop_event.set()  # 停止イベントをセット

    # スレッドリストを安全に操作
    with threads_lock:
        for thread in threads:
            if thread.is_alive():
                thread.join()  # スレッドが終了するのを待つ

    print("All threads stopped. Exiting program.")
    logging.info("Main program stopped.")
    sys.exit(0)


# シグナルハンドラ設定関数 (OSごとに異なるシグナルを設定)
def setup_signal_handlers():
    signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
    if hasattr(signal, "SIGTERM"):  # Linuxでのみ使用可能
        signal.signal(signal.SIGTERM, signal_handler)  # kill コマンド


# ログ設定関数
def configure_logging():
    """
    機能:   ファイルにローリングでログを保存する。
            ロガーはアプリケーションレベルのエラーと、SQLAlchemyエンジンの処理ログを記録。
            ログレベルは logging.DEBUG logging.INFO logging.WARNING logging.ERROR logging.CRITICAL
    """

    if not logger.handlers:
        # ログのフォーマッターを作成
        log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

        # ログファイルの設定
        log_file = "prodtrac_error.log"
        log_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,  # 5MBでローテーション
            backupCount=5,  # 最大5世代分のログファイルを保持
            encoding="utf-8",  # UTF-8形式でログを出力
        )
        log_handler.setFormatter(log_formatter)

        # ロガーにファイルハンドラーを追加
        logger.addHandler(log_handler)
        logger.setLevel(logging.INFO)  # プログラムのエラーログレベルを設定

        # SQLAlchemyのロギング設定
        sqlalchemy_logger = logging.getLogger("sqlalchemy.engine")
        sqlalchemy_logger.setLevel(logging.WARNING)  # SQLAlchemyのログレベルを設定

        # StreamHandler（標準出力へのログ）を削除
        for handler in sqlalchemy_logger.handlers[:]:
            if isinstance(handler, logging.StreamHandler):
                sqlalchemy_logger.removeHandler(handler)

        # SQLAlchemyのログをファイルに記録する設定（同じファイルハンドラを使う）
        sqlalchemy_logger.addHandler(log_handler)


# LCD画面に継続不能なエラーを出力する
def show_fatal_error(oled_manager, code, hold_time=30):
    """
    致命的エラーをOLEDに表示して固定する。
    hold_time: プログラム終了前に最低限表示を保持する秒数
    """
    messages = {
        "E01": ["E01 DB接続エラー", "管理者へ連絡"],
        "E02": ["E02 設定異常", "管理者へ連絡"],
        "E03": ["E03 DB書込異常", "管理者へ連絡"],
        "E04": ["E04 DB切断発生", "再起動して下さい"],
        "E07": ["E07 リーダー未検出", "接続し再起動して下さい"],
    }
    lines = messages.get(code, ["不明なエラー", "管理者へ連絡"])

    try:
        if oled_manager:
            oled_manager.show_error(lines)  # duration指定なし = 固定表示
        else:
            print("[ERROR]", " / ".join(lines))
    except Exception as e:
        print(f"[ERROR fallback] {lines} ({e})")

    # プログラム終了前に一定時間保持
    try:
        time.sleep(hold_time)
    except KeyboardInterrupt:
        pass


# LCD画面に復帰可能なエラーを出力する
def show_temp_error(oled_manager, code):
    """
    E05～E10のように自動復帰可能なエラーをOLEDに出す（5秒表示）
    """
    messages = {
        "E05": ["E05 QRコード異常", " "],
        "E06": ["E06 DB書込異常", "新規で記録します"],
        "E08": ["E08フォールバック", "再読み込み下さい"],
        "E10": ["E10 予期せぬ異常", "管理者へ連絡"],
    }
    lines = messages.get(code, ["E?? 不明", "管理者へ連絡"])
    if oled_manager:
        oled_manager.show_error(lines, duration=5)
    else:
        print("[ERROR TEMP]", " / ".join(lines))


# メインプログラムはサブルーチンや引数を一通り読み込んだ最後に置く
def main_program():
    global lcd
    global pair_state
    global current_state

    # データベースを初期化
    init_db()

    # 設定ファイル読み込み
    config = configparser.ConfigParser()
    config.read("config.ini", encoding="utf-8")

    # LCD を初期化
    use_lcd = True
    lcd = OledDisplayManager() if (use_lcd and OLED_AVAILABLE) else DummyLCD()

    # 起動LCD画面表示
    frames_dir = "/home/pi/Prodtrac/png_boot"
    if not os.path.exists(frames_dir):
        print(f"Animation image directory not found: {frames_dir}")
    else:
        lcd.display_animation(frames_dir=frames_dir)

    # PortSettings 抽出
    port_map = {}
    for section in config.sections():
        if section.startswith("PortSettings"):
            # enable フラグ確認
            if config.get(section, "enable", fallback="yes").lower() != "yes":
                logging.info(f"[config] Skipping {section} (enable flag not yes)")
                continue

            port = config.get(section, "port", fallback=None)
            if port:
                port_map[port] = section


    # 個別検証して有効なものだけ残す
    valid_port_map = handle_port_validation_and_continue(config, port_map)
    if not valid_port_map:
        try:
            lcd.update(status="設定エラー", worker_lcd="", process_lcd="", timer="--:--", check_no_lcd="      ")
        except Exception:
            pass
        stop_event.set()
        return

    # connection_status を明示初期化（全ポート False）
    for port in valid_port_map.keys():
        connection_status[port] = False

    # DISPLAY_SECTION 初期値の取得
    with session_scope() as session:
        initial_worker = config[DISPLAY_SECTION].get("worker_cd", "")
        initial_process = config[DISPLAY_SECTION].get("process_cd", "")
        worker_lcd_common = get_worker_lcd(session, initial_worker)
        process_lcd_common = get_process_lcd(session, initial_process)

        if DEBUG_MODE:
            print(
                f"[DEBUG] initial LCD worker_cd:{initial_worker} -> worker_lcd:{worker_lcd_common}"
            )
            print(
                f"[DEBUG] initial LCD process_cd:{initial_process} -> process_lcd:{process_lcd_common}"
            )

    lcd.update(
        worker_lcd=worker_lcd_common,
        process_lcd=process_lcd_common,
        status=STATUS_WAITING,
        timer="00:00",
        check_no_lcd="      ",
    )

    # current_state / pair_state を空で初期化
    current_state = {}
    pair_state = {}

    # 有効なポートごとに初期化（config.iniの値 + DBのラベルを解決）
    for port, section in valid_port_map.items():
        settings = config[section]
        port_worker  = settings.get("worker_cd", initial_worker)
        port_process = settings.get("process_cd", initial_process)

        # DBからLCD用の表示ラベルを解決
        with session_scope() as session:
            try:
                port_worker_lcd  = get_worker_lcd(session, port_worker) if port_worker else ""
                port_process_lcd = get_process_lcd(session, port_process) if port_process else ""
            except Exception as e:
                logging.error(f"[{port}] Failed to load LCD labels from DB: {e}")
                port_worker_lcd  = worker_lcd_common if port_worker  == initial_worker  else ""
                port_process_lcd = process_lcd_common if port_process == initial_process else ""

        # config.iniの値やDBラベルで上書き
        if port not in current_state:
            init_current_state(port, lcd=lcd)

        current_state[port].update({
            "worker_cd":   port_worker,
            "process_cd":  port_process,
            "worker_lcd":  port_worker_lcd,
            "process_lcd": port_process_lcd,
        })

        # ペア作業モードの状態も必ず初期化
        init_pair_state_for_port(port)

        if DEBUG_MODE:
            print(
                f"[DEBUG] current_state[{port}] initialized with "
                f"worker_cd={port_worker}({port_worker_lcd}), "
                f"process_cd={port_process}({port_process_lcd}), "
                f"status={STATUS_WAITING}"
            )
            print("[DEBUG] current_state initialized keys:", list(current_state.keys()))

    if DEBUG_MODE:
        logger.error("This is a sample error message. [DEBUG_MODE=True]")

    if DEBUG_MODE:
        print("Config content:")
        for section in config.sections():
            print(f"Section: {section}")
            for key, value in config.items(section):
                print(f"  {key}: {value}")

    last_qr_codes = {}


    # シリアル接続・スレッド起動（有効なポートのみ）
    threads = []
    for port, section in valid_port_map.items():
        try:
            ser = None
            try:
                ser = open_serial_port(config[section])
            except serial.SerialException as se:
                logging.error(f"Specified serial device for {port} not available: {se}")
                connection_status[port] = False
                continue
            except Exception as e:
                logging.error(f"Unexpected error opening {port}: {e}")
                connection_status[port] = False
                continue

            if ser:
                connection_status[port] = True
                if DEBUG_MODE:
                    print(f"[DEBUG] Opening thread for {port} (section={section})")

                thread = threading.Thread(
                    target=read_from_port,
                    args=(
                        ser,                # シリアルインスタンス
                        port,               # ポート名 (/dev/ttyACM0 のような文字列)
                        last_qr_codes,      # dict
                        stop_event,         # スレッドイベント
                        config,             # コンフィグパーサまたはdict
                        section,            # セッション名 (設定ファイルから)
                        lcd,                # LCDフォールバックインスタンス
                        pair_state,         # ペアモード dict
                        oled_manager,       # 利用可能な場合OLEDマネージャーを渡す（利用不可時はNone）
                    ),
                    name=f"Thread-{port}",
                    daemon=True,
                )
                thread.start()
                threads.append(thread)

            else:
                connection_status[port] = False
                logging.error(f"Failed to open serial port for {port} (section={section})")
        except Exception as e:
            logging.error(f"Port thread start failed for {port}: {e}")
            connection_status[port] = False
            continue

    # すべてのポートでスレッドが起動しなかった場合は致命扱い（E07）
    if not threads:
        logging.critical("No serial port threads started. Treating as fatal E07.")
        try:
            show_fatal_error(oled_manager, "E07", hold_time=60.0)
        except Exception:
            logging.critical("Failed to show E07 on OLED; exiting.")
        sys.exit(1)

    # 停止イベント待機
    stop_event.wait()

    # ここに到達したら停止シーケンスへ
    stop_event.set()

    try:
        stop_all_lcd_timers(final_render=True)
        try:
            if oled_manager:
                if hasattr(oled_manager, "stop_all"):
                    oled_manager.stop_all()
                elif hasattr(oled_manager, "stop_worker_swap_all"):
                    oled_manager.stop_worker_swap_all()
                else:
                    if oled_manager and hasattr(oled_manager, "stop_worker_swap"):
                        oled_manager.stop_worker_swap()
        except Exception as e:
            logging.warning(f"oled_manager global stop failed: {e}")
    except Exception as e:
        logging.warning(f"stop_all_lcd_timers failed: {e}")

    for port in list(connection_status.keys()):
        connection_status[port] = False

    for t in threads:
        try:
            t.join(timeout=3.0)
            if t.is_alive():
                logging.warning(f"Thread did not stop in time: {t.name}")
        except Exception as e:
            logging.warning(f"Thread join failed: {t.name} -> {e}")

    print("Main program stopped")
    logging.info("Main program stopped cleanly")


# メイン処理
if __name__ == "__main__":
    configure_logging()
    setup_signal_handlers()
    setup_qr_fallback_logger()

    print(f"Prod Trac {Version} is \033[38;2;255;0;0mlive.\033[0m Ready to track your productive genius. Let's roll!!! ")

    boot_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"Prod Trac {Version} is live!🚀🚀🚀 - Started at {boot_time}")

    # main_program を別スレッドで実行するようにしてもOKだがここに置いた
    main_thread = threading.Thread(target=main_program, name="MainProgramThread")
    main_thread.start()

    try:
        main_thread.join()

    except KeyboardInterrupt:
        print("Program interrupted by user.")
        logging.error("Program interrupted by user.")
        try:
            # 先に表示系を止めてからメインに停止通知しても良い
            try:
                stop_all_lcd_timers(final_render=True)
            except Exception as e:
                logging.warning(f"stop_all_lcd_timers on KeyboardInterrupt failed: {e}")

            stop_event.set()
            main_thread.join(timeout=5.0)

        except Exception:
            pass
