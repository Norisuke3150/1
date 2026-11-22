#!/usr/bin/env python3
"""15〜20秒ショートドラマ自動結合スクリプト（パイプライン工程5）

使い方:
    python3 scripts/assemble_drama.py <project_id> [options]   # 1案件
    python3 scripts/assemble_drama.py --all [options]          # 素材が揃った未完成の案件をまとめて
    出力は1案件1行（OK / NG / SKIP）。途中経過を見たいときは --verbose

入力:  workspace/<project_id>/inbox/
         - GPTのZIP（title.txt / subtitles.txt|srt / drama.json などを含む）
         - Google Flow の動画 clip_01.mp4〜clip_04.mp4
         - Google AI Studio の音声 narration.wav または cut_01.wav〜（カット別）
出力:  output/<project_id>/final_short.mp4 と report.json（作業フォルダ work/ は終了時に自動削除）

処理の流れ:
    1. inbox のZIPを解凍し、タイトル・字幕・動画・音声を読み込む
    2. 音声の長さと無音区間を ffprobe / silencedetect で測り、各カットの尺を決める
    3. 各クリップを setpts で速度調整し、同じ設定でエンコードして concat demuxer で結合する（xfadeは使わない）
    4. 音声を -14 LUFS に揃え、クリップの環境音を 0.20〜0.25 で薄く重ねる
    5. 1080x1920 のレイアウト（上部黒帯タイトル・中央映像・下部字幕）で書き出す
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

# ---- 画面レイアウト ----------------------------------------------------------
W, H, FPS = 1080, 1920, 30
TITLE_BAND = (0, 420)
VIDEO_BAND = (420, 1380)
SUB_BAND = (1380, 1920)
SAFE_BOTTOM = 320                       # 最下部はSNSのUIと被るので空ける
SUB_LIMIT_Y = H - SAFE_BOTTOM           # 字幕はここより上に収める (=1600)
TITLE_BG = "#F5D033"
TITLE_MAX_CHARS = 12
SUB_MAX_CHARS = 16

# ---- 音声 --------------------------------------------------------------------
TARGET_LUFS = -14.0
TARGET_TP = -1.5
AMBIENT_DEFAULT = 0.22
AMBIENT_RANGE = (0.20, 0.25)
SILENCE_DB = -35
SILENCE_MIN = 0.25
TAIL_PAD = 0.4                          # 最後のセリフのあとに残す余韻（秒）
TARGET_TOTAL = (15.0, 20.0)

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
TEXT_EXT = {".txt", ".md", ".srt", ".json"}

BUNDLED_FONT = ROOT / "fonts" / "NotoSansCJKjp-Bold-subset.otf"   # 同梱（JIS第1・第2水準＋記号）
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
FONT_CANDIDATES = [  # 太字（同梱フォントで描けない文字があったときに探す）
    # macOS
    "/System/Library/Fonts/ヒラギノ角ゴシック W8.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W7.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    # Windows
    "C:/Windows/Fonts/BIZ-UDGothicB.ttc",
    "C:/Windows/Fonts/YuGothB.ttc",
    "C:/Windows/Fonts/meiryob.ttc",
    f"{_LOCALAPPDATA}/Microsoft/Windows/Fonts/NotoSansJP-Bold.ttf",
    # Linux（Debian/Ubuntu・Fedora・Arch）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf",
]
FALLBACK_FONTS = [  # 細字しかないとき（擬似太字で描く）
    "C:/Windows/Fonts/YuGothM.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/truetype/takao-gothic/TakaoPGothic.ttf",
    "/usr/share/fonts/truetype/vlgothic/VL-PGothic-Regular.ttf",
]


class AssembleError(Exception):
    pass


VERBOSE = False          # --verbose のときだけ途中経過を出す（通常は1案件1行）
WARNINGS: list[str] = []  # 案件ごとの警告（report.json に残す）


def log(msg: str) -> None:
    if VERBOSE:
        print(msg, flush=True)


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    if VERBOSE:
        print(f"⚠️  {msg}", flush=True)


# ---- ffmpeg ヘルパー ----------------------------------------------------------
def run(cmd: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        lines = res.stderr.strip().splitlines() or ["(エラー出力なし)"]
        src = next((Path(cmd[i + 1]).name for i, c in enumerate(cmd[:-1]) if c == "-i"), "?")
        raise AssembleError(f"{cmd[0]}が失敗（入力: {src}）: {lines[-1][:160]}\n" + "\n".join(lines[-25:]))
    return res


def ffprobe_json(path: Path) -> dict:
    res = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)])
    return json.loads(res.stdout)


def media_duration(path: Path) -> float:
    info = ffprobe_json(path)
    dur = info.get("format", {}).get("duration")
    if dur is None:
        durs = [float(s["duration"]) for s in info.get("streams", []) if "duration" in s]
        if not durs:
            raise AssembleError(f"長さを測れません: {path.name}")
        return max(durs)
    return float(dur)


def has_audio(path: Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in ffprobe_json(path).get("streams", []))


def detect_silences(path: Path) -> list[tuple[float, float]]:
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN}", "-f", "null", "-"],
        capture_output=True, text=True)
    silences, start = [], None
    for line in res.stderr.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    if start is not None:
        silences.append((start, media_duration(path)))
    return silences


def speech_regions(silences: list[tuple[float, float]], total: float) -> list[tuple[float, float]]:
    regions, cur = [], 0.0
    for s, e in silences:
        if s - cur > 0.12:
            regions.append((cur, s))
        cur = max(cur, e)
    if total - cur > 0.12:
        regions.append((cur, total))
    return regions


def measure_loudness(path: Path) -> dict:
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
         f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", res.stderr, re.S)
    if not m:
        raise AssembleError(f"ラウドネスを測れません: {path.name}")
    return json.loads(m.group(0))


# ---- 入力の読み込み ------------------------------------------------------------
def natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for member in zf.infolist():
        name = member.filename
        if not member.flag_bits & 0x800:  # UTF-8フラグなし: Mac(UTF-8)かWindows(Shift_JIS)で作られたZIP
            try:
                raw = name.encode("cp437")
                for enc in ("utf-8", "cp932"):
                    try:
                        name = raw.decode(enc)
                        break
                    except UnicodeDecodeError:
                        pass
            except UnicodeEncodeError:
                pass
        target = (dest / name).resolve()
        if dest not in target.parents and target != dest:
            raise AssembleError(f"ZIP内に不正なパスがあります: {member.filename}")
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def collect_files(inbox: Path, work: Path) -> list[Path]:
    unz = work / "unzipped"
    files: list[Path] = []
    for p in sorted(inbox.rglob("*"), key=natural_key):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() == ".zip":
            dest = unz / p.stem
            with zipfile.ZipFile(p) as zf:
                safe_extract(zf, dest)
            log(f"📦 ZIPを解凍: {p.name}")
        else:
            files.append(p)
    if unz.exists():
        files += [p for p in unz.rglob("*") if p.is_file()]
    return [p for p in files
            if "__MACOSX" not in p.parts and not p.name.startswith(".")]


@dataclass
class Line:
    text: str
    cut: int | None = None
    start: float | None = None
    end: float | None = None


@dataclass
class Inputs:
    clips: list[Path]
    audios: list[Path]
    title: list[str]
    lines: list[Line]
    mute_cuts: set[int] = field(default_factory=set)
    ambient_volume: float | None = None


SPEAKER_RE = re.compile(r"^[^\s:：「」]{1,8}[:：]\s*")
CUT_TAG_RE = re.compile(r"^\s*[\[［【(（]?\s*(?:cut|カット|ｶｯﾄ|C)\s*0*(\d+)\s*[\]］】)）:：]?\s*", re.I)
SRT_TIME_RE = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def clean_text(t: str) -> str:
    t = SPEAKER_RE.sub("", t.strip())
    t = t.strip()
    if t.startswith("「") and t.endswith("」"):
        t = t[1:-1]
    return t.strip()


def parse_subtitle_txt(text: str) -> list[Line]:
    lines, cur_cut = [], None
    for raw in text.splitlines():
        s = raw.strip().lstrip("-・*●").strip()
        if not s or s.startswith("#"):
            continue
        m = CUT_TAG_RE.match(s)
        if m:
            cur_cut = int(m.group(1))
            s = s[m.end():].strip()
            if not s:
                continue
        if re.fullmatch(r"[（(].*[）)]", s):   # ト書きは字幕にしない
            continue
        t = clean_text(s)
        if t:
            lines.append(Line(t, cur_cut))
    return lines


def parse_srt(text: str) -> list[Line]:
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        rows = [r for r in block.strip().splitlines() if r.strip()]
        for i, r in enumerate(rows):
            m = SRT_TIME_RE.search(r)
            if m:
                g = [int(x) for x in m.groups()]
                st = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
                en = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
                body = " ".join(clean_text(x) for x in rows[i + 1:])
                if body:
                    out.append(Line(body, None, st, en))
                break
    return out


def split_sections(text: str) -> tuple[list[str], str | None]:
    """「【タイトル】」「【字幕】」のような見出しで区切られたテキストから取り出す"""
    title, subs, mode = [], [], None
    for raw in text.splitlines():
        s = raw.strip()
        head = re.sub(r"[#【】\[\]■◆●:：\s]", "", s)
        if re.fullmatch(r"(フック)?タイトル(案)?|title|黄色ラベル", head, re.I):
            mode = "title"; continue
        if re.fullmatch(r"(セリフ)?字幕(テキスト)?|セリフ|subtitles?|台本", head, re.I):
            mode = "subs"; continue
        if re.match(r"^[#【■◆]", s):
            mode = None; continue
        if mode == "title" and s:
            title.append(s.lstrip("-・*").strip())
        elif mode == "subs":
            subs.append(raw)
    return title, ("\n".join(subs) if subs else None)


def read_text(p: Path) -> str:
    """UTF-8 / UTF-8(BOM) / UTF-16(BOM, Windowsメモ帳) / Shift_JIS(cp932) を自動判別して読む"""
    raw = p.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    warn(f"文字コードを判別できないため、読めない文字を飛ばしました: {p.name}")
    return raw.decode("utf-8", errors="replace")


TITLE_KEYS = ("title", "titles", "title_lines", "hook_title", "hook", "label",
              "yellow_label", "meta_title", "タイトル", "フックタイトル", "黄色ラベル")
SUB_KEYS = ("subtitles", "subtitle", "lines", "dialogue",
            "dialogues", "serifs", "字幕", "セリフ")
TEXT_KEYS = ("text", "line", "subtitle", "serif", "dialogue", "セリフ", "字幕", "テキスト")
CUT_KEYS = ("cut", "cut_no", "cut_number", "cut_id", "scene", "カット")
CUTS_KEYS = ("cuts", "scenes", "shots", "カット")


def _first(d: dict, keys: tuple) -> object:
    for k in keys:
        if k in d and d[k] not in (None, "", []):
            return d[k]
    lower = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k in lower and lower[k] not in (None, "", []):
            return lower[k]
    return None


def _cut_no(v: object) -> int | None:
    m = re.search(r"\d+", str(v)) if v is not None else None
    return int(m.group()) if m else None


def _title_lines(v: object) -> list[str]:
    if isinstance(v, str):
        rows = [s for s in re.split(r"\n|\\n", v) if s.strip()]
        if len(rows) == 1 and re.search(r"／| / ", v):   # 「1行目／2行目」形式
            rows = [s for s in re.split(r"／| / ", v) if s.strip()]
        return rows
    if isinstance(v, list):
        if v and all(isinstance(x, str) for x in v):
            return v
        if v and isinstance(v[0], (dict, list)):      # 候補が複数ある場合は1つ目
            return _title_lines(v[0])
    if isinstance(v, dict):
        lines = [v[k] for k in sorted(v) if re.fullmatch(r"(line|行)_?\d+", str(k), re.I)]
        if lines:
            return lines
        inner = _first(v, ("lines", "text", "main", "selected", "final") + TITLE_KEYS)
        return _title_lines(inner) if inner is not None else []
    return []


def _sub_items(v: object, cut: int | None) -> list[Line]:
    out: list[Line] = []
    if isinstance(v, str):
        out += [Line(clean_text(s), cut) for s in v.splitlines() if clean_text(s)]
    elif isinstance(v, list):
        for x in v:
            out += _sub_items(x, cut)
    elif isinstance(v, dict):
        c = _cut_no(_first(v, CUT_KEYS)) or cut
        text = _first(v, TEXT_KEYS)
        if isinstance(text, (list, dict)):
            return _sub_items(text, c)
        if text:
            t = clean_text(str(text))
            if t:
                out.append(Line(t, c, v.get("start"), v.get("end")))
    return out


def parse_meta_json(d: object) -> tuple[list[str], list[Line], set[int], float | None]:
    """drama.json / meta_titles.json を読む。いろいろな書き方を受け付ける:
       {"title": ["1行目","2行目"], "subtitles": [{"cut":1,"text":"..."}]}
       {"hook_title": "1行目\\n2行目", "cuts": [{"cut":1,"subtitles":["...","..."]}]}
       {"title": {"line1":"...","line2":"..."}, "cuts": {"cut1": ["..."], "cut2": "..."}}
    """
    if isinstance(d, list):
        d = {"cuts": d}
    if not isinstance(d, dict):
        return [], [], set(), None
    title = _title_lines(_first(d, TITLE_KEYS))
    if not title:  # {"hook_title_line1": "...", "hook_title_line2": "..."} 形式
        numbered = sorted((int(m.group(1)), str(v)) for k, v in d.items()
                          if (m := re.fullmatch(r"(?:hook_)?title_?line_?(\d+)", str(k), re.I)) and v)
        title = [v for _, v in numbered]
    lines: list[Line] = []
    cuts = _first(d, CUTS_KEYS)                         # カット別の字幕を優先
    if cuts is None:
        subs = _first(d, SUB_KEYS)
        if subs is not None:
            lines = _sub_items(subs, None)
    else:
        if isinstance(cuts, dict):                      # {"cut1": [...], "cut2": "..."}
            cuts = [{"cut": _cut_no(k), "subtitles": v} for k, v in cuts.items()]
        if isinstance(cuts, list):
            for i, c in enumerate(cuts, 1):
                if isinstance(c, dict):
                    no = _cut_no(_first(c, CUT_KEYS)) or i
                    body = _first(c, SUB_KEYS + TEXT_KEYS)
                    lines += _sub_items(body, no) if body is not None else []
                else:
                    lines += _sub_items(c, i)
    mute = {int(x) for x in d.get("mute_ambient_cuts", []) if str(x).isdigit()}
    amb = d.get("ambient_volume")
    return [str(t).strip() for t in title if str(t).strip()], lines, mute, \
        float(amb) if amb is not None else None


def load_inputs(files: list[Path]) -> Inputs:
    videos = sorted([p for p in files if p.suffix.lower() in VIDEO_EXT], key=natural_key)
    named = [p for p in videos if re.match(r"clip[_\-\s]?\d+", p.stem, re.I)]
    clips = named or videos
    if not clips:
        raise AssembleError("動画クリップ（clip_01.mp4 など）が inbox にありません")
    if not 3 <= len(clips) <= 4:
        warn(f"クリップが {len(clips)} 本です（想定は3〜4本）")

    audios = sorted([p for p in files if p.suffix.lower() in AUDIO_EXT], key=natural_key)
    if not audios:
        raise AssembleError("音声ファイル（narration.wav など）が inbox にありません")
    narr = [p for p in audios if re.search(r"narration|voice|full|all|音声|ナレ", p.stem, re.I)]
    if len(audios) > 1 and len(narr) == 1:
        audios = narr
    if len(audios) > 1 and len(audios) != len(clips):
        raise AssembleError(f"カット別音声が {len(audios)} 本、クリップが {len(clips)} 本で数が合いません")

    title: list[str] = []
    lines: list[Line] = []
    mute: set[int] = set()
    amb = None
    texts = [p for p in files if p.suffix.lower() in TEXT_EXT]

    # 1) drama.json → meta_titles.json → そのほかのJSON の順に読む
    def json_rank(p: Path) -> int:
        s = p.stem.lower()
        return 0 if s == "drama" else 1 if s == "meta_titles" else 2
    for p in sorted((t for t in texts if t.suffix.lower() == ".json"), key=json_rank):
        try:
            d = json.loads(read_text(p))
        except json.JSONDecodeError:
            warn(f"JSONとして読めません: {p.name}")
            continue
        t, ls, m, a = parse_meta_json(d)
        if t and not title:
            title = t
            log(f"   タイトルを {p.name} から読み込み")
        if ls and not lines:
            lines = ls
            log(f"   字幕を {p.name} から読み込み")
        mute |= m
        amb = a if amb is None else amb

    read = read_text
    if not title:  # 2) title.txt
        for p in texts:
            if re.search(r"title|タイトル", p.stem, re.I) and p.suffix.lower() in {".txt", ".md"}:
                title = [l.strip() for l in read(p).splitlines() if l.strip() and not l.startswith("#")]
                break
    if not lines:  # 3) subtitles.srt / subtitles.txt
        for p in sorted(texts, key=lambda x: x.suffix.lower() != ".srt"):
            if re.search(r"sub|字幕|セリフ|script|台本", p.stem, re.I):
                lines = parse_srt(read(p)) if p.suffix.lower() == ".srt" else parse_subtitle_txt(read(p))
                if lines:
                    break
    if not title or not lines:  # 4) 見出し付きのまとめテキスト
        for p in texts:
            if p.suffix.lower() in {".txt", ".md"}:
                t, s = split_sections(read(p))
                if t and not title:
                    title = t
                if s and not lines:
                    lines = parse_subtitle_txt(s)

    title = [t for t in (x.strip() for x in title) if t][:2]
    if not title:
        raise AssembleError("タイトルが見つかりません（ZIPに title.txt を入れてください）")
    for t in title:
        if len(t) > TITLE_MAX_CHARS:
            warn(f"タイトル行が{len(t)}文字です（目安{TITLE_MAX_CHARS}文字以内）: {t}")
    if not lines:
        warn("字幕テキストが見つかりません。字幕なしで書き出します")
    return Inputs(clips, audios, title, [l for l in lines if l.text], mute, amb)


# ---- 尺の計算 -----------------------------------------------------------------
def build_narration(audios: list[Path], work: Path) -> tuple[Path, list[float]]:
    """音声を1本の48kHzステレオWAVにし、カット別音声ならその境界も返す"""
    out = work / "narration_raw.wav"
    if len(audios) == 1:
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(audios[0]),
             "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(out)])
        return out, []
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for a in audios:
        cmd += ["-i", str(a)]
    prep = "".join(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo[a{i}];"
                   for i in range(len(audios)))
    joined = "".join(f"[a{i}]" for i in range(len(audios)))
    cmd += ["-filter_complex", f"{prep}{joined}concat=n={len(audios)}:v=0:a=1[out]",
            "-map", "[out]", "-c:a", "pcm_s16le", str(out)]
    run(cmd)
    bounds, acc = [0.0], 0.0
    for a in audios:
        acc += media_duration(a)
        bounds.append(acc)
    return out, bounds


def assign_lines(lines: list[Line], regions: list[tuple[float, float]],
                 seg: tuple[float, float]) -> None:
    """字幕を発話区間に割り当てる（区間数が合わなければ文字数で按分）"""
    k = len(lines)
    if k == 0:
        return
    regs = [(max(s, seg[0]), min(e, seg[1])) for s, e in regions if e > seg[0] and s < seg[1]]
    regs = [r for r in regs if r[1] - r[0] > 0.05] or [seg]
    while len(regs) > k:  # 一番短い無音でつなぐ
        gaps = [regs[i + 1][0] - regs[i][1] for i in range(len(regs) - 1)]
        i = gaps.index(min(gaps))
        regs[i:i + 2] = [(regs[i][0], regs[i + 1][1])]
    if len(regs) == k:
        for ln, (s, e) in zip(lines, regs):
            ln.start, ln.end = s, e
        return
    # 区間が足りない: 発話の始まり〜終わりを文字数で按分
    s0, e0 = regs[0][0], regs[-1][1]
    total = sum(max(1, len(l.text)) for l in lines)
    t = s0
    for ln in lines:
        d = (e0 - s0) * max(1, len(ln.text)) / total
        ln.start, ln.end = t, t + d
        t += d


def snap(t: float, candidates: list[float], tol: float = 1.0) -> float:
    near = [c for c in candidates if abs(c - t) <= tol]
    return min(near, key=lambda c: abs(c - t)) if near else t


def plan_timeline(inp: Inputs, narration: Path, per_cut_bounds: list[float]) -> list[float]:
    """各カットの境界（秒）を決め、字幕の表示時間も埋める"""
    n = len(inp.clips)
    dur = media_duration(narration)
    sil = detect_silences(narration)
    regions = speech_regions(sil, dur)
    lines = inp.lines
    tagged = bool(lines) and all(l.cut is not None for l in lines)
    timed = bool(lines) and all(l.start is not None and l.end is not None for l in lines)

    if per_cut_bounds:                                    # A) カット別音声
        bounds = per_cut_bounds[:]
        if not timed:
            if tagged:
                for c in range(1, n + 1):
                    assign_lines([l for l in lines if l.cut == c], regions,
                                 (bounds[c - 1], bounds[c]))
            else:
                assign_lines(lines, regions, (0.0, dur))
    else:                                                 # B) 1本の音声
        if lines and not timed:
            assign_lines(lines, regions, (0.0, dur))
        cuts_present = {l.cut for l in lines} if tagged else set()
        if tagged and cuts_present == set(range(1, n + 1)):
            bounds = [0.0]
            for c in range(1, n):
                last = max(l.end for l in lines if l.cut == c)
                first = min(l.start for l in lines if l.cut == c + 1)
                bounds.append((last + first) / 2)
            bounds.append(dur)
        else:
            src = [media_duration(c) for c in inp.clips]
            mids = [(s + e) / 2 for s, e in sil]
            bounds, acc = [0.0], 0.0
            for d in src[:-1]:
                acc += d
                bounds.append(snap(acc / sum(src) * dur, mids))
            bounds.append(dur)

    bounds[-1] += TAIL_PAD
    # 字幕のチラつきを防ぐ: 前後に少し余裕を持たせ、近い字幕はつなげる
    lines.sort(key=lambda l: l.start or 0)
    for i, l in enumerate(lines):
        l.start = max(0.0, l.start - 0.05)
        l.end = l.end + 0.15
        if i + 1 < len(lines):
            nxt = lines[i + 1].start - 0.05
            if nxt - l.end < 0.4:
                l.end = nxt
            l.end = min(l.end, nxt)
        l.end = min(l.end, bounds[-1])
    return bounds


# ---- 文字の描画 ----------------------------------------------------------------
def missing_glyphs(path: str, idx: int, text: str) -> str:
    """フォントに含まれていない文字を返す（□や豆腐になる文字）"""
    f = ImageFont.truetype(path, 40, index=idx)
    notdef = f.getmask("\U0010FFFD")
    ref = (notdef.size, bytes(notdef))
    out = []
    for ch in sorted(set(text)):
        if ch.isspace() or ord(ch) < 0x21:
            continue
        m = f.getmask(ch)
        if (m.size, bytes(m)) == ref:
            out.append(ch)
    return "".join(out)


def find_font(explicit: str | None, text: str) -> tuple[str, int, bool]:
    """タイトル・字幕の全文字を描けるフォントを探す。(パス, ttc番号, 本物の太字か)
    優先順: --font → fonts/ に置いたフォント → 同梱フォント → OSの太字ゴシック → OSの細字ゴシック"""
    user = sorted(str(p) for p in (ROOT / "fonts").glob("*")
                  if p.suffix.lower() in {".ttf", ".otf", ".ttc"} and p != BUNDLED_FONT)
    bold = ([explicit] if explicit else []) + user + [str(BUNDLED_FONT)] + FONT_CANDIDATES
    try:
        res = subprocess.run(["fc-match", "-f", "%{file}", "Noto Sans CJK JP:style=Bold"],
                             capture_output=True, text=True)
        if re.search(r"bold|black|heavy|w[6-9]", res.stdout, re.I):
            bold.append(res.stdout.strip())
    except FileNotFoundError:
        pass
    seen, best = set(), None
    for path, real_bold in [(c, True) for c in bold] + [(c, False) for c in FALLBACK_FONTS]:
        if not path or path in seen or not Path(path).is_file():
            continue
        seen.add(path)
        try:
            idx = jp_index(path)
            miss = missing_glyphs(path, idx, text)
        except OSError:
            continue
        if not miss:
            if not real_bold:
                warn(f"太字の日本語フォントが見つからないため、擬似太字で描きます: {path}")
            return path, idx, real_bold
        if best is None or len(miss) < len(best[3]):
            best = (path, idx, real_bold, miss)
        log(f"   {Path(path).name} では描けない文字があります: {miss} → 次のフォントを探します")
    if best:
        warn(f"どのフォントでも描けない文字があります: {best[3]}")
        return best[0], best[1], best[2]
    raise AssembleError("日本語フォントが見つかりません。fonts/ に太字ゴシック（例: NotoSansJP-Bold.ttf）を置いてください")


def jp_index(path: str) -> int:
    if not path.lower().endswith(".ttc"):
        return 0
    for i in range(10):
        try:
            name = " ".join(ImageFont.truetype(path, 20, index=i).getname())
        except OSError:
            break
        if re.search(r"JP|Japan|ゴシック|Gothic|Hiragino|Meiryo|メイリオ|Yu ", name):
            return i
    return 0


def wrap_jp(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    # 句読点・スペースで切れるならそこで切る
    n = -(-len(text) // max_chars)
    target = len(text) / n
    out, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= max_chars or (len(cur) >= target * 0.8 and ch in "、。！？!? 　…"):
            out.append(cur.strip())
            cur = ""
    if cur.strip():
        out.append(cur.strip())
    return out


def render_title(lines: list[str], font: tuple[str, int, bool], out: Path) -> None:
    path, idx, real_bold = font
    img = Image.new("RGBA", (W, TITLE_BAND[1]), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad_x, pad_y, gap, max_w = 40, 26, 16, W - 120
    size = 92
    while True:
        f = ImageFont.truetype(path, size, index=idx)
        stroke = 0 if real_bold else max(2, size // 30)
        boxes = [d.textbbox((0, 0), t, font=f, stroke_width=stroke) for t in lines]
        widths = [b[2] - b[0] for b in boxes]
        lh = max(b[3] - b[1] for b in boxes)
        block_h = lh * len(lines) + gap * (len(lines) - 1)
        if (max(widths) <= max_w - 2 * pad_x and block_h + 2 * pad_y <= TITLE_BAND[1] - 80) or size <= 40:
            break
        size -= 4
    box_w = max(widths) + 2 * pad_x
    box_h = block_h + 2 * pad_y
    x0 = (W - box_w) // 2
    y0 = (TITLE_BAND[1] - box_h) // 2
    d.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=14, fill=TITLE_BG)
    y = y0 + pad_y
    for t, b, w in zip(lines, boxes, widths):
        d.text(((W - w) // 2 - b[0], y - b[1]), t, font=f, fill="black",
               stroke_width=stroke, stroke_fill="black")
        y += lh + gap
    img.save(out)


def render_subtitle(text: str, font: tuple[str, int, bool], out: Path) -> None:
    path, idx, _ = font
    area_h = SUB_LIMIT_Y - SUB_BAND[0]           # 220px（最下部320pxは空ける）
    img = Image.new("RGBA", (W, area_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rows = wrap_jp(text, SUB_MAX_CHARS)
    if len(rows) > 2:
        warn(f"字幕が3行以上になります。短くするのがおすすめです: {text}")
    size = 62
    while True:
        f = ImageFont.truetype(path, size, index=idx)
        stroke = max(4, size // 12)
        boxes = [d.textbbox((0, 0), r, font=f, stroke_width=stroke) for r in rows]
        lh = max(b[3] - b[1] for b in boxes)
        block_h = lh * len(rows) + 10 * (len(rows) - 1)
        if (max(b[2] - b[0] for b in boxes) <= W - 80 and block_h <= area_h - 30) or size <= 34:
            break
        size -= 4
    y = 24                                       # 下部黒帯の上寄り
    for r, b in zip(rows, boxes):
        w = b[2] - b[0]
        d.text(((W - w) // 2 - b[0], y - b[1]), r, font=f, fill="white",
               stroke_width=stroke, stroke_fill="black")
        y += lh + 10
    img.save(out)


# ---- 映像の組み立て -----------------------------------------------------------
X264 = ["-c:v", "libx264", "-profile:v", "high", "-level:v", "4.0", "-pix_fmt", "yuv420p",
        "-r", str(FPS), "-x264-params", "keyint=60:min-keyint=30:scenecut=0"]


def atempo_chain(tempo: float) -> str:
    parts = []
    while tempo < 0.5:
        parts.append("atempo=0.5")
        tempo /= 0.5
    while tempo > 2.0:
        parts.append("atempo=2.0")
        tempo /= 2.0
    parts.append(f"atempo={tempo:.6f}")
    return ",".join(parts)


def render_segment(clip: Path, frames: int, out: Path, mute: bool) -> dict:
    src = media_duration(clip)
    target = frames / FPS
    factor = target / src                        # >1 でスロー、<1 で早回し
    vh = VIDEO_BAND[1] - VIDEO_BAND[0]
    vf = (f"setpts=(PTS-STARTPTS)*{factor:.6f},fps={FPS},"
          f"scale={W}:{vh}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={W}:{vh}:(ow-iw)/2:(oh-ih)/2:black,"
          f"pad={W}:{H}:0:{VIDEO_BAND[0]}:black,setsar=1,"
          f"tpad=stop_mode=clone:stop_duration=2")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(clip)]
    use_audio = has_audio(clip) and not mute
    if use_audio:
        af = (f"[0:a]asetpts=PTS-STARTPTS,{atempo_chain(1 / factor)},"
              f"aformat=sample_rates=48000:channel_layouts=stereo,apad,atrim=0:{target:.6f}[a]")
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        af = f"[1:a]atrim=0:{target:.6f}[a]"
    cmd += ["-filter_complex", f"[0:v]{vf}[v];{af}", "-map", "[v]", "-map", "[a]",
            "-frames:v", str(frames), *X264, "-crf", "14", "-preset", "medium",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(out)]
    run(cmd)
    return {"clip": clip.name, "source_sec": round(src, 3), "target_sec": round(target, 3),
            "speed": round(1 / factor, 3), "ambient": "on" if use_audio else "off"}


def normalize_narration(raw: Path, total: float, out: Path) -> dict:
    m = measure_loudness(raw)
    af = (f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA=11:"
          f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
          f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true,"
          f"aresample=48000,apad,atrim=0:{total:.6f}")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw), "-af", af,
         "-ac", "2", "-c:a", "pcm_s16le", str(out)])
    return {"before_lufs": float(m["input_i"]), "after_lufs": float(measure_loudness(out)["input_i"])}


def final_render(concat: Path, narration: Path, title_png: Path,
                 subs: list[tuple[Path, float, float]], ambient: float, out: Path) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(concat), "-i", str(narration), "-i", str(title_png)]
    for p, _, _ in subs:
        cmd += ["-i", str(p)]
    fc = [f"[0:v][2:v]overlay=0:{TITLE_BAND[0]}[v0]"]
    last = "v0"
    for i, (_, s, e) in enumerate(subs):
        fc.append(f"[{last}][{i + 3}:v]overlay=0:{SUB_BAND[0]}:enable='between(t,{s:.3f},{e:.3f})'[v{i + 1}]")
        last = f"v{i + 1}"
    fc.append(f"[0:a]volume={ambient}[amb]")
    fc.append("[1:a][amb]amix=inputs=2:duration=first:normalize=0,"
              "alimiter=limit=0.89:level=false[aout]")
    cmd += ["-filter_complex", ";".join(fc), "-map", f"[{last}]", "-map", "[aout]",
            *X264, "-crf", "18", "-preset", "slow", "-maxrate", "12M", "-bufsize", "24M",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", "-shortest", str(out)]
    run(cmd)


# ---- メイン -------------------------------------------------------------------
def assemble(project_id: str, ambient: float | None, mute_all: bool,
             mute_cuts: set[int], font_path: str | None, keep_work: bool = False) -> dict:
    """1案件を結合する。作業フォルダ work/ は成功・失敗にかかわらず最後に消す"""
    WARNINGS.clear()
    work = ROOT / "workspace" / project_id / "work"
    try:
        return _assemble(project_id, ambient, mute_all, mute_cuts, font_path)
    finally:
        if not keep_work and work.exists():
            shutil.rmtree(work, ignore_errors=True)


def _assemble(project_id: str, ambient: float | None, mute_all: bool,
              mute_cuts: set[int], font_path: str | None) -> dict:
    proj = ROOT / "workspace" / project_id
    inbox = proj / "inbox"
    if not inbox.is_dir():
        raise AssembleError(f"inbox がありません: {inbox}")
    work = proj / "work"
    if work.exists():
        shutil.rmtree(work)
    (work / "subs").mkdir(parents=True)
    out_dir = ROOT / "output" / project_id
    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"🎬 {project_id} を結合します")
    inp = load_inputs(collect_files(inbox, work))
    log(f"   クリップ {len(inp.clips)}本 / 音声 {len(inp.audios)}本 / 字幕 {len(inp.lines)}行")
    log(f"   タイトル: {' / '.join(inp.title)}")

    vol = ambient if ambient is not None else (inp.ambient_volume or AMBIENT_DEFAULT)
    if not AMBIENT_RANGE[0] <= vol <= AMBIENT_RANGE[1]:
        warn(f"環境音の音量 {vol} は推奨範囲 {AMBIENT_RANGE} の外です")
    mutes = mute_cuts | inp.mute_cuts
    if mute_all:
        mutes = set(range(1, len(inp.clips) + 1))

    narr_raw, per_cut = build_narration(inp.audios, work)
    bounds = plan_timeline(inp, narr_raw, per_cut)
    frames = [round(b * FPS) for b in bounds]
    for i in range(1, len(frames)):
        frames[i] = max(frames[i], frames[i - 1] + 1)
    total = frames[-1] / FPS
    if not TARGET_TOTAL[0] <= total <= TARGET_TOTAL[1]:
        warn(f"総尺が {total:.2f}秒です（目標 {TARGET_TOTAL[0]:.0f}〜{TARGET_TOTAL[1]:.0f}秒）")

    log("🎞  各カットを音声の長さに合わせています")
    cuts, seg_files = [], []
    for i, clip in enumerate(inp.clips, 1):
        seg = work / f"seg_{i:02d}.mkv"
        info = render_segment(clip, frames[i] - frames[i - 1], seg, i in mutes)
        info["cut"] = i
        if not 0.5 <= info["speed"] <= 2.0:
            warn(f"カット{i}の速度が {info['speed']}倍です。動きが不自然かもしれません")
        cuts.append(info)
        seg_files.append(seg)
        log(f"   カット{i}: {info['source_sec']:.2f}秒 → {info['target_sec']:.2f}秒（{info['speed']}倍）")

    lst = work / "concat.txt"
    esc = lambda p: p.resolve().as_posix().replace("'", "'\\''")
    lst.write_text("".join(f"file '{esc(p)}'\n" for p in seg_files), encoding="utf-8")
    concat = work / "concat.mkv"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", str(concat)])

    log("🔊 音声を -14 LUFS に揃えています")
    narr = work / "narration_norm.wav"
    loud = normalize_narration(narr_raw, total, narr)

    log("🔤 タイトルと字幕を描いています")
    font = find_font(font_path, "".join(inp.title) + "".join(l.text for l in inp.lines))
    bad = missing_glyphs(font[0], font[1], "".join(inp.title) + "".join(l.text for l in inp.lines))
    if bad:  # 絵文字など、どのフォントにもない文字は□にせず外す
        warn(f"描けない文字を外しました: {bad}")
        strip = lambda t: "".join(ch for ch in t if ch not in bad).strip()
        inp.title = [strip(t) for t in inp.title]
        for l in inp.lines:
            l.text = strip(l.text)
        inp.lines = [l for l in inp.lines if l.text]
    title_png = work / "title.png"
    render_title(inp.title, font, title_png)
    subs = []
    for i, l in enumerate(inp.lines, 1):
        p = work / "subs" / f"sub_{i:02d}.png"
        render_subtitle(l.text, font, p)
        subs.append((p, l.start, l.end))

    final = out_dir / "final_short.mp4"
    log("📤 書き出し中")
    final_render(concat, narr, title_png, subs, vol, final)

    report = build_report(final, project_id, inp, cuts, loud, vol, font[0])
    report["warnings"] = WARNINGS[:]
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy(title_png, out_dir / "title_preview.png")
    return report


def build_report(final: Path, project_id: str, inp: Inputs, cuts: list[dict],
                 loud: dict, vol: float, font: str) -> dict:
    info = ffprobe_json(final)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    m = measure_loudness(final)
    num, den = (int(x) for x in v["r_frame_rate"].split("/"))
    return {
        "project_id": project_id,
        "output": str(final.relative_to(ROOT)),
        "duration_sec": round(float(info["format"]["duration"]), 3),
        "resolution": f"{v['width']}x{v['height']}",
        "fps": round(num / den, 3),
        "video": f"{v['codec_name']} {v.get('profile')} level {v.get('level', 0) / 10:.1f} {v.get('pix_fmt')}",
        "audio": f"{a['codec_name']} {a.get('sample_rate')}Hz {a.get('channels')}ch",
        "loudness": {
            "narration_before_lufs": loud["before_lufs"],
            "narration_after_lufs": loud["after_lufs"],
            "final_integrated_lufs": float(m["input_i"]),
            "final_true_peak_dbtp": float(m["input_tp"]),
            "ambient_volume": vol,
        },
        "title": inp.title,
        "subtitles": [{"text": l.text, "start": round(l.start, 2), "end": round(l.end, 2)} for l in inp.lines],
        "cuts": cuts,
        "font": font,
        "size_mb": round(final.stat().st_size / 1e6, 2),
    }


def print_report(r: dict) -> None:
    L = r["loudness"]
    log(f"   解像度    : {r['resolution']} / {r['fps']}fps / {r['video']}")
    log(f"   音声レベル: ナレーション {L['narration_before_lufs']:.1f} → {L['narration_after_lufs']:.1f} LUFS"
        f" / 完成品 {L['final_integrated_lufs']:.1f} LUFS（ピーク {L['final_true_peak_dbtp']:.1f} dBTP）"
        f" / 環境音 {L['ambient_volume']}")
    w = r.get("warnings", [])
    print(f"OK    {r['project_id']}  {r['duration_sec']:.2f}秒  "
          f"{'警告なし' if not w else f'警告{len(w)}件(report.json)'}  {r['output']}", flush=True)


def inbox_missing(inbox: Path) -> list[str]:
    """素材が揃っているかを、解凍せずにファイル名だけで調べる"""
    files = [p for p in inbox.rglob("*") if p.is_file() and not p.name.startswith(".")] if inbox.is_dir() else []
    ext = lambda xs: [p for p in files if p.suffix.lower() in xs]
    miss = []
    if not ext({".zip"}) and not [p for p in ext(TEXT_EXT) if re.search(r"title|meta|drama|タイトル", p.stem, re.I)]:
        miss.append("ZIP")
    if not ext(VIDEO_EXT):
        miss.append("動画")
    if not ext(AUDIO_EXT):
        miss.append("音声")
    return miss


def run_one(pid: str, args, mute_cuts: set[int]) -> bool:
    try:
        print_report(assemble(pid, args.ambient, args.mute_ambient, mute_cuts, args.font, args.keep_work))
        return True
    except AssembleError as e:
        print(f"NG    {pid}  {str(e).splitlines()[0]}", flush=True)
        return False


def main() -> int:
    global VERBOSE
    for stream in (sys.stdout, sys.stderr):      # Windowsのコンソール(cp932)でも絵文字・日本語で落ちないように
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="ショートドラマ自動結合（工程5）")
    ap.add_argument("project_id", nargs="?", help="workspace/<project_id>/inbox を読み込む")
    ap.add_argument("--all", action="store_true",
                    help="workspace/ の全案件のうち、素材が揃っていて未完成のものだけを順に結合する")
    ap.add_argument("--ambient", type=float, help=f"環境音の音量（既定 {AMBIENT_DEFAULT}、推奨 0.20〜0.25）")
    ap.add_argument("--mute-ambient", action="store_true", help="全カットのFlow元音声を消す（AI声が入っているとき）")
    ap.add_argument("--mute-cuts", default="", help="環境音を消すカット番号（例: 2,4）")
    ap.add_argument("--font", help="タイトル・字幕に使うフォントファイル")
    ap.add_argument("--keep-work", action="store_true", help="作業フォルダ work/ を消さずに残す（調査用）")
    ap.add_argument("--verbose", "-v", action="store_true", help="途中経過と警告の中身も表示する")
    args = ap.parse_args()
    VERBOSE = args.verbose
    if bool(args.project_id) == args.all:
        ap.error("project_id か --all のどちらか一方を指定してください")
    mute_cuts = {int(x) for x in args.mute_cuts.split(",") if x.strip()}
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"NG    {tool} が見つかりません。インストールしてください", flush=True)
            return 1

    if not args.all:
        return 0 if run_one(args.project_id, args, mute_cuts) else 1

    ws = ROOT / "workspace"
    done = skipped = failed = 0
    for proj in sorted((p for p in ws.iterdir() if p.is_dir() and not p.name.startswith(".")),
                       key=natural_key) if ws.is_dir() else []:
        if (ROOT / "output" / proj.name / "final_short.mp4").exists():
            skipped += 1                          # 完成済みは黙ってスキップ
            continue
        miss = inbox_missing(proj / "inbox")
        if miss:
            print(f"SKIP  {proj.name}  素材不足: {'・'.join(miss)}", flush=True)
            skipped += 1
            continue
        if run_one(proj.name, args, mute_cuts):
            done += 1
        else:
            failed += 1
    print(f"合計  完成{done} / スキップ{skipped} / 失敗{failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
