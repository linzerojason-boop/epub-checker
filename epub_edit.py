# -*- coding: utf-8 -*-
"""编辑与合并层：目录检查/生成、封面、修重复目录项、EPUB 合并。

from epub_parse import *  # noqa: F401,F403
from epub_check import *  # noqa: F401,F403


依赖 epub_parse（解析）与 epub_check（识别），不依赖 GUI。
所有名字经 epub_core 门面原样再导出。
"""
import os
import posixpath
import re
import shutil
import time
import unicodedata
import zipfile
import difflib
import inspect
import xml.etree.ElementTree as ET
from io import BytesIO

from epub_parse import (
    local, resolve, rel_to_opf, fmt_size, is_text_media, is_image_media,
    get_opf_path, parse_opf, opf_text_images, normalize_text, extract_toc,
    _nav_tree, _ncx_tree, read_toc_tree, spine_docs, image_refs, cover_hrefs,
    css_url_refs, is_mobi_file, is_pdf_file, mobi_records, mobi_first_image_index,
    mobi_images, _jpeg_size, _is_page_jpeg, pdf_page_count, pdf_images, dhash,
    hamming, _thumb, _mad, _find_match, TEXT_MIN_AVG, MIN_PAGE_SIDE, K_CAND,
    MAD_THRESHOLD, DH_THRESHOLD,
)
from epub_check import *  # noqa: F401,F403
from epub_check import book_format, detect_book, book_manifest, book_info_cached
try:
    from PIL import Image
except Exception:
    Image = None
try:
    import numpy as _NP
except Exception:
    _NP = None

__version__ = "3.12"
class TocListError(Exception):
    """章节清单无法解析。"""


_TOC_SEP = re.compile(r"^\s*([-=_*~#]{3,})\s*$")


_TOC_TARGET = re.compile(r"\s*(?:->|\||=>)\s*")


_TOC_NUM = re.compile(r"^(\d+(?:\.\d+)*)\s*[.、]?\s*")


