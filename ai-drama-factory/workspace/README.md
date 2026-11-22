# workspace

1本ごとに `workspace/<project_id>/inbox/` を作り、素材を入れてから「結合して」と指示します。

```
workspace/20261121-08_dokodemoii/
├── inbox/        ← ここに素材を全部入れる
│   ├── gpt_package.zip        GPTのZIP（title.txt・subtitles.txt・原画など）
│   ├── clip_01.mp4 〜 clip_04.mp4   Google Flowの動画
│   └── narration.wav          Google AI Studioの音声（カット別なら cut_01.wav 〜）
└── work/         ← スクリプトの作業用（毎回作り直される）
```

完成品は `output/<project_id>/final_short.mp4` に出ます。
このフォルダの中身（素材）はコミットされません。
