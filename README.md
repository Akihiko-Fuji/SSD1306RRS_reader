
SSD1309/SSD1306 日本語RSSリーダー

概要（Overview）
Raspberry PiとSSD1309/SSD1306 OLEDディスプレイ向けの日本語RSSニュースリーダーです。<BR>
ボタンによる記事・フィード切り替え、日本語フォント対応、記事テキストの自動スクロール表示など、多機能なコンパクトニュース端末を実現します。<BR>

主な機能（Features）
複数RSSフィードからの最新ニュース自動取得・更新<BR>
記事タイトル＆本文（説明）の日本語表示（フォント同梱/外部指定可能）<BR>
記事テキストの自動横スクロール表示<BR>
GPIOによるボタン操作：記事進む/戻る、フィード切替<BR>
シンプルなOLED画面デザイン＆アニメーション効果<BR>
定期自動RSS再取得
安全な終了処理・エラー通知

動作環境（Requirements）
Raspberry Pi（Zero, 3, 4などI2C・GPIOが使えるもの）
SSD1309 または SSD1306 OLED（I2C版、解像度128x64/32等はコードで調整）
Python3

必要なPythonライブラリ
 luma.oled
 feedparser
 pillow
 RPi.GPIO

インストール方法（Installation）
必要なライブラリをインストール
　pip3 install luma.oled feedparser pillow RPi.GPIO
このリポジトリをクローン
　git clone https://github.com/your-username/ssd1309-jp-rss
　cd ssd1309-jp-rss
　日本語フォントファイル（例：JF-Dot-MPlusH12.ttf等）を同じディレクトリに配置
　config部でI2Cアドレスやピン番号、ディスプレイサイズを必要に応じて調整

使い方（Usage）
・スクリプトを実行
 　python3 SSD1309_RSS.py
・起動時に自動でフィード取得＆表示開始
・各物理ボタンで「記事送り／戻り」「RSSフィード切替」が可能

配線例（Wiring Example）
　I2C接続（VCC, GND, SDA, SCL）、配線を伸ばす場合は、プルアップ抵抗を設けてください。
　GPIOピン割当（例：17, 27, 18）は物理ボタンに接続

フォントについて（Fonts）
　JF-DotやM+ FONTSなど日本語ビットマップフォントを推奨
　任意の.ttfファイル指定可

カスタマイズ方法（Customization）
　登録するRSSフィードの編集
　OELDサイズ（WIDTH, HEIGHT）の変更
　フォント差し替え
　スクロール速度や表示タイミングの調整

既知の課題・TODO（Known Issues / TODO）
　特定のRSSで説明文が出ない等のフォーマット例外
　OLEDやボタン機種による誤動作のまれな発生


作者・連絡先（Author / Contact）
Akihiko Fujita
issuesやpull requests歓迎
スクリーンショット／動画（Screenshots / Video）