def parse_toc_list(text):
    """把用户录入的章节清单解析成条目列表。

    一行一条；空行、`#` 注释、分隔线忽略；行首缩进（2 空格/tab=2 空格）与
    `1.` / `1.1` 编号共同决定层级；行尾 `-> 文件名` 指定目标文件。
    """
    if text is None:
        text = ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    items = []
    warnings = []
    prev_level = 1
    for lineno, raw in enumerate(text.split("\n"), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if _TOC_SEP.match(raw):
            continue
        line = raw.rstrip()
        target = None
        parts = _TOC_TARGET.split(line, maxsplit=1)
        if len(parts) == 2:
            line, target = parts[0], parts[1].strip() or None
        indent = _toc_indent_cols(line)
        body = line.strip()
        if not body:
            continue
        lvl_indent = indent // 2 + 1
        m = _TOC_NUM.match(body)
        lvl_num = body[:m.end(1)].count(".") + 1 if m else 1
        level = max(lvl_indent, lvl_num)
        level = max(1, min(level, prev_level + 1))
        if level > 4:
            warnings.append("第 %d 行层级超过 4，已压到 4" % lineno)
            level = 4
        title = body
        if not title:
            continue
        items.append({"level": level, "title": title,
                      "target": target, "line": lineno})
        prev_level = level
    if not items:
        raise TocListError("清单里没有可用的章节行")
    return items


_TITLE_STRIP = re.compile(
    r"[\s\u3000!-/:-@\[-`{-~\u2018\u2019\u201c\u201d\u2014\u2013\u00b7\u2026"
    r"\u3001\u3002\uff01\uff1f\uff1b\uff1a\uff08\uff09\u3010\u3011\u300a\u300b\u3008\u3009]")


def normalize_title(s):
    """标题归一化：全角转半角、去章节编号、去空白与标点、小写。"""
    import unicodedata
    s = unicodedata.normalize("NFKC", s or "").lower()
    t = re.sub(r"第\s*[0-9一二三四五六七八九十百零〇]+\s*[章节回卷部篇话集]", "", s)
    t = re.sub(r"^\s*\d+(?:\.\d+)*\s*[.、]?\s*", "", t)
    out = _TITLE_STRIP.sub("", t)
    if out:
        return out
    # 只有编号的标题（「第一章」「第01话」）剥完是空串：退回整串去标点，别产出空 key
    return _TITLE_STRIP.sub("", s) or (s.strip())


def toc_diff(items, tree):
    """清单与书内目录比对。"""
    import difflib
    want = [normalize_title(i["title"]) for i in items]
    got = [normalize_title(t.get("title") or "") for t in tree]
    pairs = {}
    used_toc = set()
    for i, nt in enumerate(want):
        if not nt:
            continue
        for j, tt in enumerate(got):
            if j in used_toc or not tt:
                continue
            if nt == tt:
                pairs[i] = j
                used_toc.add(j)
                break
    scored = []
    for i in range(len(items)):
        if i in pairs or not want[i]:
            continue
        for j in range(len(tree)):
            if j in used_toc or not got[j]:
                continue
            r = difflib.SequenceMatcher(None, want[i], got[j]).ratio()
            if r >= 0.8:
                scored.append((r, i, j))
    for r, i, j in sorted(scored, reverse=True):
        if i in pairs or j in used_toc:
            continue
        pairs[i] = j
        used_toc.add(j)
    missing = [items[i]["title"] for i in range(len(items)) if i not in pairs]
    extra = [tree[j].get("title") or "" for j in range(len(tree)) if j not in used_toc]
    order = []
    last_j = -1
    for i in range(len(items)):
        j = pairs.get(i)
        if j is None:
            continue
        if j < last_j:
            order.append((items[i]["title"], tree[j].get("title") or ""))
        last_j = max(last_j, j)
    level = []
    for i, j in sorted(pairs.items()):
        if items[i]["level"] != (tree[j].get("level") or 1):
            level.append((items[i]["title"], items[i]["level"],
                          tree[j].get("level") or 1))
    return {"missing": missing, "extra": extra, "order": order,
            "level": level, "matched": len(pairs)}


def toc_check(path, items):
    """检查一本 EPUB 的目录结构并与清单比对，返回 (报告文本, 问题列表)。"""
    fmt = book_format(path)
    if fmt != "EPUB":
        return "只支持 EPUB，当前文件是 %s" % (fmt or "未知格式"), \
               [("错误", "只支持 EPUB")]
    try:
        z = zipfile.ZipFile(path)
    except Exception as exc:
        return "打不开 EPUB：%s: %s" % (type(exc).__name__, exc), [("错误", "打不开 EPUB")]
    with z:
        try:
            opf = parse_opf(z, get_opf_path(z))
        except Exception as exc:
            return "解析 OPF 失败：%s: %s" % (type(exc).__name__, exc), \
                   [("错误", "解析 OPF 失败")]
        tree = read_toc_tree(z, opf)
        probs = toc_structure_problems(z, opf)
        nav_type = "EPUB3 nav" if opf.get("nav_href") else (
            "EPUB2 ncx" if opf.get("ncx_href") else "无")
    diff = toc_diff(items, tree)
    lines = ["目录检查：%s" % os.path.basename(path),
             "目录类型：%s" % nav_type,
             "书内目录 %d 条 / 清单 %d 条 / 匹配 %d 条"
             % (len(tree), len(items), diff["matched"]),
             "",
             "—— 自身结构 ——"]
    if probs:
        lines.extend("[%s] %s" % (lv, msg) for lv, msg in probs)
    else:
        lines.append("未发现问题")
    lines.extend(["", "—— 与清单比对 ——"])
    if diff["missing"]:
        lines.append("清单有、书里没有（%d）：%s"
                     % (len(diff["missing"]), "、".join(diff["missing"][:20])))
    if diff["extra"]:
        lines.append("书里有、清单没有（%d）：%s"
                     % (len(diff["extra"]), "、".join(diff["extra"][:20])))
    for a, b in diff["order"][:10]:
        lines.append("顺序不一致：清单「%s」在书里「%s」之后" % (a, b))
    for t, want_lv, got_lv in diff["level"][:10]:
        lines.append("层级不一致：「%s」清单 %d 级，书里 %d 级"
                     % (t, want_lv, got_lv))
    if not (diff["missing"] or diff["extra"] or diff["order"] or diff["level"]):
        lines.append("清单与书内目录一致")
    return "\n".join(lines), probs


def pair_toc_items(items, docs):
    """把清单条目按顺序/显式 target 配到正文文档。"""
    errors = []
    by_name = {}
    for d in docs:
        base = posixpath.basename(d["href"].replace("\\", "/")).lower()
        by_name[base] = d
        by_name[posixpath.splitext(base)[0]] = d
    used = set()
    plan = []
    for it in items:
        target = (it.get("target") or "").strip()
        if not target:
            plan.append(None)
            continue
        name = posixpath.basename(target.replace("\\", "/")).lower()
        doc = by_name.get(name) or by_name.get(posixpath.splitext(name)[0])
        if not doc:
            errors.append("清单第 %d 行的目标文件在书里不存在：%s"
                          % (it["line"], it["target"]))
            return [], errors
        if doc["id"] in used:
            errors.append("清单第 %d 行与前面的条目指向同一个文件：%s"
                          % (it["line"], it["target"]))
            return [], errors
        used.add(doc["id"])
        plan.append(doc)
    free = [d for d in docs if d["id"] not in used]
    need = sum(1 for d in plan if d is None)
    if need > len(free):
        errors.append("清单需要 %d 个文件，但书里只剩 %d 个正文文档，缺少 %d 个"
                      % (need, len(free), need - len(free)))
        return [], errors
    entries = []
    for it, doc in zip(items, plan):
        if doc is None:
            doc = free.pop(0)
        entries.append({"level": it["level"], "title": it["title"],
                        "href": doc["href"]})
    return entries, errors


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;") \
        .replace(">", "&gt;").replace('"', "&quot;")


def _rel_href(href, opf_dir, doc_full):
    """把「相对 OPF 目录的 href」换算成「相对目标文件（nav/ncx）所在目录的 href」。"""
    href = (href or "").replace("\\", "/")
    if not href:
        return ""
    full = posixpath.normpath(posixpath.join(opf_dir or "", href))
    base = posixpath.dirname(doc_full or "")
    if not base or base == ".":
        return href
    return posixpath.relpath(full, base)


def _tree_from_entries(entries):
    """把扁平 level 列表转成嵌套树。"""
    root = {"children": []}
    stack = [(0, root)]
    for e in entries:
        lv = max(1, int(e.get("level") or 1))
        while stack and stack[-1][0] >= lv:
            stack.pop()
        if not stack:
            stack = [(0, root)]
        node = {"title": e.get("title") or "", "href": e.get("href"),
                "children": []}
        stack[-1][1]["children"].append(node)
        stack.append((lv, node))
    return root["children"]


def build_nav_xhtml(entries, opf_dir, nav_full):
    """生成 EPUB3 nav.xhtml。"""
    def emit(nodes, indent):
        pad = "  " * indent
        out = [pad + "<ol>"]
        for n in nodes:
            href = _esc(_rel_href(n["href"], opf_dir, nav_full)) if n.get("href") else ""
            label = _esc(n["title"])
            if n["children"]:
                out.append(pad + "  <li>")
                out.append(pad + "    <span>%s</span>" % label)
                out.extend(emit(n["children"], indent + 2))
                out.append(pad + "  </li>")
            else:
                out.append(pad + '  <li><a href="%s">%s</a></li>' % (href, label))
        out.append(pad + "</ol>")
        return out

    body = "\n".join(emit(_tree_from_entries(entries), 3))
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<!DOCTYPE html>\n'
           '<html xmlns="http://www.w3.org/1999/xhtml" '
           'xmlns:epub="http://www.idpf.org/2007/ops">\n'
           '<head><meta charset="utf-8"/><title>目录</title></head>\n'
           '<body>\n  <nav epub:type="toc" id="toc">\n%s\n  </nav>\n</body>\n</html>\n'
           % body)
    return xml.encode("utf-8")


