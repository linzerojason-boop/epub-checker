# -*- coding: utf-8 -*-
"""EPUB / MOBI / PDF 解析层。

只做「把文件读成数据结构」：zip、OPF、spine、nav/ncx、图片指纹。
不含判定逻辑，也不写盘。
"""
import contextlib
import io
import os
import posixpath
import re
import struct
import zipfile
from heapq import nsmallest
from io import BytesIO
import xml.etree.ElementTree as ET

try:
    from PIL import Image
except Exception:          # 没有 Pillow 时退化成纯哈希，不致命
    Image = None

try:
    import numpy as _NP
except Exception:
    _NP = None


DH_THRESHOLD = 10   # dHash 汉明距离阈值: <=10 视为同一页

TEXT_MIN_AVG = 200  # 文本文件平均字符数: <200 => 图片版(漫画)

MIN_PAGE_SIDE = 300  # 页图最短边阈值: 小于此值视为缩略图/装饰图, 不计入页数

K_CAND = 16           # 纯Python回退: dHash 前 K 个候选

MAD_THRESHOLD = 8.0

def local(tag):
    """去掉 XML 命名空间，取本地名。"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag

def resolve(base_dir, href):
    """把相对 href 解析成 zip 内规范路径(去 fragment、去 ./ 与 ../)。"""
    if href is None:
        return None
    href = href.split("#")[0].strip()
    if not href:
        return None
    return posixpath.normpath(posixpath.join(base_dir, href))

def rel_to_opf(opf, path):
    """把 zip 内绝对路径转成相对 OPF 目录的路径(用于跨书匹配)。"""
    base = opf["dir"]
    if base and path.startswith(base + "/"):
        return path[len(base) + 1:]
    return path

def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d B" % n) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%d B" % n

def is_text_media(mt):
    mt = (mt or "").lower()
    return ("html" in mt) or ("xhtml" in mt) or ("oeb" in mt) or ("xml" in mt)

def is_image_media(mt):
    return (mt or "").lower().startswith("image/")

def get_opf_path(z):
    """从 META-INF/container.xml 读主 OPF 的 full-path。"""
    data = z.read("META-INF/container.xml")
    root = ET.fromstring(data)
    for el in root.iter():
        if local(el.tag) == "rootfile":
            return el.get("full-path")
    raise ValueError("container.xml 里找不到 rootfile")

def parse_opf(z, opf_path):
    """解析 OPF，返回 version / manifest / spine / nav / ncx。"""
    data = z.read(opf_path)
    root = ET.fromstring(data)
    version = root.get("version", "?")
    manifest = []
    spine = []
    for el in root.iter():
        ln = local(el.tag)
        if ln == "item":
            manifest.append({
                "id": el.get("id"),
                "href": el.get("href"),
                "media_type": el.get("media-type", ""),
                "properties": el.get("properties", ""),
                "fallback": el.get("fallback", ""),
            })
        elif ln == "itemref":
            spine.append({
                "idref": el.get("idref"),
                "linear": el.get("linear", "yes"),
                "properties": el.get("properties", ""),
            })
    nav_href = None
    ncx_href = None
    for m in manifest:
        if "nav" in m["properties"]:
            nav_href = m["href"]
        if m["media_type"] == "application/x-dtbncx+xml":
            ncx_href = m["href"]
    return {
        "path": opf_path,
        "dir": posixpath.dirname(opf_path),
        "version": version,
        "manifest": manifest,
        "spine": spine,
        "nav_href": nav_href,
        "ncx_href": ncx_href,
    }

def opf_text_images(z, opf):
    """按 manifest 的 media-type 分出文本文件集与图片文件集。"""
    base = opf["dir"]
    texts, images = set(), set()
    for m in opf["manifest"]:
        href = resolve(base, m["href"])
        if href is None:
            continue
        if is_text_media(m["media_type"]):
            texts.add(href)
        elif is_image_media(m["media_type"]):
            images.add(href)
    return texts, images

def normalize_text(raw):
    txt = raw.decode("utf-8", errors="replace")
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()

def extract_toc(z, href):
    """返回 {href(相对形式): 标题}。"""
    result = {}
    if not href or href not in z.namelist():
        return result
    raw = z.read(href)
    try:
        root = ET.fromstring(raw)
    except Exception:
        return result
    for el in root.iter():
        ln = local(el.tag)
        if ln == "a":  # EPUB3 nav.xhtml
            h = el.get("href")
            if h:
                title = "".join(el.itertext()).strip()
                result[h.split("#")[0]] = title
        elif ln == "navPoint":  # EPUB2 ncx
            title = ""
            src = None
            for child in el:
                cln = local(child.tag)
                if cln == "navLabel":
                    for t in child.iter():
                        if local(t.tag) == "text":
                            title = (t.text or "").strip()
                elif cln == "content":
                    src = child.get("src")
            if src:
                result[src.split("#")[0]] = title
    return result

def _nav_tree(root):
    """从 nav.xhtml 的 <nav epub:type=toc> 里取 (title, href, level) 列表。"""
    ops = "{http://www.idpf.org/2007/ops}type"
    nav = None
    for el in root.iter():
        if local(el.tag) == "nav" and (el.get(ops) or el.get("type") or "") == "toc":
            nav = el
            break
    if nav is None:
        for el in root.iter():
            if local(el.tag) == "nav":
                nav = el
                break
    out = []

    def walk(node, level):
        for child in node:
            if local(child.tag) != "li":
                continue
            title, href = "", None
            frag = ""
            sub = None
            for gc in child:
                ln = local(gc.tag)
                if ln in ("a", "span"):
                    if title == "":
                        title = "".join(gc.itertext()).strip()
                    if ln == "a" and href is None and gc.get("href"):
                        raw = gc.get("href")
                        href, _, frag = raw.partition("#")
                elif ln in ("ol", "ul"):
                    sub = gc
            out.append({"href": href, "frag": frag, "title": title, "level": level})
            if sub is not None:
                walk(sub, level + 1)

    if nav is not None:
        for el in nav.iter():
            if local(el.tag) in ("ol", "ul"):
                walk(el, 1)
                break
    return out

def _ncx_tree(root):
    """从 toc.ncx 的 navMap 取 (title, src, level)，按 playOrder 排序。"""
    nav_map = None
    for el in root.iter():
        if local(el.tag) == "navMap":
            nav_map = el
            break
    out = []

    def walk(node, level):
        for child in node:
            if local(child.tag) != "navPoint":
                continue
            title, src, frag = "", None, ""
            for gc in child:
                ln = local(gc.tag)
                if ln == "navLabel":
                    for t in gc.iter():
                        if local(t.tag) == "text":
                            title = (t.text or "").strip()
                            break
                elif ln == "content" and gc.get("src"):
                    raw = gc.get("src")
                    src, _, frag = raw.partition("#")
            try:
                order = float(child.get("playOrder") or 0)
            except ValueError:
                order = 0.0
            out.append({"order": order, "href": src, "frag": frag,
                        "title": title, "level": level})
            walk(child, level + 1)

    if nav_map is not None:
        walk(nav_map, 1)
    out.sort(key=lambda t: t["order"])
    return out

def read_toc_tree(z, opf):
    """按 OPF 的 nav / ncx 读出带层级的目录树。"""
    for key in ("nav_href", "ncx_href"):
        href = opf.get(key)
        if not href:
            continue
        full = resolve(opf["dir"], href)
        if not full or full not in z.namelist():
            continue
        try:
            root = ET.fromstring(z.read(full))
        except Exception:
            continue
        base = posixpath.dirname(full)
        if key == "nav_href":
            tree = _nav_tree(root)
        else:
            tree = [{"href": t["href"], "frag": t.get("frag") or "",
                     "title": t["title"], "level": t["level"]}
                    for t in _ncx_tree(root)]
        # 目录里的 href 相对 nav/ncx 自己所在的目录（可能不是 OPF 目录），统一转成 zip 内的全路径
        for t in tree:
            h = t.get("href") or ""
            t["href"] = posixpath.normpath(posixpath.join(base, h)) if h else ""
        return tree
    return []

def spine_docs(z, opf):
    """spine 里的正文文档（相对 OPF 目录），排除 nav / ncx / 目录页。"""
    by_id = {}
    for m in opf["manifest"]:
        by_id[m["id"]] = m
    skip = set()
    for m in opf["manifest"]:
        props = (m["properties"] or "").lower()
        href = (m["href"] or "").lower()
        if "nav" in props or href.endswith("nav.xhtml") or href.endswith("toc.ncx") \
                or href.endswith("toc.xhtml") or href.endswith("cover.xhtml"):
            skip.add(m["id"])
    docs = []
    for ref in opf["spine"]:
        m = by_id.get(ref["idref"])
        if not m or m["id"] in skip:
            continue
        if not is_text_media(m["media_type"]):
            continue
        docs.append({"id": m["id"], "href": m["href"],
                     "linear": ref.get("linear", "yes")})
    return docs

def image_refs(z, texts, images):
    """返回 {图片路径: [引用它的文本文件列表]}。"""
    refs = {img: [] for img in images}
    for tf in texts:
        if tf not in z.namelist():
            continue
        txt = z.read(tf).decode("utf-8", errors="replace")
        for m in re.finditer(r'''<img[^>]*\bsrc\s*=\s*["']([^"']+)["']''', txt, re.I):
            src = m.group(1)
            r = resolve(posixpath.dirname(tf), src)
            if r in refs:
                refs[r].append(tf)
    return refs

def cover_hrefs(z, opf):
    """返回封面图片在 zip 内的路径集合(来自 OPF 的 meta cover 与 item properties=cover-image)。"""
    covers = set()
    try:
        data = z.read(opf["path"])
        root = ET.fromstring(data)
    except Exception:
        return covers
    cover_ids = set()
    for el in root.iter():
        if local(el.tag) == "meta" and el.get("name") == "cover":
            c = el.get("content")
            if c:
                cover_ids.add(c)
    for m in opf["manifest"]:
        if m["id"] in cover_ids or "cover-image" in m["properties"]:
            h = resolve(opf["dir"], m["href"])
            if h:
                covers.add(h)
    return covers

def css_url_refs(z):
    """返回所有 CSS 文件里 url(...) 引用的 zip 内路径集合。"""
    refs = set()
    for cf in z.namelist():
        if not cf.lower().endswith(".css"):
            continue
        txt = z.read(cf).decode("utf-8", errors="replace")
        for m in re.finditer(r'''url\s*\(\s*["']?([^"')]+)["']?\s*\)''', txt, re.I):
            url = m.group(1).strip().split("#")[0].split("?")[0]
            if url:
                refs.add(resolve(posixpath.dirname(cf), url))
    return refs

def is_mobi_file(path):
    return path.lower().endswith((".mobi", ".azw", ".azw3", ".prc"))

def is_pdf_file(path):
    return path.lower().endswith(".pdf")

def mobi_records(path):
    """解析 PalmDB, 返回按文件顺序的记录字节列表。"""
    with open(path, "rb") as f:
        data = f.read()
    nrec = struct.unpack(">H", data[76:78])[0]
    starts = []
    for i in range(nrec):
        starts.append(struct.unpack(">I", data[78 + i * 8: 82 + i * 8])[0])
    recs = []
    for i in range(nrec):
        s = starts[i]
        e = starts[i + 1] if i + 1 < nrec else len(data)
        recs.append(data[s:e])
    return recs

def mobi_first_image_index(path):
    recs = mobi_records(path)
    if not recs:
        return 0
    rec0 = recs[0]
    if len(rec0) < 0x70 or rec0[16:20] != b"MOBI":
        return 0
    return struct.unpack(">I", rec0[16 + 0x5C: 16 + 0x5C + 4])[0]

def mobi_images(path):
    """从图片版 MOBI 提取 JPEG 页(按 FF D8 切分), 过滤缩略图, 返回 JPEG 字节列表。"""
    recs = mobi_records(path)
    first = mobi_first_image_index(path)
    if first <= 0 or first >= len(recs):
        return []
    blob = b"".join(recs[first:])
    parts = blob.split(b"\xff\xd8")
    return [b"\xff\xd8" + p for p in parts[1:] if p and _is_page_jpeg(b"\xff\xd8" + p)]

def _jpeg_size(data):
    """解析 JPEG 的 SOF 标记, 返回 (宽, 高) 或 None(失败)。"""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 <= n:
                h = struct.unpack(">H", data[i + 5:i + 7])[0]
                w = struct.unpack(">H", data[i + 7:i + 9])[0]
                return (w, h)
            return None
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2  # 无长度字段的标记
            continue
        seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
        if seglen < 2:
            return None
        i += 2 + seglen
    return None

def _is_page_jpeg(b):
    """按尺寸过滤: 无法解析则保留, 尺寸过小(缩略图)则丢弃。"""
    sz = _jpeg_size(b)
    if sz is None:
        return True
    return sz[0] >= MIN_PAGE_SIDE and sz[1] >= MIN_PAGE_SIDE

def pdf_page_count(path):
    """粗略统计 PDF 页数: 数 /Type /Page 对象(/Pages 不计数)。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return 0
    return len(re.findall(rb"/Type\s*/Page(?![sA-Za-z0-9_])", data))

