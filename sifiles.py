#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIFILES（改訂版表記規則）の文字列を解析・検証・生成するプログラム

対応する主な規則（付録の規則番号）
  ・附帯施設の記号と次数（8, 11〜15, Table 1・2）
  ・分岐の括弧と右岸・左岸（17〜20.1, 18.1）
  ・対型の合流と添字番号（21〜21.3, 23, 24）
  ・継手構造と水路施設の情報（25〜28, SIFILES-M/F）
  ・変数・改行・コメント・名前・位置指定（29〜36）
  ・記述の一意性（38〜38.4：流入がそろうまで記述しない深さ優先探索）

使い方
  python sifiles.py validate "PT(T>(LL1)VT1H)F[1]."
  python sifiles.py validate --file network.sif
  python sifiles.py to-json "PT(T>(LL1)VT1H)F[1]." > graph.json
  python sifiles.py generate graph.json
  python sifiles.py normalize "PR(Rp<)(F>)E."
  python sifiles.py normalize --chain "PT(L>LT(C(LL1)(T1L2)T2H)H)T(E>)T(E>)F."
  python sifiles.py demo
"""
import argparse
import itertools
import json
import re
import sys
from collections import deque

# ---------------------------------------------------------------------------
# 記号表（論文 Table 1・Table 2）  None は次数未定（n）
# ---------------------------------------------------------------------------
SYMBOLS = {
    # パイプライン
    "T": 3, "L": 2, "Ce": 2, "E": 1, "C": 4,
    "R": None, "F": None, "Rp": None,
    "Pg": 2, "Pv": 2, "Pr": 2, "Pf": 2, "St": 1, "Sy": 1,
    "P": 2, "At": 2, "Su": None, "Sw": None,
    "Ds": None, "Dc": 3, "H": 1,
    "Mf": 0, "Mw": 0, "Mp": 0, "Mc": 0,
    "As": 1, "Av": 1, "Ha": 1,
    "Pw": 1, "Vs": 2, "S": 0, "Ww": 3, "Js": 2, "Jd": 2, "Bt": 0,
    "V": 2, "Vc": 2, "D": 1, "Hw": 1,
    # 開水路
    "Jg": 2, "Jc": 2, "Si": 2, "So": 2, "Ti": 2, "To": 2,
    "Ui": 2, "Uo": 2, "Bi": 2, "Bo": 2,
    "Dg": None, "Db": None, "Do": None, "Og": 3, "Ov": 3, "Oo": 3,
    "Y": None, "Mk": 0, "Wf": 2, "W": 2, "Wk": 2, "Gm": 2, "Ga": 2,
    "Sv": 0, "Ss": 0,
    "G": 2, "Gc": 2, "Gj": 2, "Gs": 2, "Gt": 2, "Gb": 2,
    "Gp": 2, "Gf": 2, "Gw": 2, "Gy": 2,
    # 未定の施設（規則37）
    "?": None,
}

FULLWIDTH = str.maketrans({
    "（": "(", "）": ")", "［": "[", "］": "]", "＜": "<", "＞": ">",
    "％": "%", "＠": "@", "．": ".", "？": "?", "＋": "+", "－": "-",
    "＝": "=", "＃": "#", "＿": "_", "：": ":", "，": ",",
})


class SifilesError(Exception):
    pass


# ---------------------------------------------------------------------------
# グラフ
# ---------------------------------------------------------------------------
class Node:
    def __init__(self, nid, sym):
        self.id = nid
        self.sym = sym
        self.degree_spec = None     # ［次数］
        self.indices = []           # 記述された添字番号（記述順）
        self.bank = None            # '>' '<'
        self.attached = []          # 次数0の附帯施設
        self.name = None            # <<名前>>
        self.marked = False         # ＠（規則34）

    def to_dict(self):
        d = {"id": self.id, "sym": self.sym}
        if self.degree_spec is not None:
            d["degree"] = self.degree_spec
        if self.attached:
            d["attached"] = self.attached
        if self.name:
            d["name"] = self.name
        return d


class Graph:
    def __init__(self):
        self.nodes = []
        self.edges = []   # dict(u, v, kind='tree'|'closure', attr=str|None, marked=bool)

    def out_edges(self, u):
        return [e for e in self.edges if e["u"] == u]

    def in_edges(self, v):
        return [e for e in self.edges if e["v"] == v]

    def degree(self, u):
        return sum(1 for e in self.edges if e["u"] == u or e["v"] == u)


# ---------------------------------------------------------------------------
# 前処理：コメント・改行・変数の展開（規則29〜33）
# ---------------------------------------------------------------------------
def preprocess(text):
    text = text.translate(FULLWIDTH)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)          # 複数行コメント
    text = re.sub(r"//[^\n]*", "", text)                        # 1行コメント
    text = re.sub(r"\+\s*\n", "", text)                         # 行末の＋で継続
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        raise SifilesError("文字列が空です")
    defs, main = {}, []
    for l in lines:
        m = re.match(r"^(x\d+)\s*::\s*(.+)$", l)
        if m:
            defs[m.group(1)] = m.group(2).replace(" ", "")
        else:
            main.append(l.replace(" ", ""))
    if len(main) != 1:
        raise SifilesError("変数の定義以外の行（本体の式）がちょうど1行ではありません")
    had_period = any(strip_periods(x) != x for x in [main[0]] + list(defs.values()))
    s = strip_periods(expand(main[0], defs))
    return s + "." if had_period else s


def strip_periods(s):
    """角括弧の外にあるピリオドを取り除く（水路施設の情報の小数点は残す）"""
    out, depth = [], 0
    for c in s:
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        if c == "." and depth == 0:
            continue
        out.append(c)
    return "".join(out)


def expand(s, defs, depth=0):
    if depth > 50:
        raise SifilesError("変数の置き換えが循環しています")

    def repl(m):
        name, bank = m.group(1), m.group(2) or ""
        if name not in defs:
            raise SifilesError(f"変数 {name} の置き換え式がありません")
        body = strip_periods(expand(defs[name], defs, depth + 1))
        if bank:   # 規則29.1：先頭の附帯施設の記号の直後に＜＞を置く
            mm = re.match(r"^(@?(?:[A-Z][a-z]?|\?)(?:\[\d+\])?(?:\d|%\d\d)*)", body)
            if not mm:
                raise SifilesError(f"変数 {name} の先頭が附帯施設の記号ではありません")
            body = body[:mm.end()] + bank + body[mm.end():]
        return body
    return re.sub(r"(x\d+)([<>])?", repl, s)


# ---------------------------------------------------------------------------
# 字句解析・構文解析
# ---------------------------------------------------------------------------
EDGE_RE = re.compile(r"([-=#])(?:(\[[^\]]*\][^\[\]]*\[[^\]]*\])([-=#]))?")


def read_symbol(s, i):
    if s[i] == "?":
        return "?", i + 1
    if not s[i].isupper():
        raise SifilesError(f"{i+1}文字目：附帯施設の記号が必要です（'{s[i]}'）")
    two = s[i:i+2]
    if len(two) == 2 and two[1].islower() and two in SYMBOLS:
        return two, i + 2
    one = s[i]
    if one not in SYMBOLS:
        raise SifilesError(f"{i+1}文字目：未定義の記号 '{two if two[1:].islower() else one}' です")
    if i + 1 < len(s) and s[i+1].islower():
        raise SifilesError(f"{i+1}文字目：未定義の記号 '{two}' です")
    return one, i + 1


def parse(text, chain=False):
    """SIFILES 文字列を Graph に変換する。構文の誤りは SifilesError。
    chain=True のときは、修正前の規則（連鎖型：同じ添字番号の施設を出現順に
    隣り合うもの同士で接続する）で添字番号を読む。"""
    s = preprocess(text)
    g = Graph()
    stack, prev, i = [], None, 0
    open_idx = {}                 # 添字番号 -> 開いた節点
    pending_edge_attr, pending_edge_mark = None, False
    ended = False
    top_level = []                # 幹線水路上の節点
    order_log = []                # 添字番号の記述順（規則21.2の検証用）
    while i < len(s):
        c = s[i]
        if ended:
            raise SifilesError(f"{i+1}文字目：ピリオドの後に文字があります")
        if c == "(":
            if prev is None:
                raise SifilesError(f"{i+1}文字目：括弧の前に附帯施設がありません")
            stack.append(prev); i += 1; continue
        if c == ")":
            if not stack:
                raise SifilesError(f"{i+1}文字目：対応する '(' がありません")
            prev = stack.pop(); i += 1; continue
        if c == ".":
            ended = True; i += 1; continue
        if s.startswith("@@", i):
            pending_edge_mark = True; i += 2; continue
        m = EDGE_RE.match(s, i)
        if m and prev is not None:
            pending_edge_attr = m.group(0); i = m.end(); continue
        mark = False
        if c == "@":
            mark = True; i += 1
        sym, i = read_symbol(s, i)
        node = Node(len(g.nodes), sym)
        node.marked = mark
        dm = re.match(r"\[(\d+)\]", s[i:])
        if dm:
            node.degree_spec = int(dm.group(1)); i += dm.end()
        im = re.match(r"(?:\d|%\d\d)+", s[i:])
        if im:
            node.indices = [int(x.lstrip("%")) for x in re.findall(r"%\d\d|\d", im.group(0))]
            i += im.end()
        if i < len(s) and s[i] in "<>":
            node.bank = s[i]; i += 1
        while i < len(s) and s[i] == "_":
            sm = re.match(r"_([A-Z][a-z]?)", s[i:])
            if not sm or SYMBOLS.get(sm.group(1), -1) != 0:
                raise SifilesError(f"{i+1}文字目：'_' の後は次数0の附帯施設の記号が必要です")
            node.attached.append(sm.group(1)); i += sm.end()
        nm = re.match(r"<<(.*?)>>", s[i:])
        if nm:
            node.name = nm.group(1); i += nm.end()
        g.nodes.append(node)
        if prev is not None:
            g.edges.append({"u": prev, "v": node.id, "kind": "tree",
                            "attr": pending_edge_attr, "marked": pending_edge_mark})
        elif pending_edge_attr:
            raise SifilesError("先頭の附帯施設の前に水路施設の記号があります")
        pending_edge_attr, pending_edge_mark = None, False
        if not stack:
            top_level.append(node.id)
        for k in node.indices:
            order_log.append((node.id, k))
            if chain:
                if k in open_idx:
                    g.edges.append({"u": open_idx[k], "v": node.id, "kind": "closure",
                                    "attr": None, "marked": False, "index": k})
                open_idx[k] = node.id
                continue
            if k in open_idx:
                u = open_idx.pop(k)
                if u == node.id:
                    raise SifilesError(f"添字番号{k}が同じ附帯施設で開いて閉じています")
                # 規則21.3：先に現れた方が上流側
                g.edges.append({"u": u, "v": node.id, "kind": "closure",
                                "attr": None, "marked": False, "index": k})
            else:
                open_idx[k] = node.id
        prev = node.id
    if stack:
        raise SifilesError("閉じていない '(' があります")
    if open_idx and not chain:
        raise SifilesError(f"対になっていない添字番号があります：{sorted(open_idx)}")
    if not ended:
        raise SifilesError("最後にピリオドがありません（規則31）")
    g.main = top_level
    g.expanded = s
    return g


# ---------------------------------------------------------------------------
# 検証
# ---------------------------------------------------------------------------
def check_numbering(g):
    """規則21.2：添字番号の付け方"""
    issues, used = [], set()
    for n in g.nodes:
        if not n.indices:
            continue
        closes = [k for k in n.indices if k in used]
        opens = [k for k in n.indices if k not in used]
        if n.indices != sorted(closes) + sorted(opens):
            issues.append(f"{n.sym}（{n.id+1}番目）：閉じる番号を先に、開く番号を後に、それぞれ小さい順に書きます（規則21.2(2)）")
        freed = set(closes)
        used -= freed
        for k in sorted(opens):
            expect = 1
            while expect in used or expect in freed:
                expect += 1
            if k != expect:
                issues.append(f"{n.sym}（{n.id+1}番目）：開く添字番号は{expect}であるべきところ{k}になっています（規則21.2(1)(3)）")
            used.add(k)
    return issues


def is_dag_and_reachable(g):
    src = g.nodes[0].id
    indeg = {n.id: 0 for n in g.nodes}
    for e in g.edges:
        indeg[e["v"]] += 1
    q = deque([n for n in indeg if indeg[n] == 0]); seen = 0
    while q:
        u = q.popleft(); seen += 1
        for e in g.out_edges(u):
            indeg[e["v"]] -= 1
            if indeg[e["v"]] == 0:
                q.append(e["v"])
    dag = seen == len(g.nodes)
    reach, q = {src}, deque([src])
    while q:
        u = q.popleft()
        for e in g.out_edges(u):
            if e["v"] not in reach:
                reach.add(e["v"]); q.append(e["v"])
    return dag, len(reach) == len(g.nodes)


def validate(text):
    """文字列を検証し、(Graph, 誤り, 注意) を返す。"""
    errors, warnings = [], []
    try:
        g = parse(text)
    except SifilesError as ex:
        return None, [str(ex)], []
    main = set(g.main)
    parent = {e["v"]: e["u"] for e in g.edges if e["kind"] == "tree"}
    # 右岸・左岸の記号（規則18, 18.1）
    for n in g.nodes:
        p = parent.get(n.id)
        is_head = p is not None and p in main and n.id not in main
        if is_head and n.bank is None:
            errors.append(f"{n.sym}（{n.id+1}番目）：幹線水路から分岐した支線水路の先頭に＞または＜がありません（規則18）")
        if not is_head and n.bank is not None:
            errors.append(f"{n.sym}（{n.id+1}番目）：＞・＜は幹線水路から分岐した支線水路の先頭にのみ付けます（規則18, 24）")
    for u in g.main:
        banks = [g.nodes[e["v"]].bank for e in g.out_edges(u)
                 if e["kind"] == "tree" and e["v"] not in main]
        if "<" in banks and ">" in banks[banks.index("<"):]:
            errors.append(f"{g.nodes[u].sym}（{u+1}番目）：右岸側の支線水路を先に記述します（規則18.1）")
    # 添字番号（規則21.2）
    errors += check_numbering(g)
    # 次数（規則11〜15）
    for n in g.nodes:
        deg = g.degree(n.id)
        table = SYMBOLS[n.sym]
        spec = n.degree_spec
        if spec is not None:
            if spec != deg:
                errors.append(f"{n.sym}（{n.id+1}番目）：指定した次数［{spec}］と実際の接続数{deg}が一致しません")
        elif table is not None and table != deg:
            warnings.append(f"{n.sym}（{n.id+1}番目）：記号表の次数{table}と接続数{deg}が異なります。［{deg}］の指定が必要です（規則15）")
        elif table is None and deg == 1:
            warnings.append(f"{n.sym}（{n.id+1}番目）：境界点なので［1］を付けます（規則15）")
    # 適用条件（規則38.4）
    dag, reach = is_dag_and_reachable(g)
    if not dag:
        errors.append("流れをたどって元の附帯施設に戻る経路があります（規則38.4）")
    if not reach:
        errors.append("最上流から流れの向きにたどれない附帯施設があります（規則38.4）")
    return g, errors, warnings


# ---------------------------------------------------------------------------
# 生成（規則17〜21.3, 24, 38.1〜38.3）
#   入力：各節点の流出する水路を「下流を向いて右から左」の順に並べたグラフ
# ---------------------------------------------------------------------------
def fmt_index(k):
    return str(k) if k < 10 else f"%{k:02d}"


def main_line(out, src, dst):
    """規則38.1：最短経路。複数あれば分かれる所で右側を選ぶ"""
    rev = {}
    for u, vs in out.items():
        for v in vs:
            rev.setdefault(v, []).append(u)
    dist, q = {dst: 0}, deque([dst])
    while q:
        v = q.popleft()
        for u in rev.get(v, []):
            if u not in dist:
                dist[u] = dist[v] + 1; q.append(u)
    if src not in dist:
        raise SifilesError("最上流から最下流へ到達できません")
    path = [src]
    while path[-1] != dst:
        u = path[-1]
        path.append(next(v for v in out[u] if dist.get(v) == dist[u] - 1))
    return path


def generate(spec):
    """spec: {"nodes":[{"id","sym",("attached"),("name")}...],
              "out":{id:[右から左の順の下流節点...]}, "source", "sink",
              ("edge_attr":{"u->v": "-" など})}"""
    nodes = {n["id"]: n for n in spec["nodes"]}
    out = {k: list(spec.get("out", {}).get(k, [])) for k in nodes}
    src, dst = spec["source"], spec["sink"]
    eattr = spec.get("edge_attr", {})
    indeg = {k: 0 for k in nodes}
    for u in out:
        for v in out[u]:
            indeg[v] += 1
    main = main_line(out, src, dst)
    nxt = {main[i]: main[i + 1] for i in range(len(main) - 1)}
    mainset = set(main)
    remaining = dict(indeg)
    order, closures, children, bank, side = [], [], {}, {}, {}

    def ordered_outs(u):
        outs = out[u]
        if u in nxt:                       # 幹線水路上：右岸（右→左）、左岸（左→右）、幹線
            k = outs.index(nxt[u])
            right, left = outs[:k], list(reversed(outs[k + 1:]))
            for v in right:
                bank.setdefault(v, ">")
            for v in left:
                bank.setdefault(v, "<")
            return [(v, ">") for v in right] + [(v, "<") for v in left] + [(nxt[u], None)]
        s = side.get(u, ">")               # 支線内（規則20）
        seq = outs if s == ">" else list(reversed(outs))
        return [(v, s) for v in seq]

    def dfs(u):
        order.append(u)
        kids = []
        for v, sd in ordered_outs(u):
            remaining[v] -= 1
            if remaining[v] > 0:
                closures.append((u, v))    # 規則38.3：合流として番号を開く
            else:
                if v not in mainset:
                    side[v] = sd
                kids.append(v)
                dfs(v)
        children[u] = kids

    dfs(src)
    if len(order) != len(nodes):
        raise SifilesError("記述できない附帯施設があります（規則38.4の条件を満たしていません）")

    # 添字番号（規則21.2）
    pos = {u: i for i, u in enumerate(order)}
    opens = {u: sorted((pos[v], v) for (a, v) in closures if a == u) for u in order}
    closes = {v: sorted((pos[a], a) for (a, b) in closures if b == v) for v in order}
    num, used, idx_text = {}, set(), {}
    for u in order:
        cl = sorted(num.pop((a, u)) for _, a in closes[u])
        freed = set(cl)
        op = []
        for _, v in opens[u]:
            k = 1
            while k in used or k in freed:
                k += 1
            used.add(k); num[(u, v)] = k; op.append(k)
        used -= freed
        idx_text[u] = "".join(fmt_index(k) for k in cl) + "".join(fmt_index(k) for k in sorted(op))

    # 次数の表記（規則15）
    deg = {k: 0 for k in nodes}
    for u in out:
        for v in out[u]:
            deg[u] += 1; deg[v] += 1

    def label(u):
        n = nodes[u]
        table = SYMBOLS.get(n["sym"], "undef")
        if table == "undef":
            raise SifilesError(f"未定義の記号 {n['sym']} です")
        d = ""
        if deg[u] == 1 and table != 1:
            d = "[1]"
        elif table is not None and table != deg[u]:
            d = f"[{deg[u]}]"
        b = bank.get(u, "") if (u not in mainset and any(nxt.get(p) is not None and u in out[p]
                                                           for p in mainset)) else ""
        if u in mainset:
            b = ""
        att = "".join("_" + a for a in n.get("attached", []))
        nm = f"<<{n['name']}>>" if n.get("name") else ""
        return n["sym"] + d + idx_text[u] + b + att + nm

    def emit(u):
        s = label(u)
        k = children[u]
        parts = []
        for c in k:
            parts.append(eattr.get(f"{u}->{c}", "") + emit(c))
        if not parts:
            return s
        return s + "".join(f"({p})" for p in parts[:-1]) + parts[-1]

    return emit(src) + "."


# ---------------------------------------------------------------------------
# 文字列 → 生成用グラフ（右から左の順を推定）
# ---------------------------------------------------------------------------
def geometry_candidates(g):
    """文字列から、各節点の流出水路の『右から左』の並びの候補を列挙する。
    括弧で書かれた水路の並びは規則から一意に戻せるが、添字番号で書かれた
    水路が左右のどこにあるかは文字列から分からないため、全ての挿入位置を試す。"""
    main = set(g.main)
    head_side = {}
    parent = {e["v"]: e["u"] for e in g.edges if e["kind"] == "tree"}

    def side_of(u):
        while u not in main:
            if u in head_side:
                return head_side[u]
            p = parent[u]
            if p in main:
                head_side[u] = g.nodes[u].bank or ">"
                return head_side[u]
            u = p
        return None
    base, extras = {}, {}
    for n in g.nodes:
        u = n.id
        tree = [e["v"] for e in g.out_edges(u) if e["kind"] == "tree"]
        clos = [e["v"] for e in g.out_edges(u) if e["kind"] == "closure"]
        if u in main:
            nxt = [v for v in tree if v in main]
            br = [v for v in tree if v not in main]
            right = [v for v in br if g.nodes[v].bank == ">"]
            left = [v for v in br if g.nodes[v].bank != ">"]
            geo = right + nxt + list(reversed(left))
        else:
            geo = tree if side_of(u) == ">" else list(reversed(tree))
        base[u], extras[u] = geo, clos
    choices = []
    for u in base:
        opts = []
        for perm in itertools.permutations(extras[u]):
            for pos in itertools.combinations_with_replacement(range(len(base[u]) + 1), len(perm)):
                lst = list(base[u])
                for off, (p, v) in enumerate(zip(pos, perm)):
                    lst.insert(p + off, v)
                opts.append(lst)
        uniq = []
        for o in opts:
            if o not in uniq:
                uniq.append(o)
        choices.append((u, uniq or [list(base[u])]))
    return choices


def graph_to_spec(g, out):
    spec = {"nodes": [], "out": {}, "source": f"n{g.nodes[0].id}",
            "sink": f"n{g.main[-1]}", "edge_attr": {}}
    for n in g.nodes:
        d = {"id": f"n{n.id}", "sym": n.sym}
        if n.attached:
            d["attached"] = n.attached
        if n.name:
            d["name"] = n.name
        spec["nodes"].append(d)
        spec["out"][f"n{n.id}"] = [f"n{v}" for v in out[n.id]]
    for e in g.edges:
        if e["attr"]:
            spec["edge_attr"][f"n{e['u']}->n{e['v']}"] = e["attr"]
    return spec


def strip_degree(s):
    return re.sub(r"\[\d+\]", "", s)


def check_conformance(g, limit=50000):
    """規則38に従った記述かどうか：ある左右の配置のもとで、
    生成し直した文字列が入力と一致するものがあるかを調べる。"""
    choices = geometry_candidates(g)
    total = 1
    for _, opts in choices:
        total *= len(opts)
    if total > limit:
        return None, f"配置の候補が多すぎるため判定を省略しました（{total}通り）"
    target = strip_degree(g.expanded)
    first = None
    for combo in itertools.product(*[opts for _, opts in choices]):
        out = {u: lst for (u, _), lst in zip(choices, combo)}
        try:
            s = generate(graph_to_spec(g, out))
        except SifilesError:
            continue
        if first is None:
            first = s
        if strip_degree(s) == target:
            return True, s
    return False, first


def isomorphic_check(spec, g):
    """元のグラフと読み戻したグラフが（記号付き有向グラフとして）同型か厳密に確認"""
    ids = [n["id"] for n in spec["nodes"]]
    sym = {n["id"]: n["sym"] for n in spec["nodes"]}
    E1 = {(u, v) for u, vs in spec["out"].items() for v in vs}
    E2 = {(e["u"], e["v"]) for e in g.edges}
    # 記号と入出次数の組で候補を絞り、バックトラックで対応を探す
    sig1 = {u: (sym[u], sum(1 for a, b in E1 if a == u), sum(1 for a, b in E1 if b == u)) for u in ids}
    sig2 = {n.id: (n.sym, sum(1 for a, b in E2 if a == n.id), sum(1 for a, b in E2 if b == n.id)) for n in g.nodes}
    mapping = {}
    used = set()
    order = list(ids)

    def ok(u, x):
        for (a, b) in E1:
            if a == u and b in mapping and (x, mapping[b]) not in E2:
                return False
            if b == u and a in mapping and (mapping[a], x) not in E2:
                return False
        return True

    def bt(i):
        if i == len(order):
            return True
        u = order[i]
        for x in sig2:
            if x not in used and sig2[x] == sig1[u] and ok(u, x):
                mapping[u] = x; used.add(x)
                if bt(i + 1):
                    return True
                del mapping[u]; used.discard(x)
        return False
    return len(E1) == len(E2) and bt(0)


# ---------------------------------------------------------------------------
# 正規化：緩い規則で書かれた文字列を正規形の文字列に書き直す
# ---------------------------------------------------------------------------
def normalize(text, chain=False, limit=50000):
    """緩い規則で書かれた文字列を、正規形の規則（規則15, 17〜21.3, 24, 38.1〜38.3）
    に従った文字列に書き直す。

    入力に求めるのは文法だけで、添字番号の付け方、合流の書き方、分岐の記述順、
    次数の［1］の有無は問わない。ただし、次の2点は入力の文字列から読み取る。
      ・幹線水路から分岐する支線水路の右岸・左岸（＞・＜）
      ・同じ側に分岐する水路同士の左右の並び（右岸側は右から、左岸側は左から
        書かれているものとみなす）
    添字番号で書かれた水路が分岐点の左右のどこにあるかは文字列から分からない
    ため、その位置によって結果が変わる場合は、候補をすべて返す。

    戻り値：(正規形の文字列, 候補のリスト, 注意のリスト)"""
    g = parse(text, chain=chain)
    notes = []
    main = set(g.main)
    parent = {e["v"]: e["u"] for e in g.edges if e["kind"] == "tree"}
    for n in g.nodes:
        p = parent.get(n.id)
        if p is not None and p in main and n.id not in main and n.bank is None:
            raise SifilesError(f"{n.sym}（{n.id+1}番目）：幹線水路から分岐した支線水路の先頭に＞または＜がありません（規則18）")
    dag, reach = is_dag_and_reachable(g)
    if not dag:
        raise SifilesError("流れをたどって元の附帯施設に戻る経路があります（規則38.4）")
    if not reach:
        raise SifilesError("最上流から流れの向きにたどれない附帯施設があります（規則38.4）")
    if any(n.marked for n in g.nodes) or any(e["marked"] for e in g.edges):
        notes.append("＠・＠＠による位置の指定は正規形には含めないため、取り除きました")
    choices = geometry_candidates(g)
    total = 1
    for _, opts in choices:
        total *= len(opts)
    if total > limit:
        raise SifilesError(f"左右の配置の候補が多すぎます（{total}通り）。to-json で配置を確定し、generate を使ってください")
    results = []
    for combo in itertools.product(*[opts for _, opts in choices]):
        out = {u: lst for (u, _), lst in zip(choices, combo)}
        try:
            r = generate(graph_to_spec(g, out))
        except SifilesError:
            continue
        if r not in results:
            results.append(r)
    if not results:
        raise SifilesError("正規形の文字列を生成できませんでした")
    # 入力がすでに正規形の一つと一致すればそれを、そうでなければ辞書順で最初のものを代表とする
    target = strip_degree(g.expanded)
    rep = next((r for r in results if strip_degree(r) == target), sorted(results)[0])
    if len(results) > 1:
        notes.append(f"添字番号で書かれた水路の左右の位置が文字列から決まらないため，正規形の候補が{len(results)}通りあります．"
                     "配置を確定するには to-json で出力したJSONの out を現地の配置に合わせて直し，generate を使ってください")
    return rep, results, notes


def cmd_normalize(text, chain=False, show_all=False):
    try:
        rep, results, notes = normalize(text, chain=chain)
    except SifilesError as ex:
        sys.exit("正規化できません：" + str(ex))
    print(rep)
    for n in notes:
        print("注意：" + n, file=sys.stderr)
    if show_all and len(results) > 1:
        print("候補：", file=sys.stderr)
        for r in results:
            print("  " + r, file=sys.stderr)
    # 検算：正規形の文字列が元と同じグラフを表すこと
    g0 = parse(text, chain=chain)
    spec = {"nodes": [{"id": f"n{n.id}", "sym": n.sym} for n in g0.nodes],
            "out": {f"n{n.id}": [f"n{e['v']}" for e in g0.out_edges(n.id)] for n in g0.nodes}}
    same = isomorphic_check(spec, parse(rep))
    print(f"検算：{'元の文字列と同じネットワークです' if same else '元の文字列とネットワークが一致しません'}", file=sys.stderr)


# ---------------------------------------------------------------------------
# コマンド
# ---------------------------------------------------------------------------
def report(text):
    g, errors, warnings = validate(text)
    print(f"入力：{text.strip()}")
    if g is None:
        print("【構文の誤り】"); [print("  ・" + e) for e in errors]; return False
    if g.expanded != text.strip().translate(FULLWIDTH):
        print(f"展開後：{g.expanded}")
    n_clos = sum(1 for e in g.edges if e["kind"] == "closure")
    print(f"附帯施設 {len(g.nodes)}，水路施設 {len(g.edges)}（うち合流 {n_clos}），"
          f"幹線水路 {'→'.join(g.nodes[u].sym for u in g.main)}")
    if errors:
        print("【規則違反】"); [print("  ・" + e) for e in errors]
    if warnings:
        print("【注意】"); [print("  ・" + w) for w in warnings]
    if not errors:
        ok, s = check_conformance(g)
        if ok is None:
            print(f"【一意性】{s}")
        elif ok:
            print("【一意性】規則38に従った記述です（最上流と最下流を定めたときの一通りの記述と一致）")
        else:
            print("【一意性】規則38に従った記述ではありません")
            if s:
                print(f"  規則に従った記述の一例：{s}")
    else:
        print("【一意性】規則違反があるため判定していません")
    print()
    return not errors


def cmd_to_json(text):
    g, errors, _ = validate(text)
    if g is None:
        sys.exit("構文の誤り：" + errors[0])
    base = {u: opts[0] for u, opts in geometry_candidates(g)}
    ok, _ = check_conformance(g)
    if ok:   # 一致する配置が見つかればそれを使う
        choices = geometry_candidates(g)
        target = strip_degree(g.expanded)
        for combo in itertools.product(*[o for _, o in choices]):
            out = {u: l for (u, _), l in zip(choices, combo)}
            if strip_degree(generate(graph_to_spec(g, out))) == target:
                base = out
                break
    spec = graph_to_spec(g, base)
    spec["_note"] = "out は各附帯施設から流出する水路を、下流を向いて右から左の順に並べたもの"
    print(json.dumps(spec, ensure_ascii=False, indent=2))


def cmd_generate(path):
    spec = json.load(open(path, encoding="utf-8"))
    s = generate(spec)
    g = parse(s)
    same = isomorphic_check(spec, g)
    print(s)
    print(f"読み戻しの確認：{'元のグラフと一致' if same else '不一致'}", file=sys.stderr)


DEMO = [
    ("幹線水路上の迂回水路", "P[1]T(L>LVLL1)T1F[1]."),
    ("規則38.3：支線水路内の迂回水路", "P[1]T(T>(LL1)VT1H)F[1]."),
    ("規則21.1：2本の迂回水路が十字管で合流", "P[1]T(V1<)T(V2>)C12F[1]."),
    ("例3（格子状の管網）", "HwT(T>(T(T(LT1T2L3)C14C21T32)C43C14T21)C32C43T14)T2T3T4F[1]."),
    ("規則29, 29.1：変数と右岸記号", "P[1]T(x1>)x2\nx1::T(H)LE\nx2::VLLF[1]."),
    ("SIFILES-F：水路施設の情報", "P[1]V=[80,1,100,90]vp,600[78,1,100,98]=VF[1]."),
    ("誤りの例：添字番号の付け方", "P[1]T(LLVLL2)T2F[1]."),
    ("誤りの例：右岸・左岸の記号がない", "P[1]T(H)F[1]."),
    ("規則38に従わない例：幹線水路が最短経路でない", "P[1]T(V1>)LLLT1F[1]."),
]


def cmd_demo():
    for title, s in DEMO:
        print("■ " + title)
        report(s)
    # 生成の例
    print("■ グラフからの生成（規則38.3の例）")
    spec = {"nodes": [{"id": "P", "sym": "P"}, {"id": "T1", "sym": "T"}, {"id": "T2", "sym": "T"},
                      {"id": "L1", "sym": "L"}, {"id": "L2", "sym": "L"}, {"id": "V", "sym": "V"},
                      {"id": "T3", "sym": "T"}, {"id": "H", "sym": "H"}, {"id": "F", "sym": "F"}],
            "out": {"P": ["T1"], "T1": ["T2", "F"], "T2": ["L1", "V"], "L1": ["L2"],
                    "L2": ["T3"], "V": ["T3"], "T3": ["H"]},
            "source": "P", "sink": "F"}
    s = generate(spec)
    print(f"生成：{s}")
    print(f"読み戻し：{'元のグラフと一致' if isomorphic_check(spec, parse(s)) else '不一致'}")


def main():
    ap = argparse.ArgumentParser(description="SIFILES 文字列の解析・検証・生成")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="文字列を検証する")
    v.add_argument("text", nargs="?")
    v.add_argument("--file")
    j = sub.add_parser("to-json", help="文字列を生成用のグラフ（JSON）に変換する")
    j.add_argument("text")
    gnr = sub.add_parser("generate", help="グラフ（JSON）から文字列を生成する")
    gnr.add_argument("json")
    nz = sub.add_parser("normalize", help="緩い規則で書かれた文字列を正規形に書き直す")
    nz.add_argument("text", nargs="?")
    nz.add_argument("--file")
    nz.add_argument("--chain", action="store_true", help="修正前の規則（連鎖型の添字番号）で読む")
    nz.add_argument("--all", action="store_true", help="正規形の候補が複数ある場合にすべて表示する")
    sub.add_parser("demo", help="例題で動作を確認する")
    a = ap.parse_args()
    if a.cmd == "validate":
        text = open(a.file, encoding="utf-8").read() if a.file else a.text
        if not text:
            sys.exit("文字列または --file を指定してください")
        sys.exit(0 if report(text) else 1)
    elif a.cmd == "normalize":
        text = open(a.file, encoding="utf-8").read() if a.file else a.text
        if not text:
            sys.exit("文字列または --file を指定してください")
        cmd_normalize(text, chain=a.chain, show_all=a.all)
    elif a.cmd == "to-json":
        cmd_to_json(a.text)
    elif a.cmd == "generate":
        cmd_generate(a.json)
    else:
        cmd_demo()


if __name__ == "__main__":
    main()