def build_ncx(entries, opf_dir, ncx_full, title, uid):
    """生成 EPUB2 兼容的 toc.ncx。"""
    counter = [0]

    def emit(nodes, depth):
        pad = "  " * depth
        out = []
        for n in nodes:
            counter[0] += 1
            src = _esc(_rel_href(n["href"], opf_dir, ncx_full)) if n.get("href") else ""
            out.append(pad + '<navPoint id="np-%d" playOrder="%d">'
                       % (counter[0], counter[0]))
            out.append(pad + "  <navLabel><text>%s</text></navLabel>"
                       % _esc(n["title"]))
            out.append(pad + '  <content src="%s"/>' % src)
            if n["children"]:
                out.extend(emit(n["children"], depth + 1))
            out.append(pad + "</navPoint>")
        return out

    body = "\n".join(emit(_tree_from_entries(entries), 2))
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
           '  <head>\n'
           '    <meta name="dtb:uid" content="%s"/>\n'
           '    <meta name="dtb:depth" content="3"/>\n'
           '    <meta name="dtb:totalPageCount" content="0"/>\n'
           '    <meta name="dtb:maxPageNumber" content="0"/>\n'
           '  </head>\n'
           '  <docTitle><text>%s</text></docTitle>\n'
           '  <navMap>\n%s\n  </navMap>\n</ncx>\n'
           % (_esc(uid), _esc(title), body))
    return xml.encode("utf-8")


def patch_opf(opf_bytes, opf_dir, title="", uid="",
              nav_full=None, ncx_full=None, cover=None, cover_page=None):
    """给 OPF 补 nav / ncx / 封面条目与 spine 属性，返回新字节。

    nav_full / ncx_full 为 None 时不改动目录条目（加封面时用）。
    cover={"href": ..., "id": "cover-image", "media_type": "image/jpeg"}。
    cover_page={"href": ..., "id": "cover-page"} 时在 spine 首位插入封面页。
    """
    root = ET.fromstring(opf_bytes)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag[1:].split("}")[0]
    prefix = ("{%s}" % ns) if ns else ""
    manifest = spine = metadata = None
    for el in root:
        ln = local(el.tag)
        if ln == "manifest":
            manifest = el
        elif ln == "spine":
            spine = el
        elif ln == "metadata":
            metadata = el
    if manifest is None:
        manifest = ET.SubElement(root, prefix + "manifest")
    if spine is None:
        spine = ET.SubElement(root, prefix + "spine")
    if metadata is None:
        metadata = ET.SubElement(root, prefix + "metadata")

    def rel(full):
        if opf_dir and full.startswith(opf_dir + "/"):
            return full[len(opf_dir) + 1:]
        return full

    def uniq(base):
        ids = {el.get("id") for el in manifest if local(el.tag) == "item"}
        cand, n = base, 1
        while cand in ids:
            n += 1
            cand = "%s%d" % (base, n)
        return cand

    if nav_full:
        ET.SubElement(manifest, prefix + "item", {
            "id": uniq("nav"), "href": rel(nav_full),
            "media-type": "application/xhtml+xml", "properties": "nav"})
    if ncx_full:
        ncx_id = uniq("ncx")
        ET.SubElement(manifest, prefix + "item", {
            "id": ncx_id, "href": rel(ncx_full),
            "media-type": "application/x-dtbncx+xml"})
        spine.set("toc", ncx_id)

    if cover:
        # 只允许一条 properties="cover-image"：先把旧条目的这个属性摘掉，
        # 否则源书本来就标过时会留下两条（EPUB3 不合规，verify 也不报）。
        for el in list(manifest):
            props = (el.get("properties") or "").split()
            if "cover-image" in props:
                rest = [x for x in props if x != "cover-image"]
                if rest:
                    el.set("properties", " ".join(rest))
                else:
                    el.attrib.pop("properties", None)
        cover_id = uniq(cover.get("id") or "cover-image")
        ET.SubElement(manifest, prefix + "item", {
            "id": cover_id, "href": rel(cover["href"]),
            "media-type": cover.get("media_type", "image/jpeg"),
            "properties": "cover-image"})
        meta = None
        for el in metadata:
            if local(el.tag) == "meta" and el.get("name") == "cover":
                meta = el
                break
        if meta is None:
            meta = ET.SubElement(metadata, prefix + "meta")
        meta.set("name", "cover")
        meta.set("content", cover_id)

    if cover_page:
        page_id = uniq(cover_page.get("id") or "cover-page")
        ET.SubElement(manifest, prefix + "item", {
            "id": page_id, "href": rel(cover_page["href"]),
            "media-type": "application/xhtml+xml"})
        ref = ET.Element(prefix + "itemref")
        ref.set("idref", page_id)
        ref.set("linear", "yes")
        spine.insert(0, ref)
        guide = None
        for el in root:
            if local(el.tag) == "guide":
                guide = el
        if guide is None:
            guide = ET.SubElement(root, prefix + "guide")
        found = None
        for el in guide:
            if local(el.tag) == "reference" and el.get("type") == "cover":
                found = el
        if found is None:
            found = ET.SubElement(guide, prefix + "reference")
            found.set("type", "cover")
        found.set("href", rel(cover_page["href"]))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def write_epub_variant(src, dst, replace=None, extras=None, cover_first=False):
    """按原顺序复制 zip，替换/追加若干条目；mimetype 固定首条 STORED。

    replace 里的名字若原 zip 里没有，会作为新条目追加。
    """
    replace = dict(replace or {})
    extras = dict(extras or {})
    part = dst + ".part"
    try:
        with zipfile.ZipFile(src) as zin, \
                zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as zout:
            mi = zipfile.ZipInfo("mimetype")
            mi.compress_type = zipfile.ZIP_STORED
            zout.writestr(mi, b"application/epub+zip")
            seen = set()
            for info in zin.infolist():
                if info.filename == "mimetype":
                    continue
                seen.add(info.filename)
                if info.filename in replace:
                    zout.writestr(info, replace[info.filename])
                else:
                    # 流式复制：大书里单个 300MB 资源也不会整块进内存
                    with zin.open(info.filename) as fi, zout.open(info, "w") as fo:
                        shutil.copyfileobj(fi, fo, 1024 * 1024)
            for name, data in replace.items():
                if name not in seen and name != "mimetype":
                    zout.writestr(name, data)
            for name, data in extras.items():
                if name not in seen and name not in replace:
                    zout.writestr(name, data)
        os.replace(part, dst)
    except Exception:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        raise
    return dst


