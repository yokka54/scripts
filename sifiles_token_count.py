#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIFILES と他の記法（接続リスト・表形式・DOT言語・JSON（通常）・JSON（1行形式））の
文字数とトークン数を比較するスクリプト。
各記法の文字列は sifiles_convert.py で作る（同じフォルダに sifiles.py と
sifiles_convert.py が必要）。

・SIFILES-L の文字列をグラフに変換し、同じ情報（施設の種類、接続の向き、
  支線の右岸・左岸）を持つ各形式に書き出す。
・合流は「同じ添字番号を持つ施設は、文字列中の出現順に隣り合うもの同士を接続する」
  規則で解釈する。
・トークン数は、指定したバックエンドで計測する。
    anthropic : Anthropic API の token counting（要 ANTHROPIC_API_KEY）
    gemini    : Gemini API の countTokens（要 GEMINI_API_KEY）
    tiktoken  : OpenAI 系トークナイザ（ローカル計算。初回のみ語彙ファイルをダウンロード）

使い方の例:
    python sifiles_token_count.py --dry-run
    python sifiles_token_count.py --backends anthropic
    python sifiles_token_count.py --backends gemini --gemini-model <モデルID>
    python sifiles_token_count.py --backends anthropic gemini tiktoken --gemini-model <モデルID>
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# 例題（論文 5.2.1〜5.2.3）
# ---------------------------------------------------------------------------
EXAMPLES = {
    "例1": "P[1]T(L>LT(C(LL1)(T1L1)T1H)H)T(E>)T(E>)F[1].",
    "例2": "DT(E<)F[1].",
    "例3": "HwT(T>(T(T(LT1T2L3)C14C21T32)C43C14T21)C32C43T14)T2T3T4F[1].",
}

# ---------------------------------------------------------------------------
# SIFILES-L の簡易パーサ
# ---------------------------------------------------------------------------
TOKEN = re.compile(
    r"([A-Z][a-z]?|\?)"      # 施設記号
    r"(\[\d*\])?"            # 次数
    r"((?:\d|%\d\d)*)"       # 合流の添字番号（対型）
    r"([<>])?"               # 右岸・左岸
    r"(_[A-Z][a-z]?)?"       # 付属施設（0次）
    r"|[()]|\."
)


def parse(s):
    """SIFILES-L をノード（施設記号）と有向辺 (始点, 終点, 右岸/左岸) に変換する。"""
    nodes, edges = [], []
    stack, prev, ring = [], None, {}
    pos = 0
    for m in TOKEN.finditer(s):
        if m.start() != pos:
            raise ValueError(f"解釈できない文字があります: {s[pos:m.start()]!r}")
        pos = m.end()
        t = m.group(0)
        if t == "(":
            stack.append(prev)
        elif t == ")":
            prev = stack.pop()
        elif t == ".":
            continue
        else:
            sym, _deg, idx, bank = m.group(1), m.group(2), m.group(3), m.group(4)
            nid = len(nodes)
            nodes.append(sym)
            if prev is not None:
                edges.append((prev, nid, {">": "R", "<": "L"}.get(bank)))
            for k in re.findall(r"%\d\d|\d", idx or ""):
                if k in ring:    # 対型：同じ番号の2つ目で閉じる（先に現れた方が上流側）
                    edges.append((ring.pop(k), nid, None))
                else:
                    ring[k] = nid
            prev = nid
    if pos != len(s) or stack:
        raise ValueError("文字列の終わりまたは括弧の対応が不正です")
    return nodes, edges


def max_depth(s):
    d = m = 0
    for c in s:
        if c == "(":
            d += 1
            m = max(m, d)
        elif c == ")":
            d -= 1
    return m


