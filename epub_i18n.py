# -*- coding: utf-8 -*-
"""界面与报告文案的翻译层。

约定：key 就是中文原文。找不到译文时原样返回中文，代码因此永远可读、永远不炸。
用法：
    import epub_i18n as i18n
    i18n.tr("开始比对")             -> "Start compare"（英文界面下）
    i18n.tr("共 %d 个任务", n=3)    -> "3 tasks"
"""

from __future__ import annotations
import re

LANGS = ("zh", "en")          # 实际可选值，界面还会多一个 "auto"
_current = "zh"

# 中文 -> 英文。只放已经翻好的条目，没翻的自动回退中文。
EN = {
    # ---- 侧栏 ----
    "EPUB 检查器": "EPUB Checker",
    "比对": "Compare",
    "编辑工具…": "Edit tools…",
    "合并 EPUB…": "Merge EPUBs…",
    "合集检查…": "Collection check…",
    "当前页：基准（A）与待检（B）的文件比对": "Current page: compare baseline (A) with checked (B)",
    "当前页：检查目录、生成目录、加封面": "Current page: inspect/generate the TOC, add a cover",
    "当前页：把多本 EPUB 合并成一册": "Current page: merge several EPUBs into one",
    "当前页：核查分册是否都在合集里": "Current page: check whether every volume is inside the omnibus",
    "语言": "Language",
    "中文": "中文",
    "English": "English",
    "跟随系统": "Follow system",
    "就绪": "Ready",
    # ---- 比对页 ----
    "左：基准（A），右：待检（B）。点「开始比对」按 A×B 逐个比对并列出结论。":
        "Left: baseline (A). Right: checked (B). Press “Start” to compare every A×B pair.",
    "基准（A）": "Baseline (A)",
    "待检（B）": "Checked (B)",
    "待检（按此顺序）": "To merge (in this order)",
    "添加文件": "Add files",
    "添加文件夹": "Add folder",
    "移除选中": "Remove selected",
    "清空": "Clear",
    "把文件或文件夹拖到这里": "Drop files or folders here",
    "开始比对": "Start",
    "取消": "Cancel",
    "只看": "Show",
    "导出 CSV": "Export CSV",
    "任务表": "Tasks",
    "详细报告": "Details",
    "双击任务表某一行查看该文件的详细报告": "Double-click a row to see the per-book report",
    # ---- 表格列 ----
    "序号": "No.",
    "格式关系": "Formats",
    "结论": "Verdict",
    "差异摘要": "Summary",
    "备注": "Note",
    # ---- 结论 ----
    "⏳ 待执行": "⏳ Pending",
    "✓ 一致": "✓ Identical",
    "≈ 合集内含（内容一致）": "≈ Contained (same content)",
    "~ 部分一致": "~ Partial",
    "✗ 存在差异": "✗ Differences",
    "⛔ 无法比对": "⛔ Not comparable",
    "失败": "Failed",
    "已取消": "Cancelled",
    "跳过": "Skipped",
    "全部": "All",
    # ---- 运行时提示 ----
    "正在取消…": "Cancelling…",
    "已取消": "Cancelled",
    "完成": "Done",
    "解析基准": "Reading baseline",
    "解析待检": "Reading checked file",
    "比对": "Comparing",
    "共 %d 个任务，开始执行": "%d task(s), starting",
    "共 %d 个任务，开始等待": "%d task(s), queued",
    "比对完成：%d 个任务": "Finished: %d task(s)",
    "显示 %d / 共 %d 行": "Showing %d of %d row(s)",
    "已导出：%s": "Exported: %s",
}


def set_lang(lang: str) -> str:
    """lang 取 'zh' | 'en' | 'auto'；返回实际生效的语言。"""
    global _current
    if lang == "auto":
        lang = system_lang()
    if lang not in LANGS:
        lang = "zh"
    _current = lang
    return _current


def current() -> str:
    return _current


