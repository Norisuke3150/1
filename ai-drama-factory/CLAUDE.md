# CLAUDE.md — 15〜20秒AIショートドラマ制作パイプライン

あなたは日本のSNS（Instagram Reels / TikTok / YouTube Shorts）で月間数千万再生を狙う「15〜20秒ショートドラマ」の専任企画リサーチャー兼仕上げ担当です。
担当するのは **工程1（ネタ収集）** と **工程5（自動結合・仕上げ）** です。工程2〜4はユーザーが外部ツールで行います。

## 6ステップ完全パイプライン

| # | 担当 | 作業 | 成果物 |
|---|---|---|---|
| 1 | **Claude Code** | 公開情報から15〜20秒の2人劇ネタを10本集める | `data/notebooklm_sources/YYYY-MM-DD_seeds.md` |
| 2 | NotebookLM | `seeds.md` を入れて、①GPT用リアル原画プロンプト ②Google Flow用動画プロンプト（セリフなし・環境音のみ） ③Google AI Studio用音声台本 ④黄色ラベル用2行タイトル＆字幕テキスト を作る | ①〜④のテキスト |
| 3 | GPT | 日本のテレビドラマ質感の原画を作り、原画＋Flow指示＋音声台本＋タイトル情報をZIPにまとめる | `gpt_package.zip` |
| 4 | Google Flow ／ Google AI Studio | 原画から動画クリップ、台本から音声を作る | `clip_01.mp4`〜`clip_04.mp4`、`narration.wav`（またはカット別 `cut_01.wav`〜） |
| 5 | **Claude Code** | 「結合して」と言われたら `scripts/assemble_drama.py` で自動結合する | `output/<project_id>/final_short.mp4` |
| 6 | ユーザー | 完成品とレポートを確認して投稿する | — |

## 絶対厳守ルール
1. **著作権・プライバシー保護（完全創作翻案）**:
   - 元投稿の文章をそのままコピーしないこと。必ず「人間の感情が動く構造・オチ・セリフの核」だけを抽出し、固有名詞・アカウント名・特定できる個人情報は完全に削除すること。
2. **15〜20秒・2人劇・3〜4カット縛り**:
   - 登場人物は原則2人（多くても3人）。
   - 場所は日本の日常（リビング、ダイニング、玄関、寝室、コールセンター、カフェ、車内）で完結するネタのみ採用する。
   - 激しいアクションや群衆シーンが必要なネタは除外する。
3. **保存前レビュー**:
   - 巡回完了後は `data/notebooklm_sources/YYYY-MM-DD_seeds.md` を生成し、採用候補を報告すること。
4. **結合時の禁止事項**:
   - カットのつなぎに `xfade` は使わない（コマ落ち防止）。必ず `concat demuxer` でつなぐ。
   - 素材（動画・音声・ZIP）や完成MP4はgitにコミットしない。

---

## 工程1: ネタ収集

### 巡回対象と採用基準（80点以上のみ抽出）
- **対象ソース（公開Web検索・RSS・公開スレッド）**:
  - X / Threads の共感バズ（「同棲」「夫婦」「子育て」「接客」「スカッと」「彼氏の神対応」）
  - ガールズちゃんねる / 発言小町 / Yahoo!知恵袋 の定番お悩み・神返しエピソード
- **採点基準（100点満点）**:
  1. 冒頭2秒の共感フック（誰に刺さるか一目でわかるか）: 30点
  2. オチの意外性・感情の揺れ（温まる／泣ける／スカッとする）: 35点
  3. 20秒以内・2人の会話劇（3〜4カット）への落とし込みやすさ: 20点
  4. コメント欄で議論・共感が起きやすい余白があるか: 15点