def verify_epub(path, expect=()):
    """自检：zip 完整、OPF 可解析、指定条目存在、目录项都指向存在的文件。"""
    probs = []
    try:
        z = zipfile.ZipFile(path)
    except Exception as exc:
        return ["打不开生成的 EPUB：%s" % exc]
    with z:
        bad = z.testzip()
        if bad:
            probs.append("压缩包损坏：%s" % bad)
        if "mimetype" not in z.namelist():
            probs.append("缺少 mimetype")
        try:
            opf = parse_opf(z, get_opf_path(z))
        except Exception as exc:
            probs.append("OPF 解析失败：%s" % exc)
            return probs
        for name in expect:
            if name not in z.namelist():
                probs.append("缺少文件：%s" % name)
        tree = read_toc_tree(z, opf)
        names = set(z.namelist())
        for t in tree:
            href = t.get("href") or ""
            if href and href not in names:
                alt = resolve(opf["dir"], href)
                if not alt or alt not in names:
                    probs.append("目录项指向不存在的文件：%s" % href)
    return probs


COVER_MAX_SIDE = 2000


COVER_TARGET_SIDE = 1600


COVER_MAX_BYTES = 2 * 1024 * 1024


def default_dst(src, suffix):
    """<原名>_<后缀>.epub，重名自动加 (2)。"""
    base = os.path.splitext(src)[0]
    cand = "%s_%s.epub" % (base, suffix)
    n = 1
    while os.path.exists(cand):
        n += 1
        cand = "%s_%s (%d).epub" % (base, suffix, n)
    return cand


def prepare_cover(image_path):
    """读图；过大则缩放转 JPEG，返回 (bytes, 扩展名, media_type)。"""
    if Image is None:
        raise RuntimeError("需要 Pillow 才能处理封面图")
    img = Image.open(image_path)
    ext = os.path.splitext(image_path)[1].lower()
    big = max(img.size) > COVER_MAX_SIDE or os.path.getsize(image_path) > COVER_MAX_BYTES
    if not big and ext in (".jpg", ".jpeg") and img.mode == "RGB":
        with open(image_path, "rb") as f:
            return f.read(), "jpg", "image/jpeg"
    if max(img.size) > COVER_TARGET_SIDE:
        img = img.copy()
        img.thumbnail((COVER_TARGET_SIDE, COVER_TARGET_SIDE))
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=88, optimize=True)
    return buf.getvalue(), "jpg", "image/jpeg"


def _opf_meta(opf_bytes):
    root = ET.fromstring(opf_bytes)
    title, uid = "", ""
    for el in root.iter():
        ln = local(el.tag)
        if ln == "title" and not title:
            title = (el.text or "").strip()
        elif ln == "identifier" and not uid:
            uid = (el.text or "").strip()
    return title or "未命名", uid or "urn:uuid:0000"


