# SSD1309/SSD1306 日本語RSSリーダー

Raspberry PiとSSD1309/SSD1306 OLEDディスプレイ用の、日本語表示対応RSSニュースリーダーです。ボタン操作による記事・フィード切り替えや自動スクロール表示にも対応します。

---

## 📌 主な機能

- 複数のRSSフィードを自動取得・定期更新
- 日本語ニュースのタイトル＆本文表示（日本語フォント利用可能）
- 記事本文の自動横スクロール表示
- GPIOボタンによる「記事送り・戻し」「RSSフィード切替」
- シンプルで見やすいOLED画面デザイン
- 効果的なローディング／切替アニメーション付き
- 安全終了（Ctrl+Cやkill時にディスプレイ・GPIOをクリーンアップ）

---

## 💻 動作環境

- **Raspberry Pi** (Zero, 3, 4など)
- **SSD1309またはSSD1306 OLED** (I2C接続, 推奨解像度: 128x64)
- **Python 3**
- 必要ライブラリ:
    - `luma.oled`
    - `feedparser`
    - `pillow`
    - `RPi.GPIO`

---

## ⚡ インストール

1. 必要なライブラリのインストール
    ```sh
    pip3 install luma.oled feedparser pillow RPi.GPIO
    ```

2. プログラムのダウンロード
    ```sh
    git clone https://github.com/your-username/ssd1309-jp-rss.git
    cd ssd1309-jp-rss
    ```

3. 日本語フォントファイル（例: JF-Dot-MPlusH12.ttfなど）をこのディレクトリにコピー  
   ※他の.ttfファイルも指定可能です

4. 必要に応じ、ソースコード内のI2Cアドレス・ピン番号・解像度などを自分の環境に合わせて編集

---

## 🚀 使い方

```sh
python3 SSD1309_RSS.py
```

- 起動後、OLEDに日本語ニュースがスクロール表示されます
- 物理ボタンで「記事送り／戻し」「フィード切替」が可能です

---

## 🔌 配線例
Raspberry Pi	OLED	備考
3.3V or 5V	VCC	
GND	GND	
SDA (e.g.2)	SDA	I2C通信
SCL (e.g.3)	SCL	I2C通信
ボタン用GPIOピン例: GPIO17（次記事）、GPIO27（前記事）、GPIO18（フィード切替）

---

## 📝 フィード・フォントのカスタマイズ
- RSS_FEEDSリスト内で表示するRSSを編集・追加できます
- 日本語フォントを差し替える場合はinitialize()関数内のパスを書き換えてください
- スクロール速度・記事表示時間も定数で調整可能です

---

## 🖼 スクリーンショット
![demo](https://github.com/Akihiko-Fuji/SSD1306RRS_reader/blob/main/demo.jpg?raw=true)

---

## ⚠️ 既知の課題
- 一部のRSSで文字化けや説明テキストが正しく取得できないことがあります
- OLED表示やボタンの配線ミスにご注意ください

---

##👤 作者
- Akihiko Fujita
- ご質問・不具合は issues まで