# ---------------------------------------------------------------------------
# 各形式への書き出し（同じ情報量、空白・改行は最小）
# ---------------------------------------------------------------------------
def to_connlist(nodes, edges):
    """接続リスト（水理計算の入力形式に準じる）：節点番号と施設種別の行の後に、
    管路ごとに「管路番号 上流側節点 下流側節点 右岸・左岸」を並べる（空白区切り、見出しなし）。"""
    lines = [f"{i+1} {t}" for i, t in enumerate(nodes)]
    lines += [f"{j+1} {a+1} {b+1}" + (f" {k}" if k else "") for j, (a, b, k) in enumerate(edges)]
    return "\n".join(lines)


def to_table(nodes, edges):
    """表形式（GISの属性テーブル）：見出し付きのノード表とリンク表（CSV）。"""
    nt = ["node_id,type"] + [f"N{i+1},{t}" for i, t in enumerate(nodes)]
    lt = ["link_id,from_node,to_node,bank"] + \
         [f"E{j+1},N{a+1},N{b+1},{k or ''}" for j, (a, b, k) in enumerate(edges)]
    return "\n".join(nt) + "\n" + "\n".join(lt)


def to_dot(nodes, edges):
    parts = [f"n{i}[label={t}]" for i, t in enumerate(nodes)]
    parts += [f"n{a}->n{b}" + (f"[bank={k}]" if k else "") for a, b, k in edges]
    return "digraph{" + ";".join(parts) + "}"


def to_dot_pretty(nodes, edges):
    """通常の書き方：1行に1文、字下げ2文字、記号の前後に空白。"""
    lines = ["digraph G {"]
    lines += [f"  n{i} [label={t}];" for i, t in enumerate(nodes)]
    lines += [f"  n{a} -> n{b}" + (f" [bank={k}]" if k else "") + ";" for a, b, k in edges]
    lines.append("}")
    return "\n".join(lines)


def _json_obj(nodes, edges):
    return {
        "nodes": [{"id": i, "type": t} for i, t in enumerate(nodes)],
        "edges": [
            dict({"source": a, "target": b}, **({"bank": k} if k else {}))
            for a, b, k in edges
        ],
    }


def to_json_pretty(nodes, edges):
    """通常の書き方：字下げ2文字（json.dumps の indent=2）。"""
    return json.dumps(_json_obj(nodes, edges), indent=2, ensure_ascii=False)


def to_json(nodes, edges):
    return json.dumps(_json_obj(nodes, edges), separators=(",", ":"), ensure_ascii=False)


FORMATS = ["SIFILES", "接続リスト", "表形式（GIS）", "DOT言語", "JSON（通常）", "JSON（1行形式）"]


def render_all(sifiles):
    """各記法の文字列は sifiles_convert.py と同じ方法で作る（施設番号は1から）"""
    from sifiles_convert import convert
    res, _notes, _n = convert(sifiles)
    strip = lambda t: t[:-1] if t.endswith("\n") else t
    texts = {
        "SIFILES": strip(res["正規形SIFILES"]),
        "接続リスト": strip(res["接続リスト"]),
        "表形式（GIS）": res["表形式_ノード表"] + strip(res["表形式_リンク表"]),
        "DOT言語": strip(res["DOT言語"]),
        "JSON（通常）": strip(res["JSON通常"]),
        "JSON（1行形式）": strip(res["JSON1行"]),
    }
    nodes, edges = parse(sifiles)
    stats = {
        "附帯施設数": len(nodes),
        "水路施設数": len(edges),
        "括弧の対": sifiles.count("("),
        "ネストの最大深さ": max_depth(sifiles),
    }
    return texts, stats


# ---------------------------------------------------------------------------
# トークン計数のバックエンド
# ---------------------------------------------------------------------------
class AnthropicCounter:
    """Anthropic API の token counting。
    メッセージ形式の固定的なオーバーヘッドを含むため、
    1文字 "X" の計数値から 1 を引いた値を補正値として差し引いた値も出力する。"""

    def __init__(self, model):
        import anthropic  # pip install anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.name = f"anthropic:{model}"
        self.overhead = self._raw("X") - 1

    def _raw(self, text):
        r = self.client.messages.count_tokens(
            model=self.model,
            messages=[{"role": "user", "content": text}],
        )
        return r.input_tokens

    def count(self, text):
        raw = self._raw(text)
        return raw, raw - self.overhead