def system_lang() -> str:
    """Windows 显示语言：0x0804 等中文变体 -> zh，其余 -> en。"""
    try:
        import ctypes
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "zh" if (lcid & 0x3FF) == 0x04 else "en"
    except Exception:
        return "zh"


def tr(text: str, **kw) -> str:
    """把中文原文翻成当前语言；没译文就用原文。"""
    out = EN.get(text, text) if _current == "en" else text
    if kw:
        try:
            out = out % kw
        except (TypeError, KeyError, ValueError):
            pass
    return out

# ---- 结论列与过滤项文案（与 epub_check.STATUS_TEXT 一一对应）----
EN.update({
    "全部": "All",
    "… 待执行": "… Pending",
    "✓ 一致": "✓ Same",
    "≈ 合集内含（内容一致）": "≈ In collection",
    "✗ 存在差异": "✗ Different",
    "△ 部分一致": "△ Partly same",
    "· 无法比对": "· Not comparable",
    "⛔ 失败": "⛔ Failed",
    "— 已取消": "— Cancelled",
    "⏭ 跳过（同一文件）": "⏭ Skipped (same file)",
})

# pages

EN.update({
    '合集：': 'Collection:',
    '合集文件（EPUB/MOBI/PDF）': 'Collection file (EPUB/MOBI/PDF)',
    '浏览…': 'Browse…',
    '源分册：': 'Source volumes:',
    '添加分册…': 'Add volumes…',
    '移除选中': 'Remove selected',
    '开始检查': 'Start check',
    '取消': 'Cancel',
    '换一本…': 'Change book…',
    '第 1 步\u3000整理章节清单': 'Step 1 — Edit the chapter list',
    '每行一个章节；行首缩进两格或写 1.1 表示层级；行尾写 -> 文件名 可指定目标。选中书以后这里已经按书内目录填好，你只要增删改。': 'One chapter per line; indent by two spaces (or number it 1.1) to nest; append -> file.html to pin a target. After picking a book this is pre-filled from its own TOC — just edit it.',
    '从 txt / md / csv 导入…': 'Import from txt / md / csv…',
    '第 2 步\u3000检查目录（只看，不动原书）': 'Step 2 — Check the TOC (read-only, the book is untouched)',
    '检查目录结构': 'Check TOC structure',
    '第 3 步\u3000写进新文件（原书不动，另存一本）': 'Step 3 — Write a new file (original stays untouched)',
    '生成目录并另存…': 'Build TOC and save as…',
    '同时写 nav.xhtml 和 toc.ncx，写完自动自检。': 'Writes both nav.xhtml and toc.ncx, then self-checks.',
    '第 4 步\u3000修复合并后重复的目录项（父项与唯一子项同标题同链接）': 'Step 4 — Fix duplicated TOC entries left by merging (a parent and its only child share title and link)',
    '修复目录重复并另存…': 'Fix duplicates and save as…',
    '顺手把 ncx 的 playOrder 重排、dtb:depth 归 1；没有重复就不写文件。': 'Also renumbers ncx playOrder and sets dtb:depth to 1; writes nothing when there is nothing to fix.',
    '封面图（EPUB 只允许一张；原书已有会先问你）': 'Cover image (EPUB allows only one; you will be asked if the book already has one)',
    '（未选择图片）': '(no image selected)',
    '选择图片…': 'Choose image…',
    '清除': 'Clear',
    '添加封面并另存…': 'Add cover and save as…',
    '把封面图片拖到这里\n或点一下选择': 'Drop a cover image here\nor click to choose',
    '把 EPUB 拖到这里，或点一下选择': 'Drop an EPUB here, or click to choose',
    '书名：': 'Title:',
    '上移': 'Up',
    '下移': 'Down',
    '封面：': 'Cover:',
    '取第一本': 'From first book',
    '不要封面': 'No cover',
    '指定图片': 'Pick an image',
    '选图…': 'Pick…',
    '未选封面图片': 'No cover image chosen',
    '输出：': 'Output:',
    '开始合并': 'Merge',
    '打开所在文件夹': 'Open containing folder',
    '就绪': 'Ready',
    '待合并（按此顺序）  ': 'Books to merge (in this order)  ',
})

