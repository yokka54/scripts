#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
緩い規則で書いた SIFILES の文字列を、比較用の7つの記法に変換するプログラム

  0. 入力（緩い規則の SIFILES）
  1. 正規形の SIFILES
  2. 接続リスト（水理計算の入力形式に準じる）
  3. 表形式（GIS の属性テーブル：ノード表とリンク表の CSV）
  4. DOT 言語（通常の書き方）
  5. JSON（通常の書き方：字下げ2文字）
  6. JSON（1行形式：JSON Lines と同じ、空白・改行なし）

2〜6 は、正規形の SIFILES を読み取ったグラフから作る。施設の番号は、正規形の
文字列に現れる順（1から）にそろえるので、どの記法でも同じ施設は同じ番号になる。
含める情報は SIFILES-L と同じ（施設の種類、接続の向き、支線の右岸・左岸）。
SIFILES-M・F の水路施設の情報がある場合は、各記法の水路施設の属性として加える。

必要なもの：同じフォルダに sifiles.py（検証用プログラム）

使い方
  python sifiles_convert.py "PT(L>LT(C(LL1)(T1L2)T2H)H)T(E>)T(E>)F." --chain
  python sifiles_convert.py --file examples.txt --chain --outdir results
    （examples.txt は1行に1つの文字列。「名前<TAB>文字列」とすれば名前を付けられる）
