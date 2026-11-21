# AI Drama Factory — 作業指示書

> ⚠️ 仮の指示書です。本来の指示書の本文が届いていないため、フォルダ構成から推測して書いています。
> 本来の指示書が用意できたら、このファイルを丸ごと差し替えてください。

## 目的
日本の日常を舞台にした短尺AIドラマ（縦型リール、約30〜60秒）を量産する。

## パイプライン
1. **ネタ出し** — `config/categories.yaml` のジャンルからネタ案を作る。
   - `data/used_seeds.jsonl` を読んで、既出のネタと被るものは除外する。
   - 採用候補は `data/notebooklm_sources/YYYY-MM-DD_seeds.md` に書き出す（NotebookLMにそのまま入れられる形式）。
2. **台本** — NotebookLMで作った台本を `output/YYYYMMDD_<slug>/script.json` に保存する。
3. **カット生成** — 台本から3〜4カットを動画生成AIで作り、`clip_01.mp4`〜として保存する。
4. **合成** — 黒帯＋黄色タイトル＋字幕を合成して `final_reel.mp4` を書き出す。
5. **ログ** — 採用したネタを `data/used_seeds.jsonl` に1行追記する。

## ルール
- 人物・背景・禁止事項は必ず `config/style_guide.yaml` に従う。
- 実在の人物・企業・商品名は出さない。
- ログ（`used_seeds.jsonl`）は追記だけ。書き換えや削除はしない。
- 動画ファイル（*.mp4）はgitにコミットしない。

## used_seeds.jsonl の書式（1行1件）
```json
{"date":"2026-11-21","category":"夫婦","slug":"fridge-pudding","title":"冷蔵庫のプリン、誰が食べた？","hook":"…","status":"adopted"}
```

## script.json の書式
```json
{
  "slug": "fridge-pudding",
  "category": "夫婦",
  "title": "冷蔵庫のプリン、誰が食べた？",
  "duration_sec": 45,
  "characters": [{"name": "妻", "age": 32}, {"name": "夫", "age": 34}],
  "cuts": [
    {"id": 1, "scene": "夜のキッチン、冷蔵庫の前", "action": "…", "dialogue": [{"speaker": "妻", "line": "…"}], "subtitle": "…", "video_prompt": "…"}
  ]
}
```