def pdf_images(path):
    """从 PDF 提取内嵌 JPEG(DCTDecode) 页图字节, 过滤缩略图。返回 JPEG 字节列表。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return []
    out = []
    pos = 0
    n = len(data)
    while True:
        s = data.find(b"stream", pos)
        if s < 0:
            break
        j = s + 6
        if j < n and data[j] == 0x0D:
            j += 1
            if j < n and data[j] == 0x0A:
                j += 1
        elif j < n and data[j] == 0x0A:
            j += 1
        e = data.find(b"endstream", j)
        if e < 0:
            break
        head = data[max(0, s - 1024):s]
        if (b"/Subtype" in head and b"/Image" in head
                and (b"/DCTDecode" in head or b"/DCT" in head)):
            raw = data[j:e].rstrip(b"\r\n\t ")
            if raw[:2] == b"\xff\xd8" and _is_page_jpeg(raw):
                out.append(raw)
        pos = e + 9
    return out

def dhash(img):
    """9x8 灰度差分哈希, 返回 64 位整数。"""
    g = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(g.getdata())
    h = 0
    for y in range(8):
        for x in range(8):
            h = (h << 1) | (1 if px[y * 9 + x] > px[y * 9 + x + 1] else 0)
    return h

def hamming(a, b):
    return bin(a ^ b).count("1")

def _thumb(img):
    """32x32 灰度缩略图 bytes(1024字节), 用于像素 MAD 确认。"""
    return img.convert("L").resize((32, 32), Image.LANCZOS).tobytes()

def _mad(a, b):
    """两个等长灰度 bytes 的平均绝对差(0-255)。"""
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)

def _find_match(th, dh, valid, np_arr):
    """在 valid[(j,dhash,thumb)] 中找 MAD 最小的候选, 返回 (best_j, best_mad, best_h)。"""
    if not valid:
        return -1, 255.0, 1 << 30
    if np_arr is not None:
        t = _NP.frombuffer(th, dtype=_NP.uint8).reshape(1, -1)
        diff = _NP.abs(np_arr.astype(_NP.int16) - t.astype(_NP.int16))
        mads = diff.mean(axis=1)
        k = int(mads.argmin())
        return valid[k][0], float(mads[k]), hamming(dh, valid[k][1])
    cands = []
    for j, cdh, cth in valid:
        h = hamming(dh, cdh)
        if len(cands) < K_CAND:
            heapq.heappush(cands, (-h, j, cth))
        elif h < -cands[0][0]:
            heapq.heapreplace(cands, (-h, j, cth))
    best_j = -1
    best_mad = 255.0
    best_h = 1 << 30
    for h_neg, j, cth in cands:
        mad = _mad(th, cth)
        if mad < best_mad:
            best_mad = mad
            best_h = -h_neg
            best_j = j
    return best_j, best_mad, best_h