"""
import argparse
import csv
import io
import json
import os
import sys

from sifiles import normalize, parse, SifilesError

BANK = {">": "R", "<": "L"}


def build_graph(canonical):
    """正規形の文字列から、施設（番号・記号）と水路施設（上流・下流・右岸左岸・情報）を得る"""
    g = parse(canonical)
    nodes = [(n.id + 1, n.sym) for n in g.nodes]
    edges = []
    for e in g.edges:
        bank = BANK.get(g.nodes[e["v"]].bank) if e["kind"] == "tree" else None
        edges.append({"from": e["u"] + 1, "to": e["v"] + 1, "bank": bank, "info": e["attr"]})
    return nodes, edges


def to_connlist(nodes, edges):
    lines = [f"{i} {t}" for i, t in nodes]
    for j, e in enumerate(edges, 1):
        cols = [str(j), str(e["from"]), str(e["to"])]
        if e["bank"]:
            cols.append(e["bank"])
        if e["info"]:
            cols.append(e["info"])
        lines.append(" ".join(cols))
    return "\n".join(lines) + "\n"


def to_table(nodes, edges):
    has_info = any(e["info"] for e in edges)
    nb, lb = io.StringIO(), io.StringIO()
    w = csv.writer(nb, lineterminator="\n")
    w.writerow(["node_id", "type"])
    for i, t in nodes:
        w.writerow([f"N{i}", t])
    w = csv.writer(lb, lineterminator="\n")
    w.writerow(["link_id", "from_node", "to_node", "bank"] + (["info"] if has_info else []))
    for j, e in enumerate(edges, 1):
        w.writerow([f"E{j}", f"N{e['from']}", f"N{e['to']}", e["bank"] or ""] +
                   ([e["info"] or ""] if has_info else []))
    return nb.getvalue(), lb.getvalue()


def to_dot(nodes, edges):
    lines = ["digraph G {"]
    lines += [f"  n{i} [label={t}];" for i, t in nodes]
    for e in edges:
        attrs = []
        if e["bank"]:
            attrs.append(f"bank={e['bank']}")
        if e["info"]:
            attrs.append('info="' + e["info"].replace('"', '\\"') + '"')
        lines.append(f"  n{e['from']} -> n{e['to']}" + (f" [{', '.join(attrs)}]" if attrs else "") + ";")
    lines.append("}")
    return "\n".join(lines) + "\n"


def json_obj(nodes, edges):
    out_edges = []
    for e in edges:
        d = {"source": e["from"], "target": e["to"]}
        if e["bank"]:
            d["bank"] = e["bank"]
        if e["info"]:
            d["info"] = e["info"]
        out_edges.append(d)
    return {"nodes": [{"id": i, "type": t} for i, t in nodes], "edges": out_edges}


def convert(text, chain=False):
    """7つの記法の文字列を辞書で返す（表形式はノード表とリンク表の組）"""
    canonical, candidates, notes = normalize(text, chain=chain)
    nodes, edges = build_graph(canonical)
    nt, lt = to_table(nodes, edges)
    obj = json_obj(nodes, edges)
    return {
        "入力": text.strip() + "\n",
        "正規形SIFILES": canonical + "\n",
        "接続リスト": to_connlist(nodes, edges),
        "表形式_ノード表": nt,
        "表形式_リンク表": lt,
        "DOT言語": to_dot(nodes, edges),
        "JSON通常": json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
        "JSON1行": json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n",
    }, notes, len(candidates)


FILES = [("入力", "0_入力_緩い規則.txt"), ("正規形SIFILES", "1_正規形SIFILES.txt"),
         ("接続リスト", "2_接続リスト.txt"), ("表形式_ノード表", "3_表形式_nodes.csv"),
         ("表形式_リンク表", "3_表形式_links.csv"), ("DOT言語", "4_DOT言語.dot"),
         ("JSON通常", "5_JSON通常.json"), ("JSON1行", "6_JSON1行.jsonl")]


def nchar(s):
    """文字数（末尾の改行1つは数えない）"""
    return len(s[:-1] if s.endswith("\n") else s)


def main():
    ap = argparse.ArgumentParser(description="緩い規則の SIFILES を7つの記法に変換する")
    ap.add_argument("text", nargs="?", help="SIFILES の文字列")
    ap.add_argument("--file", help="1行に1つの文字列を書いたファイル（名前<TAB>文字列も可）")
    ap.add_argument("--chain", action="store_true", help="修正前の規則（連鎖型の添字番号）で読む")
    ap.add_argument("--outdir", default="sifiles_converted")
    a = ap.parse_args()
    items = []
    if a.file:
        for k, line in enumerate(open(a.file, encoding="utf-8"), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, s = line.split("\t", 1) if "\t" in line else (f"例{len(items)+1}", line)
            items.append((name.strip(), s.strip()))
    elif a.text:
        items.append(("例1", a.text))
    else:
        sys.exit("文字列または --file を指定してください")

    os.makedirs(a.outdir, exist_ok=True)
    summary = []
    for name, s in items:
        try:
            res, notes, ncand = convert(s, chain=a.chain)
        except SifilesError as ex:
            print(f"■ {name}：変換できません：{ex}", file=sys.stderr)
            continue
        d = os.path.join(a.outdir, name)
        os.makedirs(d, exist_ok=True)
        for key, fn in FILES:
            with open(os.path.join(d, fn), "w", encoding="utf-8", newline="\n") as f:
                f.write(res[key])
        row = {"例": name,
               "入力": nchar(res["入力"]),
               "正規形SIFILES": nchar(res["正規形SIFILES"]),
               "接続リスト": nchar(res["接続リスト"]),
               "表形式": nchar(res["表形式_ノード表"]) + 1 + nchar(res["表形式_リンク表"]),
               "DOT言語": nchar(res["DOT言語"]),
               "JSON通常": nchar(res["JSON通常"]),
               "JSON1行": nchar(res["JSON1行"])}
        summary.append(row)
        print(f"■ {name}")
        print(f"  入力　：{res['入力'].strip()}")
        print(f"  正規形：{res['正規形SIFILES'].strip()}")
        for n in notes:
            print(f"  注意：{n}")
        base = row["正規形SIFILES"]
        print("  文字数：" + "，".join(f"{k} {v}（×{v/base:.1f}）" for k, v in row.items()
                                      if k not in ("例",)))
    if summary:
        path = os.path.join(a.outdir, "文字数一覧.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
            w.writeheader()
            w.writerows(summary)
        print(f"\n出力先：{a.outdir}/（例ごとのフォルダと 文字数一覧.csv）")


if __name__ == "__main__":
    main()
