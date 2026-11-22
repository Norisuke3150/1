# fonts

## 同梱フォント
`NotoSansCJKjp-Bold-subset.otf` — Noto Sans CJK JP Bold を、JIS第1・第2水準漢字・かな・英数字・よく使う記号（約9,400字）に絞ったもの。
ライセンスは SIL Open Font License 1.1（`OFL.txt`）。何もしなくても、Windows / Mac / Linux で同じ見た目のタイトル・字幕になります。

## 別のフォントを使いたいとき
このフォルダに太字ゴシック（.ttf / .otf / .ttc）を置くと、同梱フォントより優先されます。置いたフォントはコミットされません。
1回だけ変えたいときは `--font <パス>` を付けます。

描けない文字があった場合は、OSのフォント（ヒラギノ / BIZ UDゴシック / 游ゴシック / Noto CJK など）に自動で切り替わります。