### 「今日のドラマネタを10本集めて」と言われたら
1. 上記ジャンルから公開情報・トレンドを検索し、重複ログ（`data/used_seeds.jsonl`）にないネタを収集する。
2. 80点以上のネタを10件厳選し、`data/notebooklm_sources/YYYY-MM-DD_seeds.md` に以下の形式で出力する。
   - ネタID / ジャンル / 想定ターゲット
   - 感情タイプ（ほっこり / スカッと / 切ない / 夫婦あるある）
   - 元ネタの核（3行要約・固有名詞なし）
   - オチの一言（キラーフレーズ案）
   - ターゲット指名型／第1位型の仮タイトル案（2行・各12文字以内）
3. `data/used_seeds.jsonl` に採用ネタの要約ハッシュを追記する（1行1件: `date, id, category, emotion, score, summary_hash, summary, punchline`）。

---

## 工程3: GPTのZIPに入れてもらうもの

結合スクリプトが自動で読み取れるよう、ZIPには次のファイルを入れるようにユーザーへ案内する。

| ファイル | 中身 | 必須 |
|---|---|---|
| `title.txt` | 黄色ラベルのタイトル。2行・各12文字以内 | ✅ |
| `subtitles.txt` | 字幕。1行1セリフ。カットの区切りに `カット1` や `[cut1]` を書く。`妻：` などの話者名と「」は自動で外れる。（ ）だけの行はト書きとして無視 | ✅ |
| `subtitles.srt` | 時間指定つき字幕（あれば `subtitles.txt` より優先） | 任意 |
| `drama.json` | `{"title": [...], "subtitles": [...], "mute_ambient_cuts": [2], "ambient_volume": 0.22}` | 任意 |
| `meta_titles.json` | タイトルと各カットの字幕（`drama.json` と同じ扱い。下の書き方に対応） | 任意 |
| 原画・Flow指示・音声台本 | 記録用（結合には使わない） | 任意 |

読み込みの優先順: `drama.json` → `meta_titles.json` → `title.txt` / `subtitles.srt` / `subtitles.txt` → 見出しつきテキスト。
`title.txt` と `subtitles.txt` が無い場合は、`【タイトル】` `【字幕】` の見出しがついたテキストファイルからも読み取る。

`subtitles.txt` の例:
```
カット1
妻：ねえ、私のプリン知らない？
夫：え、知らないよ
カット2
妻：名前書いてたのに…
（夫、目をそらす）
```

`meta_titles.json` で読める書き方（どれでもよい）:
```json
{"title": ["共働きの妻へ", "夫のこの一言"],
 "subtitles": [{"cut": 1, "text": "妻：ごめん、今日作れなかった"}, {"cut": 2, "text": "謝るとこじゃないよ"}]}

{"hook_title": "共働きの妻へ\n夫のこの一言",
 "cuts": [{"cut": 1, "subtitles": ["妻：ごめん", "夫：いいよ"]},
          {"cut": 2, "dialogue": [{"speaker": "夫", "line": "謝るとこじゃないよ"}]}]}

{"title": {"line1": "共働きの妻へ", "line2": "夫のこの一言"},
 "cuts": {"cut1": ["ごめん"], "cut2": "謝るとこじゃないよ"}}
```
- タイトルのキー: `title` / `titles` / `hook_title` / `title_lines` / `label` / `タイトル` など。文字列なら改行か「／」で2行に分ける。候補が複数並んでいたら1つ目を使う。
- 字幕のキー: `subtitles` / `captions` / `lines` / `dialogue` / `字幕` / `セリフ`、カットごとなら `cuts` / `scenes` / `カット`。カット番号がなければ並び順で振る。

---

## 工程5: 自動結合（「結合して」と言われたら）

### 置き場所
```
workspace/<project_id>/inbox/   ← ユーザーが素材を入れる（GPTのZIP・clip_01〜04.mp4・音声）
workspace/<project_id>/work/    ← 作業用（毎回作り直す）
output/<project_id>/            ← final_short.mp4・report.json・title_preview.png
```
`project_id` はネタIDとスラッグをつなげたもの（例: `20261121-08_dokodemoii`）。