# headers
EN.update({
    '目录': 'TOC',
    '比对': 'Compare',
    '编辑工具': 'Edit tools',
    '合并 EPUB': 'Merge EPUBs',
    '合集检查': 'Collection check',
    '左：基准（A），右：待检（B）。点「开始比对」按 A×B 逐个比对并列出结论。': 'Left: baseline (A). Right: checked (B). Click Start to compare every A×B pair.',
    '只针对 EPUB：检查/生成目录、添加封面。把书拖进来即可。': 'EPUB only: inspect or build the TOC, add a cover. Drop a book here.',
    '按列表顺序把多本 EPUB 合成一本，另存新文件。': 'Merge several EPUBs into one, in list order, and save as a new file.',
    '检查合集里是否缺页、重复页、降质页。第一个文件当合集。': 'Check a collection for missing, duplicated or degraded pages. The first file is the collection.',
    '未选封面图片': 'No cover image chosen',
})

# merge
EN.update({
    '待合并（按此顺序）': 'Books to merge (in this order)',
})


# ---------------------------------------------------------------- 报告正文
# 报告是长文本、数字与文件名混排，这里按「行」翻译：先精确匹配，再套正则模板。
_REPORT_PATTERNS = [
    (r"^电子书完整性检查报告\s*[（(]单文件[）)]$", "EPUB integrity report (single file)"),
    (r"^CRC 校验 : 通过\s*[（(]无损坏条目[）)]$", "CRC check : passed (no corrupt entries)"),
    (r"^--- 章节\s*[（(]spine 顺序[）)]---$", "--- Chapters (spine order) ---"),
    (r"^图[（(]\d+[）)] : (.*)$", "Figure (１) : \3"),
    (r"^====+$", "="),
    (r"^电子书完整性检查报告（单文件）$", "EPUB integrity report (single file)"),
    (r"^文件 : (.*)$", "File : \1"),
    (r"^大小 : (.*)$", "Size : \1"),
    (r"^zip 条目 : (\d+) 个$", "Zip entries : \1"),
    (r"^CRC 校验 : (.*)$", "CRC check : \1"),
    (r"^通过（无损坏条目）$", "passed (no corrupt entries)"),
    (r"^container\.xml -> (.*)$", "container.xml -> \1"),
    (r"^OPF 版本 : (.*)$", "OPF version : \1"),
    (r"^manifest : (\d+) 项$", "manifest : \1 items"),
    (r"^spine : (\d+) 项$", "spine : \1 items"),
    (r"^文本文件 : (\d+) 个 / 图片 : (\d+) 个$", "Text files : \1 / Images : \2"),
    (r"^manifest 缺文件 : (\d+)$", "manifest missing files : \1"),
    (r"^spine 无效引用 : (\d+)$", "spine broken references : \1"),
    (r"^断链图片 : (\d+)$", "Broken images : \1"),
    (r"^孤儿图片 : (\d+)$", "Orphan images : \1"),
    (r"^--- 章节（spine 顺序）---$", "--- Chapters (spine order) ---"),
    (r"^\[问题\] ", "[Problem] "),
    (r"^\[提示\] ", "[Hint] "),
    (r"^结论 : 完整，未发现问题$", "Conclusion : complete, no problems found"),
    (r"^结论 : 发现 (\d+) 个问题$", "Conclusion : \1 problem(s) found"),
    (r"^结论 : (.*)$", "Conclusion : \1"),
    (r"^合集：(.*)$", "Collection: \1"),
    (r"^分册：(\d+) 本$", "Volumes: \1"),
    (r"^合并成功：(.*)$", "Merge finished: \1"),
    (r"^合并失败：(.*)$", "Merge failed: \1"),
    (r"^大小：(.*)$", "Size: \1"),
    (r"^—— 与「(.*)」的逐条差异 ——$", "—— Differences against \u300c\1\u300d ——"),
    (r"^内容相同，路径不同：(\d+) 个$", "Same content, different path: \1"),
    (r"^同路径但内容不同：(\d+) 个$", "Same path, different content: \1"),
    (r"^仅基准有：(\d+) 个$", "Baseline only: \1"),
    (r"^仅待检有：(\d+) 个$", "Checked only: \1"),
    (r"^基准 (\d+) 页 / 待检 (\d+) 页$", "Baseline \1 pages / Checked \2 pages"),
    (r"^能对应上：基准 (\d+) 页、待检 (\d+) 页$", "Paired: baseline \1, checked \2"),
    (r"^其中重复页：(\d+) 页（同一页在多处出现）$", "Duplicate pages: \1 (the same page appears more than once)"),
    (r"^内容覆盖 (\S+)%，相同的实质章节 (\d+) 个$",
     "Content coverage \1%, \2 substantial chapter(s) matched"),
    (r"^仅基准有 (\d+) 个，仅待检有 (\d+) 个$", "Baseline only \1, checked only \2"),
    (r"^图片 仅基准 (\d+) / 仅待检 (\d+)$", "Images — baseline only \1 / checked only \2"),
    (r"^页面图片比对：基准 (\d+) 页 / 待检 (\d+) 页，(\S+)% 能对应上$",
     "Page-image compare: baseline \1 / checked \2, \3% paired"),
    (r"^同一本书的不同打包：(.*)$", "Same book, different packaging: \1"),
    (r"^(\d+)/(\d+) 个文本在合集 (\S+) 中找到$", "\1/\2 text(s) found in the collection under \3"),
    (r"^，(\d+) 个同路径文件内容不同$", ", \1 same-path file(s) differ"),
    (r"^内容完全一致（(\d+) 个文本）$", "Identical content (\1 text files)"),
    (r"^目录检查：(.*)$", "TOC check: \1"),
    (r"^目录类型：(\S+)$", "TOC type: \1"),
    (r"^书内目录 (\d+) 条 / 清单 (\d+) 条 / 匹配 (\d+) 条$",
     "Book TOC \1 / list \2 / matched \3"),
    (r"^目录项指向的文件不存在：(.*)$", "TOC entry points to a missing file: \1"),
    (r"^同一个文件在目录里重复出现：(.*)$", "Same file appears twice in the TOC: \1"),
    (r"^正文有 (\d+) 个文档未被目录收录：(.*)$",
     "\1 document(s) are not in the TOC: \2"),
]

