# -*- coding: utf-8 -*-
"""检查与比对层：单本检查、两本比对、页面图比对、合集检查、任务调度。

只依赖 epub_parse，不依赖 GUI。所有名字经 epub_core 门面原样再导出。
"""
import hashlib
import heapq
import os
import posixpath
import queue
import re
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import zipfile
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
try:
    from PIL import Image
except Exception:
    Image = None
try:
    import numpy as _NP
except Exception:
    _NP = None

__version__ = "3.12"

_KEY_MU = {}  # 每文件 singleflight 锁表，见 _cache_key

PAGE_FULL_COVERAGE = 0.99


PAGE_CONTAIN_COVERAGE = 0.95   # 覆盖率到这个份上就按「一致」报, 剩下的零头多半是渲染/裁边差异


PAGE_DUP_MAD = 2.0         # 重复页判定: 只认几乎同一张(比 MAD_THRESHOLD 严得多)   # 32x32 灰度平均绝对差阈值: <=此值视为同一页


TEXT_MIN_MEANINGFUL = 200


IN_COLL_COVERAGE = 0.6   # 基准内容 >=60% 在对方找到, 且没有实质文件落空 => 合集内含


TEXT_MIN_BOOK = 1000       # 一本书的实质文字少于这个数, 文字比对没有意义


PART_COVERAGE = 0.3      # >=30% => 部分一致; 低于此值一律判"存在差异"


FMT_BY_EXT = {".epub": "EPUB", ".mobi": "MOBI", ".azw": "AZW",
              ".azw3": "AZW3", ".prc": "PRC", ".pdf": "PDF"}


STATUS_WAIT = "待执行"


STATUS_OK = "一致"


STATUS_DIFF = "不一致"


STATUS_PART = "部分一致"


STATUS_IN_COLL = "合集内含"


STATUS_CANNOT = "无法比对"


STATUS_FAIL = "失败"


STATUS_CANCEL = "已取消"


STATUS_SKIP = "跳过"


ALL_STATUS = (STATUS_OK, STATUS_IN_COLL, STATUS_DIFF, STATUS_PART,
              STATUS_CANNOT, STATUS_FAIL, STATUS_CANCEL, STATUS_SKIP)


STATUS_TEXT = {
    STATUS_WAIT: "… 待执行",
    STATUS_OK: "✓ 一致",
    STATUS_IN_COLL: "≈ 合集内含（内容一致）",
    STATUS_DIFF: "✗ 存在差异",
    STATUS_PART: "△ 部分一致",
    STATUS_CANNOT: "· 无法比对",
    STATUS_FAIL: "⛔ 失败",
    STATUS_CANCEL: "— 已取消",
    STATUS_SKIP: "⏭ 跳过（同一文件）",
}


STATUS_TAG = {
    STATUS_WAIT: "wait",
    STATUS_OK: "ok",
    STATUS_IN_COLL: "incoll",
    STATUS_DIFF: "diff",
    STATUS_PART: "part",
    STATUS_CANNOT: "cannot",
    STATUS_FAIL: "fail",
    STATUS_CANCEL: "skip",
    STATUS_SKIP: "skip",
}


STATUS_COLOR = {
    STATUS_WAIT: "#b0b0b0",
    STATUS_OK: "#7bd88f",
    STATUS_IN_COLL: "#66d9ef",
    STATUS_DIFF: "#ff8a80",
    STATUS_PART: "#ffd166",
    STATUS_CANNOT: "#9e9e9e",
    STATUS_FAIL: "#ff6b6b",
    STATUS_CANCEL: "#9e9e9e",
    STATUS_SKIP: "#9e9e9e",
}