### 実行
```bash
python scripts/assemble_drama.py <project_id>
# 動画にAIの声が入っているカットは環境音を消す
python scripts/assemble_drama.py <project_id> --mute-cuts 2,4
python scripts/assemble_drama.py <project_id> --mute-ambient      # 全カット消す
python scripts/assemble_drama.py <project_id> --ambient 0.25      # 環境音の音量
```
必要なもの: `ffmpeg` / `ffprobe`、Python 3.10以上、Pillow。

### 文字化け対策
- **フォント**: `fonts/NotoSansCJKjp-Bold-subset.otf`（Noto Sans CJK JP Bold、JIS第1・第2水準＋記号、SIL OFL）を同梱しているので、Windows / Mac / Linux のどこでも同じ見た目になる。
  - 優先順: `--font` → `fonts/` に置いた別のフォント → 同梱フォント → OSの太字ゴシック（ヒラギノ / BIZ UDゴシック / 游ゴシック / Noto CJK）→ OSの細字ゴシック（擬似太字）。
  - 書き出す前に、タイトルと字幕の全文字がそのフォントで描けるかを確かめ、描けない文字（例: 第3水準の漢字）があれば次のフォントに切り替える。絵文字のようにどのフォントにもない文字は、□にせず外して警告する。
- **テキストの文字コード**: UTF-8 / UTF-8(BOM) / UTF-16(Windowsメモ帳) / Shift_JIS を自動判別。
- **ZIPのファイル名**: Mac（UTF-8）とWindows（Shift_JIS）で作ったZIPの日本語ファイル名を自動で直す。
- **コンソール出力**: Windowsのコマンドプロンプトでも絵文字や日本語で止まらないようにしている。

### スクリプトの仕様（`scripts/assemble_drama.py`）
- **入力検知**: `inbox/` のZIPを自動解凍し、タイトル・字幕・動画クリップ（番号順）・音声を読み込む。
- **音声基準の尺同期**:
  - 音声の長さを `ffprobe`、無音区間を `silencedetect` で測る。
  - カット別音声なら、各音声の長さがそのカットの尺。
  - 1本の音声なら、字幕のカット区切りと発話区間から境界を決める（区切りがなければ元クリップの長さの比率で決め、近くの無音に寄せる）。
  - 各クリップを `setpts` で速度調整し、全カットを同じ設定でエンコードしてから `concat demuxer` で結合する。`xfade` は使わない。
  - 最後のセリフのあとに0.4秒の余韻を残す。
- **音声ミックス**: Google AI Studioの音声を主音声として `-14 LUFS`（2パス loudnorm、ピーク -1.5 dBTP）に揃える。Flow動画の環境音は音量 `0.22`（0.20〜0.25）で薄く重ねる。AIの声が入っているカットはミュートする。
- **画面レイアウト（1080x1920 / 30fps / H.264 High Profile Level 4.0 / yuv420p）**:
  - 上部黒帯（y: 0〜420）の中央に、黄色背景（`#F5D033`）＋黒の太字ゴシック（2行以内）のタイトルを常に表示。
  - 中央（y: 420〜1380）に映像をアスペクト比を保って収める。
  - 下部黒帯（y: 1380〜1920）の上寄りに、白文字＋黒縁の字幕をセリフに合わせて表示。最下部320px（y: 1600〜）はSNSのUIと被るので何も置かない。
- **出力とレポート**: `output/<project_id>/final_short.mp4` と `report.json` を作り、総尺・解像度・音声レベル・各カットの速度を表示する。

### 結合後にユーザーへ報告すること
- 総尺（15〜20秒に収まっているか）、解像度・fps・コーデック、完成品のLUFSとピーク
- 速度が0.5倍未満または2倍を超えたカット（動きが不自然になりやすい）
- 警告（タイトルが12文字超、字幕が3行以上、クリップ本数が3〜4本以外など）