def apply_toc(src, items, dst=None):
    """生成 nav + ncx、改写 OPF、另存新文件并自检。"""
    dst = dst or default_dst(src, "加目录")
    try:
        with zipfile.ZipFile(src) as z:
            opf = parse_opf(z, get_opf_path(z))
            docs = spine_docs(z, opf)
            opf_bytes = z.read(opf["path"])
        entries, errs = pair_toc_items(items, docs)
        if errs:
            return None, errs
        title, uid = _opf_meta(opf_bytes)
        dir_ = opf["dir"]
        nav_full = opf.get("nav_href") or "nav.xhtml"
        ncx_full = opf.get("ncx_href") or "toc.ncx"
        if dir_:
            nav_full = dir_ + "/" + nav_full
            ncx_full = dir_ + "/" + ncx_full
        nav_xml = build_nav_xhtml(entries, dir_, nav_full)
        ncx_xml = build_ncx(entries, dir_, ncx_full, title, uid)
        new_opf = patch_opf(opf_bytes, dir_, title=title, uid=uid,
                            nav_full=nav_full, ncx_full=ncx_full)
        write_epub_variant(src, dst,
                           replace={nav_full: nav_xml, ncx_full: ncx_xml,
                                    opf["path"]: new_opf})
        probs = verify_epub(dst, expect=[nav_full, ncx_full])
        if probs:
            os.remove(dst)
            return None, probs
        return dst, []
    except Exception as exc:
        return _fail(dst, exc)


def apply_cover(src, image_path, dst=None, replace_cover=False):
    """加封面图与封面页（spine 首位），另存新文件并自检。"""
    dst = dst or default_dst(src, "加封面")
    try:
        data, ext, media = prepare_cover(image_path)
        with zipfile.ZipFile(src) as z:
            opf = parse_opf(z, get_opf_path(z))
            opf_bytes = z.read(opf["path"])
            existing = cover_hrefs(z, opf)
            names = set(z.namelist())
        if existing and not replace_cover:
            return None, ["已存在封面（%s），未写入" % "、".join(sorted(existing))]
        title, uid = _opf_meta(opf_bytes)
        dir_ = opf["dir"]
        prefix = (dir_ + "/") if dir_ else ""
        img_full = prefix + "images/cover." + ext
        n = 1
        while img_full in names:
            n += 1
            img_full = prefix + "images/cover-%d.%s" % (n, ext)
        page_full = prefix + "cover.xhtml"
        by_id = {m["id"]: m for m in opf["manifest"]}
        spine0 = opf["spine"][0].get("idref") if opf["spine"] else ""
        first_href = (by_id.get(spine0) or {}).get("href") or ""
        has_cover_page = "cover" in posixpath.basename(first_href).lower()
        rel_img = posixpath.relpath(img_full, dir_ or ".")
        page = ('<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                '<meta charset="utf-8"/><title>封面</title></head>'
                '<body><div style="text-align:center">'
                '<img src="%s" alt="封面" style="max-width:100%%"/></div></body></html>\n'
                % _esc(rel_img)).encode("utf-8")
        new_opf = patch_opf(opf_bytes, dir_, title=title, uid=uid,
                            cover={"href": img_full, "id": "cover-image",
                                   "media_type": media},
                            cover_page=(None if has_cover_page else
                                        {"href": page_full, "id": "cover-page"}))
        repl = {img_full: data, opf["path"]: new_opf}
        expect = [img_full]
        if not has_cover_page:
            repl[page_full] = page
            expect.append(page_full)
        write_epub_variant(src, dst, replace=repl)
        probs = verify_epub(dst, expect=expect)
        if probs:
            os.remove(dst)
            return None, probs
        return dst, []
    except Exception as exc:
        return _fail(dst, exc)


def apply_both(src, items=None, image_path=None, dst=None, replace_cover=False):
    """目录 + 封面一次写入。"""
    dst = dst or default_dst(src, "已编辑")
    tmp = dst + ".toc.tmp.epub"
    if items:
        mid, probs = apply_toc(src, items, dst=tmp)
        if probs:
            return None, probs
        src = mid
    if image_path:
        out, probs = apply_cover(src, image_path, dst=dst,
                                 replace_cover=replace_cover)
        if items and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return out, probs
    if items:
        os.replace(tmp, dst)
    return dst, []


class MergeError(Exception):
    pass


def _natural_key(name):
    parts = re.split(r"(\d+)", name.lower())
    return [(0, int(p)) if p.isdigit() else (1, p) for p in parts if p != ""]


def sort_merge_inputs(paths, mode="name"):
    if mode == "size":
        return sorted(paths, key=lambda p: os.path.getsize(p))
    if mode == "mtime":
        return sorted(paths, key=lambda p: os.path.getmtime(p))
    return sorted(paths, key=lambda p: _natural_key(os.path.basename(p)))


def _open_epub_parts(path):
    try:
        z = zipfile.ZipFile(path)
    except Exception as exc:
        raise MergeError("%s 打不开：不是有效的 zip/EPUB（%s）"
                         % (os.path.basename(path), exc))
    if z.testzip() is not None:
        z.close()
        raise MergeError("%s 损坏：zip 条目 CRC 校验失败" % os.path.basename(path))
    names = set(z.namelist())
    if "META-INF/encryption.xml" in names:
        z.close()
        raise MergeError("%s 含 DRM（encryption.xml），无法合并"
                         % os.path.basename(path))
    try:
        opf_path = get_opf_path(z)
        opf = parse_opf(z, opf_path)
    except Exception as exc:
        z.close()
        raise MergeError("%s 不是有效 EPUB：读不出 OPF（%s）"
                         % (os.path.basename(path), exc))
    return z, opf_path, opf