_REPORT_EXACT = {
    "未发现问题": "no problems found",
    "清单与书内目录一致": "the list matches the book TOC",
    "清单有，书里没有": "in the list but not in the book",
    "书里有，清单没有": "in the book but not in the list",
    "顺序不一致": "different order",
    "层级不一致": "different nesting",
    "详细报告　待检（B）：%s　｜　基准（A）：%s": "Details  Checked (B): %s  |  Baseline (A): %s",
    "详细报告：%s": "Details: %s",
}


def tr_report(text):
    """把一段中文报告翻成当前语言；没有对应的行原样保留。"""
    if current() != "en" or not text:
        return text
    out = []
    for line in text.split("\n"):
        key = line.strip()
        if not key:
            out.append(line)
            continue
        if key in EN:
            out.append(EN[key])
            continue
        if key in _REPORT_EXACT:
            out.append(_REPORT_EXACT[key])
            continue
        hit = None
        for pat, rep in _REPORT_PATTERNS:
            if any(1 <= ord(ch) <= 9 for ch in rep):
                rep = "".join(
                    ("\\" + str(ord(ch))) if 1 <= ord(ch) <= 9 else ch for ch in rep
                )
            m = re.match(pat, key)
            if m:
                hit = m.expand(rep) if "\\" in rep else rep
                if m.groups():
                    hit = m.expand(rep)
                break
        out.append(hit if hit is not None else line)
    return "\n".join(out)