class GeminiCounter:
    """Gemini API の countTokens。"""

    def __init__(self, model):
        from google import genai  # pip install google-genai
        self.client = genai.Client()
        self.model = model
        self.name = f"gemini:{model}"

    def count(self, text):
        r = self.client.models.count_tokens(model=self.model, contents=text)
        return r.total_tokens, r.total_tokens


class TiktokenCounter:
    """OpenAI 系トークナイザ（ローカル計算）。"""

    def __init__(self, encoding):
        import tiktoken  # pip install tiktoken
        self.enc = tiktoken.get_encoding(encoding)
        self.name = f"tiktoken:{encoding}"

    def count(self, text):
        n = len(self.enc.encode(text))
        return n, n


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="SIFILES と他形式の文字数・トークン数の比較")
    ap.add_argument("--backends", nargs="*", default=[],
                    choices=["anthropic", "gemini", "tiktoken"],
                    help="トークン数を計測するバックエンド（複数指定可）")
    ap.add_argument("--anthropic-model", default="claude-opus-5-5")
    ap.add_argument("--gemini-model", default=None,
                    help="Gemini のモデルID（論文で使ったモデルに合わせて指定）")
    ap.add_argument("--tiktoken-encoding", default="o200k_base")
    ap.add_argument("--dry-run", action="store_true",
                    help="API を呼ばず、変換結果と文字数だけを出力する")
    ap.add_argument("--outdir", default="sifiles_token_results")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    # 変換と文字数
    rendered = {k: render_all(s) for k, s in EXAMPLES.items()}
    for ex, (texts, _stats) in rendered.items():
        for fmt, text in texts.items():
            fname = os.path.join(args.outdir, f"{ex}_{fmt}.txt")
            with open(fname, "w", encoding="utf-8") as f:
                f.write(text)

    counters = []
    if not args.dry_run:
        for b in args.backends:
            if b == "anthropic":
                counters.append(AnthropicCounter(args.anthropic_model))
            elif b == "gemini":
                if not args.gemini_model:
                    sys.exit("--gemini-model を指定してください")
                counters.append(GeminiCounter(args.gemini_model))
            elif b == "tiktoken":
                counters.append(TiktokenCounter(args.tiktoken_encoding))

    rows = []
    for ex, (texts, stats) in rendered.items():
        for fmt in FORMATS:
            text = texts[fmt]
            row = {"例": ex, "形式": fmt, "文字数": len(text), **stats}
            for c in counters:
                raw, adj = c.count(text)
                row[f"{c.name} 計数値"] = raw
                row[f"{c.name} 補正値"] = adj
            rows.append(row)

    # CSV 出力
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(args.outdir, f"token_counts_{stamp}.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # 画面表示（SIFILES に対する倍率つき）
    value_cols = ["文字数"] + [f"{c.name} 補正値" for c in counters]
    for ex in EXAMPLES:
        print(f"\n=== {ex}  {EXAMPLES[ex]}")
        print("   " + "  ".join(f"{k}={v}" for k, v in rendered[ex][1].items()))
        base = {col: next(r[col] for r in rows if r["例"] == ex and r["形式"] == "SIFILES")
                for col in value_cols}
        for r in (r for r in rows if r["例"] == ex):
            cells = [f"{col}={r[col]} (×{r[col] / base[col]:.1f})" for col in value_cols]
            print(f"   {r['形式']:<12} " + "  ".join(cells))

    print(f"\n計測日時: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"CSV: {csv_path}")
    print(f"各形式の文字列: {args.outdir}/")


if __name__ == "__main__":
    main()