def check_merge_inputs(paths):
    paths = [p for p in paths if p]
    if len(paths) < 2:
        raise MergeError("至少需要两本书才能合并（当前 %d 本）" % len(paths))
    for p in paths:
        if not os.path.exists(p):
            raise MergeError("文件不存在：%s" % p)
        if book_format(p) != "EPUB":
            raise MergeError("只支持 EPUB：%s 不是 EPUB" % os.path.basename(p))
    for p in paths:
        z, _opf_path, _opf = _open_epub_parts(p)
        z.close()
    return paths


MERGE_OPF = "EPUB/package.opf"


MERGE_NAV = "EPUB/nav.xhtml"


MERGE_NCX = "EPUB/toc.ncx"


MERGE_COVER = "EPUB/cover.jpg"


def _merge_container():
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles>\n'
            '    <rootfile full-path="EPUB/package.opf" '
            'media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n</container>\n')


def _book_language(z, opf):
    try:
        root = ET.fromstring(z.read(opf["path"]))
    except Exception:
        return "zh-CN"
    for el in root.iter():
        if local(el.tag) == "language" and (el.text or "").strip():
            return el.text.strip()
    return "zh-CN"


def _collect_book(z, opf, prefix):
    names = set(z.namelist())
    lower = {n.lower(): n for n in names}
    toc = []
    for it in read_toc_tree(z, opf):
        full = it.get("href") or ""
        if full and full not in names and opf["dir"]:
            alt = posixpath.join(opf["dir"], full)
            if alt not in names and alt.lower() in lower:
                alt = lower[alt.lower()]
            full = alt if alt in names else full
        if full in names and full.lower() not in lower:
            full = lower[full.lower()]
        if not full or full not in names:
            continue  # 悬空或空 href：合并后的目录里不放死链
        toc.append({"level": it["level"], "title": it["title"],
                    "href": prefix + "/" + full})
    first = None
    spine_ids = [s["idref"] for s in opf["spine"]]
    for m in opf["manifest"]:
        if m["id"] in spine_ids and m["href"]:
            full = posixpath.join(opf["dir"], m["href"]) if opf["dir"] else m["href"]
            full = lower.get(full.lower(), full)
            if full in names:
                first = prefix + "/" + full
                break
    if first is None and toc:
        first = toc[0]["href"]
    title, _uid = _opf_meta(z.read(opf["path"]))
    return {
        "prefix": prefix,
        "title": title or os.path.basename(prefix),
        "language": _book_language(z, opf),
        "toc": toc,
        "first": first,
    }


def _copy_book_into(zsrc, zout, prefix):
    for info in zsrc.infolist():
        name = info.filename
        if name == "mimetype" or name.startswith("META-INF/"):
            continue
        zi = zipfile.ZipInfo(prefix + "/" + name, date_time=info.date_time)
        zi.compress_type = info.compress_type
        # 流式复制，避免整本（可能是几百 MB）读进内存
        with zsrc.open(name) as fi, zout.open(zi, "w") as fo:
            shutil.copyfileobj(fi, fo, 1024 * 1024)


def _merge_manifest_items(books, zips_opfs):
    items = []
    for book, (z, opf) in zip(books, zips_opfs):
        for m in opf["manifest"]:
            if not m["href"]:
                continue
            full = posixpath.join(opf["dir"], m["href"]) if opf["dir"] else m["href"]
            if (m["media_type"] or "") == "application/oebps-package+xml":
                continue  # 子书的 OPF 不列入合并后的 manifest
            props_s = (m["properties"] or "").replace("nav", "").strip()
            props = (' properties="%s"' % props_s) if props_s else ""
            items.append((book["prefix"].replace("/", "-") + "-" + (m["id"] or "x"),
                          book["prefix"] + "/" + full,
                          m["media_type"] or "application/octet-stream", props))
    return items


def _merge_spine(books, zips_opfs):
    out = []
    for book, (z, opf) in zip(books, zips_opfs):
        for s in opf["spine"]:
            out.append(book["prefix"].replace("/", "-") + "-" + (s["idref"] or "x"))
    return out


def _nest(toc):
    """把扁平 (level,title,href) 列表转成嵌套节点。"""
    roots = []
    stack = []
    for it in toc:
        node = {"title": it["title"], "href": it["href"], "children": []}
        lv = max(1, it.get("level", 1))
        while stack and stack[-1][0] >= lv:
            stack.pop()
        if stack:
            stack[-1][1]["children"].append(node)
        else:
            roots.append(node)
        stack.append((lv, node))
    return roots


def _nav_ol(nodes, indent):
    pad = " " * indent
    out = [pad + "<ol>"]
    for n in nodes:
        out.append(pad + '  <li><a href="%s">%s</a>' % (_esc(n["href"]), _esc(n["title"])))
        if n["children"]:
            out.extend(_nav_ol(n["children"], indent + 4))
        out.append(pad + "  </li>")
    out.append(pad + "</ol>")
    return out


def build_merged_nav(books, title):
    body = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
            "<!DOCTYPE html>",
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-CN" xml:lang="zh-CN">',
            "<head><meta charset=\"UTF-8\"/><title>%s</title></head>" % _esc(title),
            "<body>",
            '<nav epub:type="toc" id="toc"><h1>%s</h1>' % _esc(title)]
    out = ["  <ol>"]
    for b in books:
        href = b["first"] or (b["toc"][0]["href"] if b["toc"] else "")
        if href:
            out.append('    <li><a href="%s">%s</a>' % (_esc(href), _esc(b["title"])))
        else:
            out.append('    <li><span>%s</span>' % _esc(b["title"]))
        kids = _nest(b["toc"])
        if kids:
            out.extend(_nav_ol(kids, 6))
        out.append("    </li>")
    out.append("  </ol>")
    return "\n".join(body + out + ["</nav>", "</body>", "</html>", ""])