def detect_book(path):
    """返回 (kind, info)。kind = 'image' | 'text'。"""
    if is_mobi_file(path):
        imgs = mobi_images(path)
        if imgs:
            return "image", "MOBI 图片版, %d 页" % len(imgs)
        return "text", "MOBI (未识别到图片; 文字版 MOBI 暂不支持比对)"
    if is_pdf_file(path):
        imgs = pdf_images(path)
        pages = pdf_page_count(path)
        if imgs and len(imgs) >= max(2, pages // 2):
            return "image", "PDF 图片版, %d 页(内嵌图 %d 张)" % (pages, len(imgs))
        return "text", "PDF 文字版, %d 页 (逐字比对需 PyMuPDF, 暂不支持)" % pages
    try:
        z = zipfile.ZipFile(path)
        opf = parse_opf(z, get_opf_path(z))
        texts, images = opf_text_images(z, opf)
    except Exception as e:
        return "text", "无法解析: %s" % e
    total = 0
    for p in texts:
        if p in z.namelist():
            total += len(normalize_text(z.read(p)))
    avg = total / max(1, len(texts))
    if images and avg < TEXT_MIN_AVG:
        return "image", "EPUB 图片版(漫画), %d 页" % len(images)
    return "text", "EPUB 文字版, %d 个文本文件" % len(texts)


def _hash_blob(data):
    """单张图片字节 => (dhash, thumb)；解不开就 (None, None)。"""
    if Image is None:
        return (None, None)
    try:
        im = Image.open(BytesIO(data))
        return (dhash(im), _thumb(im))
    except Exception:
        return (None, None)


def _hash_stream(pairs, window=None):
    """pairs 是可迭代的 (label, 取字节函数)；解码在多线程里跑，顺序不变。

    读字节仍在调用线程里做（zip 不宜并发读），所以用有界窗口限制内存。
    """
    window = window or DECODE_WORKERS * 4
    out = []
    pending = []
    try:
        for label, getter in pairs:
            pending.append((label, _POOL.submit(_hash_blob, getter())))
            if len(pending) >= window:
                lab, fut = pending.pop(0)
                d, t = fut.result()
                out.append((lab, d, t))
        for lab, fut in pending:
            d, t = fut.result()
            out.append((lab, d, t))
    finally:
        for _lab, fut in pending:
            fut.cancel()
    return out


def book_image_dhashes(path):
    """图片版书籍 => [(label, dhash|None, thumb|None)]。label 为 zip 路径或 mobi#N / pdf#N。"""
    if is_mobi_file(path):
        if Image is None:
            return []
        return _hash_stream((("mobi#%d" % i, (lambda b=b: b))
                             for i, b in enumerate(mobi_images(path), 1)))
    if is_pdf_file(path):
        if Image is None:
            return []
        return _hash_stream((("pdf#%d" % i, (lambda b=b: b))
                             for i, b in enumerate(pdf_images(path), 1)))
    try:
        z = zipfile.ZipFile(path)
        opf = parse_opf(z, get_opf_path(z))
        texts, images = opf_text_images(z, opf)
        covers = cover_hrefs(z, opf)
    except Exception:
        return []
    names = [p for p in sorted(images) if p not in covers]
    if Image is None:
        z.close()
        return [(p, None, None) for p in names]
    try:
        return _hash_stream(((p, (lambda p=p: z.read(p))) for p in names))
    finally:
        z.close()


def book_fulltext(path):
    """文字版 EPUB => 归一化后的全文(拼接)。PDF 文字版暂不支持, 返回空串。"""
    if is_pdf_file(path):
        return ""
    try:
        z = zipfile.ZipFile(path)
        opf = parse_opf(z, get_opf_path(z))
        texts, images = opf_text_images(z, opf)
    except Exception:
        return ""
    parts = []
    for p in sorted(texts):
        if p in z.namelist():
            parts.append(normalize_text(z.read(p)))
    return "\n".join(parts)


def check_single(path):
    L = []
    problems = 0
    L.append("=" * 62)
    L.append("电子书完整性检查报告 (单文件)")
    L.append("=" * 62)
    L.append("文件 : %s" % path)
    L.append("大小 : %s" % fmt_size(os.path.getsize(path)))

    if is_mobi_file(path):
        try:
            recs = mobi_records(path)
            imgs = mobi_images(path)
        except Exception as e:
            L.append("")
            L.append("结论 : 无法解析 MOBI: %s" % e)
            return "\n".join(L), 1
        L.append("格式 : MOBI/AZW (PalmDB)")
        L.append("记录数 : %d" % len(recs))
        L.append("图片页 : %d 张" % len(imgs))
        L.append("")
        L.append("结论 : %s" % ("图片版 MOBI, 结构正常" if imgs else "未识别到图片(可能为文字版 MOBI)"))
        return "\n".join(L), 0

    if is_pdf_file(path):
        pages = pdf_page_count(path)
        imgs = pdf_images(path)
        L.append("格式 : PDF")
        L.append("页数(粗略) : %d" % pages)
        L.append("内嵌 JPEG 页图 : %d 张" % len(imgs))
        L.append("")
        if pages == 0:
            L.append("结论 : 无法解析 PDF (文件可能损坏或加密)")
            return "\n".join(L), 1
        if imgs and len(imgs) >= max(2, pages // 2):
            L.append("结论 : 图片版 PDF, 结构正常 (文字提取/比对需 PyMuPDF)")
        else:
            L.append("结论 : 文字版 PDF, 结构正常 (文字提取/比对需 PyMuPDF)")
        return "\n".join(L), 0

    z = zipfile.ZipFile(path)
    names = set(z.namelist())
    L.append("zip 条目 : %d 个" % len(names))

    bad = z.testzip()
    if bad is None:
        L.append("CRC 校验 : 通过 (无损坏条目)")
    else:
        problems += 1
        L.append("CRC 校验 : 失败 - 损坏条目: %s" % bad)

    try:
        opf_path = get_opf_path(z)
    except Exception as e:
        L.append("结构解析 : 失败 - %s" % e)
        L.append("")
        L.append("结论 : 不是有效的 EPUB (缺 container.xml 或 rootfile)")
        return "\n".join(L), 1

    opf = parse_opf(z, opf_path)
    L.append("container.xml -> %s" % opf_path)
    L.append("OPF 版本 : %s" % opf["version"])
    L.append("manifest : %d 项" % len(opf["manifest"]))
    L.append("spine : %d 项" % len(opf["spine"]))
    L.append("EPUB3 nav : %s" % (opf["nav_href"] or "(无)"))
    L.append("EPUB2 ncx : %s" % (opf["ncx_href"] or "(无)"))

    texts, images = opf_text_images(z, opf)
    L.append("文本文件 : %d 个 / 图片 : %d 个" % (len(texts), len(images)))

    # manifest 缺文件
    missing = []
    man_by_id = {}
    for m in opf["manifest"]:
        man_by_id[m["id"]] = m
        h = resolve(opf["dir"], m["href"])
        if h and h not in names:
            missing.append("%s -> %s" % (m["id"], m["href"]))
    if missing:
        problems += 1
        L.append("")
        L.append("[问题] manifest 缺失文件 (%d):" % len(missing))
        for x in missing:
            L.append("   " + x)
    else:
        L.append("manifest 缺文件 : 0")

    # spine 无效引用
    bad_spine = [s["idref"] for s in opf["spine"] if s["idref"] not in man_by_id]
    if bad_spine:
        problems += 1
        L.append("[问题] spine 无效引用: " + ", ".join(bad_spine))
    else:
        L.append("spine 无效引用 : 0")

    # 断链图片
    broken = []
    for tf in texts:
        if tf not in names:
            continue
        txt = z.read(tf).decode("utf-8", errors="replace")
        for m in re.finditer(r'''<img[^>]*\bsrc\s*=\s*["']([^"']+)["']''', txt, re.I):
            src = m.group(1)
            r = resolve(posixpath.dirname(tf), src)
            if r and r not in names:
                broken.append("%s 引用 %s" % (tf, src))
    if broken:
        problems += 1
        L.append("")
        L.append("[问题] 断链图片 (%d):" % len(broken))
        for x in broken[:50]:
            L.append("   " + x)
        if len(broken) > 50:
            L.append("   ... 共 %d 处" % len(broken))
    else:
        L.append("断链图片 : 0")

    # 孤儿图片 (提示, 不计入问题: 可能是死文件, 或被合并EPUB的子书/CSS/SVG引用而未被扫描到)
    all_html = [n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
    refs_all = image_refs(z, all_html, images)
    referenced = {img for img, rlist in refs_all.items() if rlist}
    referenced |= css_url_refs(z)
    referenced |= cover_hrefs(z, opf)
    orphan = [img for img in sorted(images) if img not in referenced]
    if orphan:
        L.append("")
        L.append("[提示] 疑似孤儿图片 (%d, 未被任何页面/CSS/封面引用; 不影响完整性):" % len(orphan))
        for x in orphan[:50]:
            L.append("   " + x)
        if len(orphan) > 50:
            L.append("   ... 共 %d 个" % len(orphan))
    else:
        L.append("孤儿图片 : 0")

    # 章节列表
    toc = {}
    if opf["nav_href"]:
        toc = extract_toc(z, resolve(opf["dir"], opf["nav_href"]))
    if not toc and opf["ncx_href"]:
        toc = extract_toc(z, resolve(opf["dir"], opf["ncx_href"]))
    L.append("")
    L.append("--- 章节 (spine 顺序) ---")
    for i, s in enumerate(opf["spine"], 1):
        m = man_by_id.get(s["idref"])
        if not m:
            L.append("%3d. (无效 idref: %s)" % (i, s["idref"]))
            continue
        href = resolve(opf["dir"], m["href"])
        title = toc.get(m["href"]) or toc.get(href) or ""
        L.append("%3d. %s  ->  %s" % (i, (title or "(无标题)"), href))

    L.append("")
    if problems == 0:
        L.append("结论 : 完整，未发现问题")
    else:
        L.append("结论 : 发现 %d 个问题 (详见上方)" % problems)
    return "\n".join(L), problems


def check_pair(pathA, pathB):
    L = []
    L.append("=" * 62)
    L.append("EPUB 对比报告 (双文件)")
    L.append("=" * 62)
    zA = zipfile.ZipFile(pathA)
    zB = zipfile.ZipFile(pathB)
    L.append("文件A : %s" % pathA)
    L.append("      (%s, %d 条目)" % (fmt_size(os.path.getsize(pathA)), len(zA.namelist())))
    L.append("文件B : %s" % pathB)
    L.append("      (%s, %d 条目)" % (fmt_size(os.path.getsize(pathB)), len(zB.namelist())))

    for tag, z in (("A", zA), ("B", zB)):
        bad = z.testzip()
        L.append("CRC(%s) : %s" % (tag, "通过" if bad is None else "失败: %s" % bad))

    opfA = parse_opf(zA, get_opf_path(zA))
    opfB = parse_opf(zB, get_opf_path(zB))
    textsA, imagesA = opf_text_images(zA, opfA)
    textsB, imagesB = opf_text_images(zB, opfB)

    L.append("")
    L.append("文本文件 : A=%d 个, B=%d 个" % (len(textsA), len(textsB)))
    L.append("图片文件 : A=%d 个, B=%d 个" % (len(imagesA), len(imagesB)))

    relA = {rel_to_opf(opfA, p): p for p in textsA}
    relB = {rel_to_opf(opfB, p): p for p in textsB}
    common = sorted(set(relA) & set(relB))
    onlyA = sorted(set(relA) - set(relB))
    onlyB = sorted(set(relB) - set(relA))

    L.append("")
    L.append("--- 文本逐字比对 (共同 %d 个文件) ---" % len(common))
    same = diff = 0
    diff_lines = []
    for rel in common:
        a = normalize_text(zA.read(relA[rel]))
        b = normalize_text(zB.read(relB[rel]))
        if a == b:
            same += 1
        else:
            diff += 1
            diff_lines.append("  DIFF: %s  (A=%d 字符, B=%d 字符)" % (rel, len(a), len(b)))
    L.append("相同: %d 个 / 不同: %d 个" % (same, diff))
    L.extend(diff_lines)

    if onlyA:
        L.append("")
        L.append("仅A有的文本 (%d):" % len(onlyA))
        for p in onlyA:
            L.append("   " + relA[p])
    if onlyB:
        L.append("")
        L.append("仅B有的文本 (%d):" % len(onlyB))
        for p in onlyB:
            L.append("   " + relB[p])

    onlyImgA = sorted(imagesA - imagesB)
    onlyImgB = sorted(imagesB - imagesA)
    if onlyImgA:
        L.append("")
        L.append("仅A有的图片 (%d):" % len(onlyImgA))
        for p in onlyImgA:
            L.append("   " + p)
    if onlyImgB:
        L.append("")
        L.append("仅B有的图片 (%d):" % len(onlyImgB))
        for p in onlyImgB:
            L.append("   " + p)

    refsA = image_refs(zA, textsA, imagesA)
    refsB = image_refs(zB, textsB, imagesB)
    commonImg = sorted(imagesA & imagesB)
    changed = []
    for img in commonImg:
        ra = tuple(sorted(rel_to_opf(opfA, x) for x in refsA.get(img, [])))
        rb = tuple(sorted(rel_to_opf(opfB, x) for x in refsB.get(img, [])))
        if ra != rb:
            changed.append((img, ra, rb))
    L.append("")
    L.append("--- 插图映射 ---")
    L.append("共同图片 %d 个, 引用关系变化 %d 个" % (len(commonImg), len(changed)))
    for img, ra, rb in changed[:40]:
        L.append("  %s" % img)
        L.append("     A 引用: %s" % (", ".join(ra) or "(无)"))
        L.append("     B 引用: %s" % (", ".join(rb) or "(无)"))

    L.append("")
    if diff == 0 and not onlyA and not onlyB and not onlyImgA and not onlyImgB:
        L.append("结论 : 两份 EPUB 完全一致 (文本与文件均无差异)")
    elif diff == 0 and not onlyA and not onlyB:
        L.append("结论 : 文本内容完全一致 (仅图片文件有增删)")
    else:
        L.append("结论 : 存在差异，详见上方明细")
    return "\n".join(L)


def check_collection(collection_path, sources):
    L = []
    L.append("=" * 62)
    L.append("合集完整性检查报告")
    L.append("=" * 62)
    if not os.path.exists(collection_path):
        L.append("合集文件不存在: %s" % collection_path)
        return "\n".join(L)
    L.append("合集 : %s" % collection_path)
    L.append("      (%s)" % fmt_size(os.path.getsize(collection_path)))
    kind, info = detect_book(collection_path)
    L.append("      类型: %s" % info)

    coll_hashes = []
    coll_text = ""
    if kind == "image":
        coll_hashes = book_image_dhashes(collection_path)
        L.append("      页数: %d" % len(coll_hashes))
    else:
        coll_text = book_fulltext(collection_path)
        L.append("      文字: %d 字符(归一化)" % len(coll_text))

    coll_valid = []
    valid_idx = set()
    pos = {}
    coll_np = None
    if kind == "image":
        coll_valid = [(j, dh, th) for j, (lb, dh, th) in enumerate(coll_hashes)
                      if dh is not None and th is not None]
        valid_idx = set(j for j, _, _ in coll_valid)
        pos = {j: k for k, (j, _, _) in enumerate(coll_valid)}
        if _NP is not None and coll_valid:
            try:
                coll_np = _NP.frombuffer(
                    b"".join(t for _, _, t in coll_valid),
                    dtype=_NP.uint8).reshape(len(coll_valid), -1)
            except Exception:
                coll_np = None

    L.append("")
    L.append("分册数: %d" % len(sources))

    used = {}
    grand_missing = 0
    grand_degraded = 0
    uncomparable = 0  # 完全没参与比对的分册数（不能让它们被当成「已找到」）
    for i, src in enumerate(sources, 1):
        L.append("")
        L.append("[%d/%d] %s" % (i, len(sources), src))
        if not os.path.exists(src):
            L.append("   (文件不存在, 跳过)")
            continue
        skind, sinfo = detect_book(src)
        L.append("   类型: %s" % sinfo)
        if skind != kind:
            L.append("   [警告] 合集是 %s, 分册是 %s, 比对可能无意义" % (kind, skind))

        if skind == "image":
            if not coll_hashes:
                L.append("   (合集为文字版, 无法与图片版分册比对)")
                uncomparable += 1
                continue
            sh = book_image_dhashes(src)
            if not sh:
                L.append("   (未能提取图片, 可能缺 Pillow)")
                continue
            matched = 0
            missing = []
            degraded = []
            dup = 0
            pairs = []
            pend = []
            for idx, (lb, dh, th) in enumerate(sh, 1):
                if dh is None or th is None:
                    missing.append(lb + " (无法解码)")
                    continue
                best_j, best_mad, best_h = _find_match(th, dh, coll_valid, coll_np)
                if best_j >= 0 and best_mad <= MAD_THRESHOLD:
                    matched += 1
                    used[best_j] = used.get(best_j, 0) + 1
                    if used[best_j] > 1:
                        dup += 1
                    pairs.append((idx, best_j))
                else:
                    pend.append((idx, lb, best_mad, best_h))
            offset = None
            if pairs:
                offs = sorted(j - (idx - 1) for idx, j in pairs)
                offset = offs[len(offs) // 2]
            for idx, lb, best_mad, best_h in pend:
                if offset is not None:
                    ej = offset + (idx - 1)
                    if 0 <= ej < len(coll_hashes) and ej in valid_idx and used.get(ej, 0) == 0:
                        degraded.append("%s -> %s (MAD %.1f, 疑似源文件降质)"
                                        % (lb, coll_hashes[ej][0].split("/")[-1], best_mad))
                        used[ej] = used.get(ej, 0) + 1
                        continue
                missing.append("%s (最近 MAD %.1f, dHash %d)" % (lb, best_mad, best_h))
            grand_missing += len(missing)
            grand_degraded += len(degraded)
            L.append("   页数 %d, 匹配 %d, 疑似降质 %d, 缺失 %d%s"
                     % (len(sh), matched, len(degraded), len(missing),
                        (", 重复匹配 %d" % dup) if dup else ""))
            for x in missing[:20]:
                L.append("     缺失: " + x)
            if len(missing) > 20:
                L.append("     ... 共 %d 处未匹配" % len(missing))
            for x in degraded[:20]:
                L.append("     降质: " + x)
            if len(degraded) > 20:
                L.append("     ... 共 %d 处疑似降质" % len(degraded))
        else:
            if is_pdf_file(src):
                L.append("   (PDF 文字版暂不支持逐字比对, 需 PyMuPDF; 仅记录页数)")
                uncomparable += 1
                continue
            if not coll_text:
                L.append("   (合集无文字, 无法比对)")
                uncomparable += 1
                continue
            try:
                zsrc = zipfile.ZipFile(src)
                opf = parse_opf(zsrc, get_opf_path(zsrc))
                texts, images = opf_text_images(zsrc, opf)
            except Exception as e:
                L.append("   (无法解析: %s)" % e)
                uncomparable += 1
                continue
            man = {m["id"]: m for m in opf["manifest"]}
            found = 0
            miss = []
            for s in opf["spine"]:
                m = man.get(s["idref"])
                if not m:
                    continue
                href = resolve(opf["dir"], m["href"])
                if not href or href not in texts or href not in zsrc.namelist():
                    continue
                txt = normalize_text(zsrc.read(href))
                if not txt:
                    continue
                if txt in coll_text:
                    found += 1
                else:
                    miss.append(href)
            grand_missing += len(miss)
            L.append("   章节(spine) %d, 在合集中找到 %d, 缺失 %d"
                     % (found + len(miss), found, len(miss)))
            for x in miss[:20]:
                L.append("     缺失: " + x)
            if len(miss) > 20:
                L.append("     ... 共 %d 章未找到" % len(miss))

    if kind == "image":
        extras = [j for j, (lb, dh, th) in enumerate(coll_hashes)
                  if dh is not None and th is not None and used.get(j, 0) == 0]
        L.append("")
        dup_n = 0
        if _NP is not None:
            for j in extras:
                if j not in pos:
                    continue
                k = pos[j]
                d = _NP.abs(coll_np.astype(_NP.int16)
                            - coll_np[k].astype(_NP.int16)).mean(axis=1)
                d[k] = 999.0
                if float(d.min()) < PAGE_DUP_MAD:
                    dup_n += 1
        L.append("合集里未被任何分册匹配的页: %d（其中与另一页几乎相同: %d）"
                 % (len(extras), dup_n))
        for j in extras[:40]:
            name = coll_hashes[j][0].split("/")[-1]
            note = ""
            if coll_np is not None and j in pos:
                try:
                    k = pos[j]
                    d = _NP.abs(coll_np.astype(_NP.int16) - coll_np[k].astype(_NP.int16)).mean(axis=1)
                    d[k] = 999.0
                    j2 = int(d.argmin())
                    v2 = float(d[j2])
                    name2 = coll_hashes[coll_valid[j2][0]][0].split("/")[-1]
                    if v2 < 2.0:
                        note = "  (与 %s 相同)" % name2
                    else:
                        note = "  (最接近 %s, MAD %.1f)" % (name2, v2)
                except Exception:
                    note = ""
            L.append("   " + name + note)
        if len(extras) > 40:
            L.append("   ... 共 %d 页" % len(extras))

    L.append("")
    if uncomparable:
        L.append("说明 : 有 %d 本分册无法比对（未计入缺失统计）" % uncomparable)
    if grand_missing == 0 and grand_degraded == 0 and uncomparable:
        L.append("结论 : 未发现缺失，但并非所有分册都参与了比对")
    elif grand_missing == 0 and grand_degraded == 0:
        L.append("结论 : 所有分册内容均已在合集中找到, 未发现缺失")
    elif grand_missing == 0:
        L.append("结论 : 未发现缺失, 另有 %d 处疑似降质(源文件质量差异, 位置已对应)" % grand_degraded)
    else:
        L.append("结论 : 共 %d 处内容在合集中未找到(疑似缺失)%s, 详见上方明细"
                 % (grand_missing,
                    (", 另有 %d 处疑似降质" % grand_degraded) if grand_degraded else ""))
    return "\n".join(L)


def book_format(path):
    return FMT_BY_EXT.get(os.path.splitext(path)[1].lower(), "?")


def _norm_rel(p):
    """资源相对路径归一化: 统一分隔符, .xhtml/.htm 映射为 .html。"""
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    low = p.lower()
    if low.endswith(".xhtml"):
        p = p[:-6] + ".html"
    elif low.endswith(".htm"):
        p = p[:-4] + ".html"
    return p


def _text_digest(raw):
    """文本归一化后的 (字符数, md5): 比对只需等值判断, 不缓存全文。"""
    txt = normalize_text(raw)
    return (len(txt), hashlib.md5(txt.encode("utf-8", "replace")).hexdigest())


def _cache_key(path):
    st = os.stat(path)
    return (os.path.abspath(path), st.st_size, st.st_mtime_ns)


_CACHE = {}


_CACHE_MU = threading.Lock()


def book_manifest(path):
    """解析一本书的资源清单。

    EPUB  -> {"texts": {相对路径: (字符数, md5)}, "images": set(相对路径)}
    MOBI/PDF -> 只有页数与内嵌图计数, 无资源清单
    失败  -> info["err"] 非空, 不抛异常(由调用方决定状态)
    """
    fmt = book_format(path)
    info = {"fmt": fmt, "kind": "?", "texts": {}, "images": set(),
            "pages": 0, "err": ""}
    try:
        if fmt in ("MOBI", "AZW", "AZW3", "PRC"):
            recs = mobi_records(path)
            if not recs:
                raise ValueError("不是有效的 PalmDB")
            imgs = mobi_images(path)
            info["pages"] = len(imgs)
            info["kind"] = "image" if imgs else "text"
            info["images"] = {"mobi#%d" % i for i in range(1, len(imgs) + 1)}
        elif fmt == "PDF":
            with open(path, "rb") as f:
                head = f.read(65536)
            if b"/Encrypt" in head:
                raise ValueError("PDF 已加密，无法解析")
            page_n = pdf_page_count(path)
            if page_n == 0:
                raise ValueError("PDF 无可用页面（可能损坏）")
            imgs = pdf_images(path)
            info["pages"] = page_n
            info["kind"] = "image" if len(imgs) >= max(2, page_n // 2) else "text"
            info["images"] = {"pdf#%d" % i for i in range(1, len(imgs) + 1)}
        elif fmt == "EPUB":
            z = zipfile.ZipFile(path)
            bad = z.testzip()
            if bad is not None:
                raise ValueError("zip 损坏条目: %s" % bad)
            opf = parse_opf(z, get_opf_path(z))
            texts, images = opf_text_images(z, opf)
            names = set(z.namelist())
            for p in texts:
                if p in names:
                    info["texts"][_norm_rel(rel_to_opf(opf, p))] = _text_digest(z.read(p))
            info["images"] = {_norm_rel(rel_to_opf(opf, p)) for p in images}
            total = sum(v[0] for v in info["texts"].values())
            info["kind"] = ("image" if info["images"] and
                            total / max(1, len(texts)) < TEXT_MIN_AVG else "text")
        else:
            raise ValueError("不支持的格式: %s" % (fmt,))
    except Exception as e:
        info["err"] = "%s: %s" % (type(e).__name__, e)
    return info


def book_info_cached(path):
    """带 singleflight 的解析缓存; 排队期间文件被改过 -> Key 变化 -> 自动重解析。"""
    key = _cache_key(path)
    with _CACHE_MU:
        hit = _CACHE.get(key)
        if hit is not None:
            return hit
        key_mu = _KEY_MU.setdefault(key, threading.Lock())
    with key_mu:
        with _CACHE_MU:
            hit = _CACHE.get(key)
            if hit is not None:
                return hit
        info = book_manifest(path)
        if not info["err"]:
            # 失败结果不长期缓存: 文件可能被外部修复后重跑
            with _CACHE_MU:
                _CACHE[key] = info
                while len(_CACHE) > 200:      # ponytail: 简单上限，别让长跑会话无限涨
                    _CACHE.pop(next(iter(_CACHE)))
        return info


def _result(status, summary="", reason="", detail=None):
    return {"status": status, "summary": summary, "reason": reason,
            "detail": list(detail or [])}


def _dominant_dir(paths):
    """出现最多的顶层目录名（如 book_002），没有就返回空串。"""
    counts = {}
    for p in paths:
        if "/" in p:
            d = p.split("/", 1)[0]
            counts[d] = counts.get(d, 0) + 1
    if not counts:
        return ""
    return max(sorted(counts), key=lambda d: counts[d])


def coverage_stats(m, ta, side="a"):
    """按【实质文本的字符数】算覆盖率: 样板页再多也不算数。

    side="a" 算基准侧, side="b" 算待检侧。
    返回 (cov, matched_chars, total_chars, only_meaningful)。
    """
    total = 0
    for k, (chars, _h) in ta.items():
        if chars >= TEXT_MIN_MEANINGFUL:
            total += chars
    if side == "b":
        matched_keys = list(m["path_same"]) + [b for _a, b in m["content_same"]]
        only_keys = m["only_b"]
    else:
        matched_keys = list(m["path_same"]) + [a for a, _b in m["content_same"]]
        only_keys = m["only_a"]
    got = 0
    for k in matched_keys:
        chars = ta.get(k, (0, ""))[0]
        if chars >= TEXT_MIN_MEANINGFUL:
            got += chars
    only_a_meaningful = [k for k in only_keys
                         if ta.get(k, (0, ""))[0] >= TEXT_MIN_MEANINGFUL]
    if not total:
        # 整本都是样板页/图片页，没有实质文本可加权 → 退回按文件数算
        cov = (len(matched_keys) / float(len(ta))) if ta else 0.0
        return cov, len(matched_keys), 0, only_a_meaningful
    cov = got / float(total)
    return cov, got, total, only_a_meaningful


def match_books(ta, tb, max_detail=300):
    """资源配对: 路径优先, 内容 md5 兜底（一对一消耗）。

    ta/tb 为 {归一化相对路径: (字符数, md5)}。
    单卷 vs 合集时, 单卷章节在根、合集章节在 book_00N/ 下, 路径永远不同,
    靠内容哈希才能认出「这一卷就在合集里」。
    """
    ka, kb = set(ta), set(tb)
    common = ka & kb
    path_diff = sorted(k for k in common if ta[k] != tb[k])
    path_same = sorted(common - set(path_diff))

    by_md5_b = {}
    for p in sorted(kb - common):
        by_md5_b.setdefault(tb[p][1], []).append(p)

    content_same = []
    consumed_b = set()
    only_a = []
    for p in sorted(ka - common):
        cand = by_md5_b.get(ta[p][1]) or []
        pick = next((q for q in cand if q not in consumed_b), None)
        if pick is None:
            only_a.append(p)
        else:
            consumed_b.add(pick)
            content_same.append((p, pick))
    only_b = sorted(q for q in kb - common if q not in consumed_b)

    detail = []
    if path_same:
        detail.append("路径与内容都相同: %d 个" % len(path_same))
        for k in path_same[:max_detail]:
            detail.append("  = %s" % k)
    if content_same:
        detail.append("内容相同、路径不同: %d 个" % len(content_same))
        for a, b in content_same[:max_detail]:
            detail.append("  ≈ %s  ↔  %s" % (a, b))
    if path_diff:
        detail.append("同路径但内容不同: %d 个" % len(path_diff))
        for k in path_diff[:max_detail]:
            detail.append("  ≠ %s（基准 %d 字 / 待检 %d 字）"
                          % (k, ta[k][0], tb[k][0]))
    if only_a:
        detail.append("仅基准有: %d 个" % len(only_a))
        for k in only_a[:max_detail]:
            detail.append("  - %s（%d 字）" % (k, ta[k][0]))
    if only_b:
        detail.append("仅待检有: %d 个" % len(only_b))
        for k in only_b[:max_detail]:
            detail.append("  + %s（%d 字）" % (k, tb[k][0]))
    if len(detail) > max_detail:
        detail = detail[:max_detail] + ["…（明细过长，已截断）"]

    return {"path_same": path_same, "path_diff": path_diff,
            "content_same": content_same, "only_a": only_a,
            "only_b": only_b, "detail": detail}


_IMG_CACHE = {}


_IMG_TOTAL = [0]


_IMG_MU = threading.Lock()


# 解图并发：读字节留在调用线程，解码与缩略图丢给线程池
DECODE_WORKERS = 4


_POOL = ThreadPoolExecutor(max_workers=DECODE_WORKERS, thread_name_prefix="decode")


_IMG_CACHE_PAGES = 12000


# 页面指纹磁盘缓存：默认放 %LOCALAPPDATA%\epub_checker\cache，测试可用环境变量改写
CACHE_DIR = os.environ.get("EPUB_CHECK_CACHE") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "epub_checker", "cache")


CACHE_DISK_LIMIT = 512 * 1024 * 1024


def _disk_path(key):
    digest = hashlib.sha1(repr(key).encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, digest + ".bin")


def _disk_load(path):
    try:
        import pickle
        with open(path, "rb") as f:
            data = pickle.load(f)
        return data if isinstance(data, list) else None
    except Exception:
        return None


def _disk_prune():
    """按修改时间从旧到新删，直到总大小不超过 CACHE_DISK_LIMIT。"""
    try:
        files = []
        for name in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, name)
            try:
                files.append((os.path.getmtime(p), os.path.getsize(p), p))
            except OSError:
                continue
        files.sort()
        total = sum(size for _m, size, _p in files)
        for _m, size, p in files:
            if total <= CACHE_DISK_LIMIT:
                break
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass
    except Exception:
        pass


def _disk_save(path, data):
    try:
        import pickle
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(data, f, protocol=4)
        os.replace(tmp, path)
        _disk_prune()
    except Exception:
        pass


def page_hashes(path):
    """页面图哈希列表 [(label, dhash, thumb)]，先查内存缓存，再查磁盘缓存。"""
    try:
        key = _cache_key(path)
    except Exception:
        return book_image_dhashes(path)
    with _IMG_MU:
        hit = _IMG_CACHE.get(key)
    if hit is not None:
        return hit
    cache_file = _disk_path(key)
    on_disk = _disk_load(cache_file)
    if on_disk:
        with _IMG_MU:
            _IMG_CACHE[key] = on_disk
            _IMG_TOTAL[0] += len(on_disk)
        return on_disk
    out = [t for t in book_image_dhashes(path)
           if t[1] is not None and t[2] is not None]
    with _IMG_MU:
        if _IMG_CACHE.get(key) is None:
            _IMG_CACHE[key] = out
            _IMG_TOTAL[0] += len(out)
            while _IMG_TOTAL[0] > _IMG_CACHE_PAGES and len(_IMG_CACHE) > 1:
                oldest = next(iter(_IMG_CACHE))
                _IMG_TOTAL[0] -= len(_IMG_CACHE.pop(oldest))
    if out:
        _disk_save(cache_file, out)
    return _IMG_CACHE.get(key, out)


def _text_chars(t):
    """一本书的实质文字量(只算 >=TEXT_MIN_MEANINGFUL 的章节)。"""
    return sum(v[0] for v in t.values() if v[0] >= TEXT_MIN_MEANINGFUL)


def _no_meaningful_text(ta, tb):
    """任一侧实质文字不足 TEXT_MIN_BOOK 字(扫描版/纯图片书/只有目录) => 文字比对没意义。"""
    return _text_chars(ta) < TEXT_MIN_BOOK or _text_chars(tb) < TEXT_MIN_BOOK


def match_pages(ha, hb, mad_threshold=None, top_k=8):
    """页面图一对一匹配。ha/hb: [(label, dhash, thumb)]。"""
    th = MAD_THRESHOLD if mad_threshold is None else mad_threshold
    na, nb = len(ha), len(hb)
    if not na or not nb:
        return {"matched": [], "only_a": [x[0] for x in ha],
                "only_b": [x[0] for x in hb], "coverage": 0.0,
                "cov_a": 0.0, "cov_b": 0.0}

    pairs = []
    if _NP is not None:
        ta = _NP.array([_NP.frombuffer(x[2], dtype=_NP.uint8) for x in ha],
                       dtype=_NP.int16)
        tb = _NP.array([_NP.frombuffer(x[2], dtype=_NP.uint8) for x in hb],
                       dtype=_NP.int16)
        da = _NP.array([x[1] for x in ha], dtype=_NP.uint64)
        db = _NP.array([x[1] for x in hb], dtype=_NP.uint64)
        k = min(top_k, nb)
        for s in range(0, na, 128):
            e = min(na, s + 128)
            h = _NP.bitwise_count(da[s:e][:, None] ^ db[None, :])
            idx = _NP.argpartition(h, k - 1, axis=1)[:, :k]
            for r in range(e - s):
                i = s + r
                best = None
                for j in idx[r]:
                    mad = float(_NP.abs(ta[i] - tb[int(j)]).mean())
                    if mad <= th and (best is None or mad < best[0]):
                        best = (mad, int(j))
                if best is not None:
                    pairs.append((best[0], i, best[1]))
    else:
        for i, (_, dh_a, th_a) in enumerate(ha):
            best = None
            for j, (_, dh_b, th_b) in enumerate(hb):
                if hamming(dh_a, dh_b) > 24:
                    continue
                mad = _mad(th_a, th_b)
                if mad <= th and (best is None or mad < best[0]):
                    best = (mad, j)
            if best is not None:
                pairs.append((best[0], i, best[1]))

    pairs.sort()
    used_a, used_b, matched = set(), set(), []
    for _mad_v, i, j in pairs:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        matched.append((ha[i][0], hb[j][0], _mad_v))
    # 重复页: 同一页在书里出现多次(插画/扉页/空白页), 允许复用对侧已用过的同一页,
    # 否则一对一匹配会把"第二份同样的页"误判成「仅一侧有」。
    dups = []
    if _NP is not None:
        for i in [x for x in range(na) if x not in used_a]:
            row = _NP.abs(ta[i][None, :] - tb).mean(axis=1)
            j = int(row.argmin())
            if float(row[j]) <= PAGE_DUP_MAD:
                dups.append((float(row[j]), i, j))
                used_a.add(i)
        for j in [x for x in range(nb) if x not in used_b]:
            col = _NP.abs(tb[j][None, :] - ta).mean(axis=1)
            i = int(col.argmin())
            if float(col[i]) <= PAGE_DUP_MAD:
                dups.append((float(col[i]), i, j))
                used_b.add(j)
    else:
        for i in [x for x in range(na) if x not in used_a]:
            best = None
            for j in range(nb):
                if hamming(ha[i][1], hb[j][1]) > 8:
                    continue
                mad = _mad(ha[i][2], hb[j][2])
                if mad <= PAGE_DUP_MAD and (best is None or mad < best[0]):
                    best = (mad, j)
            if best is not None:
                dups.append((best[0], i, best[1]))
                used_a.add(i)
        for j in [x for x in range(nb) if x not in used_b]:
            best = None
            for i in range(na):
                if hamming(hb[j][1], ha[i][1]) > 8:
                    continue
                mad = _mad(hb[j][2], ha[i][2])
                if mad <= PAGE_DUP_MAD and (best is None or mad < best[0]):
                    best = (mad, i)
            if best is not None:
                dups.append((best[0], best[1], j))
                used_b.add(j)

    only_a = [x[0] for i, x in enumerate(ha) if i not in used_a]
    only_b = [x[0] for i, x in enumerate(hb) if i not in used_b]
    cov_a = len(used_a) / float(na)
    cov_b = len(used_b) / float(nb)
    return {"matched": matched, "dups": dups, "only_a": only_a, "only_b": only_b,
            "coverage": min(cov_a, cov_b), "cov_a": cov_a, "cov_b": cov_b}


def _compare_pages(path_a, path_b, ha, hb, max_detail=30):
    """按页面图给出结论（扫描版 PDF / 纯图片书）。"""
    m = match_pages(ha, hb)
    na, nb = len(ha), len(hb)
    mx = max(na, nb)
    dups = m.get("dups") or []
    cov = m["coverage"]
    pct = "100" if cov >= 0.9995 else ("%.1f" % (cov * 100))

    detail = ["基准 %d 页 / 待检 %d 页" % (na, nb),
              "能对应上: 基准 %d 页, 待检 %d 页" % (na - len(m["only_a"]),
                                                nb - len(m["only_b"]))]
    if dups:
        detail.append("其中重复页: %d 页（同一页在多处出现）" % len(dups))
    if m["only_a"]:
        detail.append("仅基准有: %d 页" % len(m["only_a"]))
        for lb in m["only_a"][:max_detail]:
            detail.append("  - %s" % lb)
    if m["only_b"]:
        detail.append("仅待检有: %d 页" % len(m["only_b"]))
        for lb in m["only_b"][:max_detail]:
            detail.append("  + %s" % lb)

    summary = "页面图片比对：基准 %d 页 / 待检 %d 页，%s%% 能对应上" % (na, nb, pct)
    if dups:
        summary += "，其中 %d 页是重复页" % len(dups)
    if not na or not nb:
        return _result(STATUS_CANNOT, summary, "一侧没有可提取的页面图", detail)
    if not m["only_a"] and not m["only_b"]:
        return _result(STATUS_OK, summary, "", detail)
    cov_a = m.get("cov_a", 0.0)
    cov_b = m.get("cov_b", 0.0)
    # 单卷 vs 合集: 小书几乎全部在大书里找到 → 合集内含, 别因为大书页多就说差异
    if cov_a >= PAGE_CONTAIN_COVERAGE and cov_b < PAGE_CONTAIN_COVERAGE:
        tail = "；基准的 %d 页基本全在待检的 %d 页里" % (na, nb)
        if m["only_a"]:
            tail += "，%d 页对不上" % len(m["only_a"])
        return _result(STATUS_IN_COLL, summary + tail, "", detail)
    if cov_b >= PAGE_CONTAIN_COVERAGE and cov_a < PAGE_CONTAIN_COVERAGE:
        tail = "；待检的 %d 页基本全在基准的 %d 页里" % (nb, na)
        if m["only_b"]:
            tail += "，%d 页对不上" % len(m["only_b"])
        return _result(STATUS_IN_COLL, summary + tail, "", detail)
    if cov >= PAGE_FULL_COVERAGE:
        loose = max(len(m["only_a"]), len(m["only_b"]))
        return _result(STATUS_OK,
                       summary + "，%d 页对不上（多半是渲染/裁边差异）" % loose,
                       "", detail)
    if not m["only_a"] or not m["only_b"]:
        return _result(STATUS_IN_COLL,
                       summary + "，一方页面全在另一方中", "", detail)
    if cov >= PART_COVERAGE:
        return _result(STATUS_PART, summary, "", detail)
    return _result(STATUS_DIFF, summary, "", detail)


def compare_books(path_a, path_b, stage=None):
    """跨格式对照: 同格式 EPUB 比资源清单, 其它组合如实给「无法比对」。"""
    def st(s):
        if stage is not None:
            stage(s)

    st("解析基准")
    try:
        ma = book_info_cached(path_a)
    except Exception as e:
        return _result(STATUS_FAIL, reason="基准文件不可用: %s" % e)
    st("解析待检")
    try:
        mb = book_info_cached(path_b)
    except Exception as e:
        return _result(STATUS_FAIL, reason="待检文件不可用: %s" % e)
    if ma["err"]:
        return _result(STATUS_FAIL, reason="基准解析失败: " + ma["err"])
    if mb["err"]:
        return _result(STATUS_FAIL, reason="待检解析失败: " + mb["err"])

    st("比对")
    # 扫描件 / 纯图片书: 两边都没有实质文字, 文字比对没有意义 -> 按页面图比对
    if _no_meaningful_text(ma["texts"], mb["texts"]):
        st("按页面图片比对")
        ha = page_hashes(path_a)
        hb = page_hashes(path_b)
        if len(ha) >= 3 and len(hb) >= 3:
            return _compare_pages(path_a, path_b, ha, hb)
    if ma["fmt"] != mb["fmt"]:
        return _result(STATUS_CANNOT,
                       "格式 %s / %s" % (ma["fmt"], mb["fmt"]),
                       "跨格式资源清单无法映射（%s↔%s），未做二进制比对" %
                       (ma["fmt"], mb["fmt"]))
    if ma["fmt"] != "EPUB":
        return _result(STATUS_CANNOT,
                       "%s / %s，页数 %d / %d" % (ma["fmt"], ma["fmt"],
                                                 ma["pages"], mb["pages"]),
                       "%s 目前只提供页数与内嵌图计数，没有可映射的资源清单" % ma["fmt"])

    ta, tb = ma["texts"], mb["texts"]
    m = match_books(ta, tb)
    path_same, path_diff = m["path_same"], m["path_diff"]
    content_same, only_a, only_b = m["content_same"], m["only_a"], m["only_b"]
    matched = len(path_same) + len(content_same)
    img_only_a = ma["images"] - mb["images"]
    img_only_b = mb["images"] - ma["images"]
    detail = m["detail"]
    if img_only_a or img_only_b:
        detail = detail + ["图片: 仅基准 %d / 仅待检 %d"
                           % (len(img_only_a), len(img_only_b))]

    if not path_diff and not only_a and not only_b and not img_only_a and not img_only_b:
        return _result(STATUS_OK, "内容完全一致（%d 个文本）" % matched, "", detail)

    cov, got, total, only_a_sig = coverage_stats(m, ta)
    # 差一点点就写 99.7%, 别四舍五入成 100% 让人以为一字不差
    pct = "100" if cov >= 0.9995 else ("%.1f" % (cov * 100))

    # 基准的实质内容基本都在对方找到了（路径对不上就按内容认出）→ 合集内含
    if content_same and cov >= IN_COLL_COVERAGE and not only_a_sig:
        sub = _dominant_dir([b for _, b in content_same])
        where = (" %s " % sub) if sub else ""
        summary = "同一本书的不同打包：内容覆盖 %s%%（%d/%d 字在合集%s中找到）" % (
            pct, got, total, where)
        if path_diff:
            summary += "，%d 个同路径文件内容不同" % len(path_diff)
        return _result(STATUS_IN_COLL, summary, "", detail)

    # 反方向: 待检的实质内容基本都在基准里找到 (基准=合集, 待检=单卷) 也是合集内含
    cov_b, got_b, total_b, only_b_sig = coverage_stats(m, tb, side="b")
    if content_same and cov_b >= IN_COLL_COVERAGE and not only_b_sig:
        sub = _dominant_dir([a for a, _b in content_same])
        where = (" %s " % sub) if sub else ""
        summary = "同一本书的不同打包：待检内容覆盖 %s%%（%d/%d 字在基准%s中找到）" % (
            "100" if cov_b >= 0.9995 else "%.1f" % (cov_b * 100), got_b, total_b, where)
        if path_diff:
            summary += "，%d 个同路径文件内容不同" % len(path_diff)
        return _result(STATUS_IN_COLL, summary, "", detail)

    # 只有样板页相同(如 titlepage 5 字)不算"部分一致", 必须有实质重叠
    if cov < PART_COVERAGE:
        bits = ["内容覆盖 %s%%" % pct]
        if matched:
            bits.append("相同的实质章节 %d 个" % len(
                [k for k in list(path_same) + [a for a, _b in content_same]
                 if ta.get(k, (0, ""))[0] >= TEXT_MIN_MEANINGFUL]))
        if only_a_sig:
            bits.append("仅基准有 %d 个" % len(only_a_sig))
        if only_b:
            bits.append("仅待检有 %d 个" % len(only_b))
        if img_only_a or img_only_b:
            bits.append("图片 仅基准 %d / 仅待检 %d"
                        % (len(img_only_a), len(img_only_b)))
        return _result(STATUS_DIFF, "，".join(bits), "", detail)

    bits = ["内容覆盖 %s%%" % pct]
    if matched:
        bits.append("%d 个文本一致" % matched)
    if path_diff:
        bits.append("%d 个内容不同" % len(path_diff))
    if only_a:
        bits.append("%d 个仅基准有" % len(only_a))
    if only_b:
        bits.append("%d 个仅待检有" % len(only_b))
    if img_only_a or img_only_b:
        bits.append("图片 仅基准 %d / 仅待检 %d" % (len(img_only_a), len(img_only_b)))
    summary = "，".join(bits) if bits else "资源清单无交集"

    status = STATUS_PART if cov >= PART_COVERAGE else STATUS_DIFF
    return _result(status, summary, "", detail)


class TaskRunner:
    """N×M 任务执行器: 固定 3 线程 + 单任务失败隔离 + 可取消。无 GUI 依赖。"""

    def __init__(self, tasks, emit, workers=3):
        # tasks: [(idx, base, check, same)]
        self.tasks = list(tasks)
        self.emit = emit
        self.workers = max(1, min(4, workers))
        self._q = queue.Queue()
        for t in self.tasks:
            self._q.put(t)
        self._mu = threading.Lock()
        self._state = {t[0]: "pending" for t in self.tasks}
        self._terminal = 0
        self.cancel_ev = threading.Event()

    @property
    def total(self):
        return len(self.tasks)

    def start(self):
        for _ in range(max(1, min(self.workers, self.total))):
            threading.Thread(target=self._worker, daemon=True).start()

    def cancel(self):
        """把尚未开始的任务标为已取消; 正在跑的由 worker 自行收尾。"""
        self.cancel_ev.set()
        with self._mu:
            pend = [i for i, s in self._state.items() if s == "pending"]
            for i in pend:
                self._state[i] = "cancelled"
        for i in pend:
            self._report(i, STATUS_CANCEL, "", "未执行（已取消）", 0.0)

    def _stage(self, idx, text):
        with self._mu:
            if self._state.get(idx) != "running":
                return
        self.emit(("stage", idx, text))

    def _report(self, idx, status, summary, reason, elapsed):
        with self._mu:
            if self._state.get(idx) == "terminal":
                return
            self._state[idx] = "terminal"
            self._terminal += 1
            done, total = self._terminal, self.total
        self.emit(("row", idx, status, summary, reason, elapsed, done, total))

    def _worker(self):
        while True:
            try:
                idx, base, chk, same = self._q.get_nowait()
            except queue.Empty:
                return
            with self._mu:
                if self._state.get(idx) != "pending":
                    continue
                self._state[idx] = "running"
            if same:
                self._report(idx, STATUS_SKIP, "同一文件，跳过自比", "", 0.0)
                continue
            t0 = time.time()
            try:
                res = compare_books(base, chk, lambda s: self._stage(idx, s))
            except Exception as e:
                res = _result(STATUS_FAIL, reason="%s: %s" % (type(e).__name__, e))
            dt = time.time() - t0
            if self.cancel_ev.is_set() and res["status"] != STATUS_FAIL:
                self._report(idx, STATUS_CANCEL, res.get("summary", ""), "任务已中断", dt)
            else:
                self._report(idx, res["status"], res.get("summary", ""),
                             res.get("reason", ""), dt)


def toc_structure_problems(z, opf):
    """检查目录自身结构问题，返回 [(级别, 说明)]。"""
    probs = []
    if not opf.get("nav_href") and not opf.get("ncx_href"):
        probs.append(("错误", "书里没有 nav.xhtml 也没有 toc.ncx，无法定位目录"))
        return probs
    tree = read_toc_tree(z, opf)
    if not tree:
        probs.append(("错误", "目录文件存在，但读不出任何目录项"))
        return probs
    base = opf["dir"]
    names = set(z.namelist())
    seen = {}
    titles = {}
    for i, t in enumerate(tree):
        href = t.get("href")
        if not href:
            # href="#" 这类自引用（目录页自己指向自己）视为「无链接」，不算错误
            continue
        full = href if href in names else resolve(base, href)
        if not full or full not in names:
            probs.append(("错误", "目录项指向的文件不存在：%s" % href))
        frag = (t.get("frag") or "").lower()
        key = ((full or href) + ("#" + frag if frag else "")).lower()
        if key in seen:
            probs.append(("错误", "同一个文件在目录里重复出现：%s" % href))
        seen[key] = i
        ttl = (t.get("title") or "").strip()
        if ttl in titles:
            probs.append(("警告", "标题重复：%s" % ttl))
        titles[ttl] = i
    docs = spine_docs(z, opf)
    names_all = set(z.namelist())
    doc_hrefs = [(d["href"] if d["href"] in names_all
                  else resolve(opf["dir"], d["href"])).lower() for d in docs]
    toc_hrefs = [t["href"].lower() for t in tree if t.get("href")]
    if len(toc_hrefs) != len(doc_hrefs):
        probs.append(("警告", "目录 %d 条，正文 %d 个，数量不一致"
                      % (len(toc_hrefs), len(doc_hrefs))))
    common_toc = [h for h in toc_hrefs if h in doc_hrefs]
    common_doc = [h for h in doc_hrefs if h in toc_hrefs]
    if common_toc != common_doc:
        mism = 0
        for a, b in zip(common_toc, common_doc):
            if a != b:
                mism += 1
                if mism <= 5:
                    probs.append(("警告", "顺序不一致：目录 %s ↔ 正文 %s" % (a, b)))
    toc_set = set(toc_hrefs)
    missing = [h for h in doc_hrefs if h not in toc_set]
    if missing:
        probs.append(("警告", "正文有 %d 个文档未被目录收录：%s"
                      % (len(missing), "、".join(missing[:10]))))
    prev = 0
    for t in tree:
        lv = t.get("level") or 1
        if prev and lv > prev + 1:
            probs.append(("警告", "层级跳级：%s 从 %d 直接到 %d"
                          % (t.get("title"), prev, lv)))
        prev = lv
    return probs