def build_merged_ncx(books, title, uid):
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">',
           '  <head><meta name="dtb:uid" content="%s"/></head>' % _esc(uid),
           "  <docTitle><text>%s</text></docTitle>" % _esc(title),
           "  <navMap>"]
    order = [0]

    def emit(node, depth):
        order[0] += 1
        pad = "  " * (depth + 1)
        out.append('%s<navPoint id="np%d" playOrder="%d">' % (pad, order[0], order[0]))
        out.append("%s  <navLabel><text>%s</text></navLabel>"
                   % (pad, _esc(node["title"])))
        if node["href"]:
            out.append('%s  <content src="%s"/>' % (pad, _esc(node["href"])))
        for ch in node["children"]:
            emit(ch, depth + 1)
        out.append("%s</navPoint>" % pad)

    for b in books:
        href = b["first"] or (b["toc"][0]["href"] if b["toc"] else "")
        emit({"title": b["title"], "href": href, "children": _nest(b["toc"])}, 0)
    out.append("  </navMap>")
    out.append("</ncx>")
    return "\n".join(out) + "\n"


def build_merged_opf(books, items, spine_ids, title, language, cover_href=None):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
             'unique-identifier="pub-id">',
             '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">',
             '    <dc:identifier id="pub-id">%s</dc:identifier>' % _esc(_merge_uid(title)),
             '    <dc:title>%s</dc:title>' % _esc(title),
             '    <dc:language>%s</dc:language>' % _esc(language)]
    if cover_href:
        lines.append('    <meta name="cover" content="cover-image"/>')
    lines += ['  </metadata>', '  <manifest>',
              '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
              '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>']
    if cover_href:
        lines.append('    <item id="cover-image" href="cover.jpg" media-type="image/jpeg" '
                     'properties="cover-image"/>')
    for iid, href, mt, props in items:
        lines.append('    <item id="%s" href="%s" media-type="%s"%s/>'
                     % (_esc(iid), _esc(href), _esc(mt), props))
    lines += ["  </manifest>", '  <spine toc="ncx">']
    for iid in spine_ids:
        lines.append('    <itemref idref="%s"/>' % _esc(iid))
    lines += ["  </spine>"]
    if books and books[0].get("first"):
        lines.append('  <guide><reference type="text" title="正文" href="%s"/></guide>'
                     % _esc(books[0]["first"]))
    lines.append("</package>")
    return "\n".join(lines) + "\n"


def _merge_uid(seed):
    import hashlib as _h
    h = _h.sha1(("%s|%s" % (seed, time.time())).encode("utf-8")).hexdigest()
    return "urn:uuid:%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:32])


def default_merge_dst(paths, title, out_dir=None):
    base = out_dir or os.path.dirname(os.path.abspath(paths[0]))
    name = "合并_%s.epub" % (re.sub(r'[\\/:*?"<>|]+', "_", title or "合集").strip() or "合集")
    dst = os.path.join(base, name)
    n = 2
    while os.path.exists(dst):
        safe = re.sub(r'[\\/:*?"<>|]+', "_", title or "合集").strip() or "合集"
        dst = os.path.join(base, "合并_%s_%d.epub" % (safe, n))
        n += 1
    return dst


def _merge_book_prefix(prefix, names):
    """子包内已有 book_NNN 目录时整体再包一层，避免与外层撞名。"""
    for name in names:
        if any(part.startswith("book_") for part in name.split("/")):
            return prefix + "/book"
    return prefix


def _notify(progress, done, total, text):
    """进度回调兼容层：新回调收 (done, total, text)，老回调只收 text。"""
    if progress is None:
        return
    try:
        n = len([p for p in inspect.signature(progress).parameters.values()
                 if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
    except (TypeError, ValueError):
        n = 1
    if n >= 3:
        progress(done, total, text)
    elif n == 2:
        progress(done, total)
    else:
        progress(text)


def merge_books(paths, title, dst=None, cover=None, order="name", progress=None):
    """把多本 EPUB 合成一本，布局与 EpubMerge 同构。返回输出路径。"""
    paths = check_merge_inputs(paths)
    if isinstance(order, str):
        paths = sort_merge_inputs(paths, order)
    else:
        paths = list(paths)
    if not dst:
        dst = default_merge_dst(paths, title)
    part = dst + ".part"
    books, pairs, cover_bytes = [], [], None
    try:
        with zipfile.ZipFile(part, "w") as zout:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zout.writestr(info, "application/epub+zip")
            zout.writestr("META-INF/container.xml", _merge_container())
            language = "zh-CN"
            for i, p in enumerate(paths, 1):
                _notify(progress, i, len(paths),
                        "正在复制第 %d/%d 本：%s"
                        % (i, len(paths), os.path.basename(p)))
                z, opf_path, opf = _open_epub_parts(p)
                try:
                    href_prefix = _merge_book_prefix("book_%03d" % i,
                                                     z.namelist())
                    copy_prefix = "EPUB/" + href_prefix
                    book = _collect_book(z, opf, href_prefix)
                    if i == 1:
                        language = book["language"]
                        if cover is None:
                            covers = sorted(cover_hrefs(z, opf))
                            if covers:
                                try:
                                    cover_bytes = z.read(covers[0])
                                except Exception:
                                    cover_bytes = None
                    _copy_book_into(z, zout, copy_prefix)
                    books.append(book)
                    pairs.append((z, opf))
                finally:
                    z.close()
            if cover == "none":
                cover_bytes = None
            elif cover:
                cover_bytes, _ext, _mt = prepare_cover(cover)
            items = _merge_manifest_items(books, pairs)
            spine_ids = _merge_spine(books, pairs)
            if cover_bytes:
                zout.writestr(MERGE_COVER, cover_bytes)
            zout.writestr(MERGE_NAV, build_merged_nav(books, title or "合集"))
            uid = _merge_uid(title or "合集")
            zout.writestr(MERGE_NCX, build_merged_ncx(books, title or "合集", uid))
            zout.writestr(MERGE_OPF,
                          build_merged_opf(books, items, spine_ids,
                                           title or "合集", language,
                                           cover_href="cover.jpg" if cover_bytes else None))
        _notify(progress, len(paths), len(paths), "正在自检…")
        probs = verify_epub(part)
        if probs:
            raise MergeError("合并结果自检未通过：" + "；".join(probs[:3]))
        os.replace(part, dst)
        return dst
    except Exception:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        raise


_TOC_LI_PAIR = re.compile(
    r'<li><a href="([^"]+)">([^<]*)</a>\s*<ol>\s*<li><a href="\1">\2</a></li>'
    r'\s*</ol>\s*</li>', re.S)


_TOC_NP_PAIR = re.compile(
    r'<navPoint id="([^"]*)" playOrder="\d+">\s*<navLabel><text>([^<]*)</text>'
    r'</navLabel>\s*<content src="([^"]+)"/>\s*<navPoint id="[^"]*" playOrder="\d+">\s*'
    r'<navLabel><text>\2</text></navLabel>\s*<content src="\3"/>\s*</navPoint>'
    r'\s*</navPoint>', re.S)


_TOC_NAVPOINT = re.compile(r'<navPoint id="([^"]*)" playOrder="(\d+)">')


def _fix_nav_dups(text):
    """折叠 `父项与唯一子项同标题同 href` 的 li，返回 (新文本, 折叠条数)。"""
    n = len(_TOC_LI_PAIR.findall(text))
    if not n:
        return text, 0
    return _TOC_LI_PAIR.sub(r'<li><a href="\1">\2</a></li>', text), n


def _fix_ncx_dups(text):
    """折叠嵌套 navPoint，重排 playOrder，dtb:depth 归 1。"""
    n = len(_TOC_NP_PAIR.findall(text))
    if n:
        text = _TOC_NP_PAIR.sub(
            lambda m: '<navPoint id="%s" playOrder="0"><navLabel><text>%s</text>'
                      '</navLabel><content src="%s"/></navPoint>'
                      % (m.group(1), m.group(2), m.group(3)), text)
        counter = [0]

        def renum(m):
            counter[0] += 1
            return '<navPoint id="%s" playOrder="%d">' % (m.group(1), counter[0])

        text = _TOC_NAVPOINT.sub(renum, text)
        text = re.sub(r'(<meta name="dtb:depth" content=")\d+(")', r'\g<1>1\g<2>', text)
    return text, n


def fix_toc_dups(src, dst=None):
    """修复合并后目录重复（父项与子项同标题同链接）。

    返回 (输出路径, nav 折叠条数, ncx 折叠条数)。没发现重复时输出为 None，
    且不写任何文件。
    """
    if book_format(src) != "EPUB":
        raise TocListError("只支持 EPUB，当前文件是 %s" % book_format(src))
    changed = {}
    nav_n = ncx_n = 0
    with zipfile.ZipFile(src) as z:
        _opf = parse_opf(z, get_opf_path(z))
        _ncx = resolve(_opf["dir"], _opf.get("ncx_href") or "") if _opf.get("ncx_href") else ""
        ncx_names = {n for n in z.namelist() if n == _ncx}
        for name in z.namelist():
            if name.endswith("nav.xhtml"):
                text = z.read(name).decode("utf-8", "replace")
                fixed, n = _fix_nav_dups(text)
                if n:
                    changed[name] = fixed.encode("utf-8")
                    nav_n += n
            elif name in ncx_names or name.lower().endswith(".ncx"):
                text = z.read(name).decode("utf-8", "replace")
                fixed, n = _fix_ncx_dups(text)
                if n:
                    changed[name] = fixed.encode("utf-8")
                    ncx_n += n
    if not changed:
        return None, 0, 0
    out = dst or default_dst(src, "修复")
    write_epub_variant(src, out, replace=changed)
    probs = verify_epub(out)
    if probs:
        try:
            os.remove(out)
        except OSError:
            pass
        raise TocListError("修复后自检没通过：%s" % "；".join(probs[:5]))
    return out, nav_n, ncx_n


def _toc_indent_cols(line):
    cols = 0
    for ch in line:
        if ch == "\t":
            cols += 2
        elif ch in (" ", "\u3000"):
            cols += 1
        else:
            break
    return cols


def _fail(dst, exc):
    if dst and os.path.exists(dst):
        try:
            os.remove(dst)
        except OSError:
            pass
    return None, ["%s: %s" % (type(exc).__name__, exc)]
