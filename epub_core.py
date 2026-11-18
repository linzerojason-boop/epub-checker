"""EPUB 检查器 · 引擎层（v3.0）。

解析 EPUB / MOBI / PDF，比对两个版本，调度批量任务。
本模块不依赖任何 GUI 库，可在无显示环境直接测试。
"""

import hashlib
import heapq
import os
import posixpath
import queue
import re
import struct
import threading
import time
import traceback
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import numpy as _NP
except ImportError:
    _NP = None

__version__ = "3.18"

from epub_parse import *  # noqa: F401,F403
from epub_parse import (local, resolve, rel_to_opf, fmt_size, is_text_media,
    is_image_media, get_opf_path, parse_opf, opf_text_images, normalize_text,
    extract_toc, _nav_tree, _ncx_tree, read_toc_tree, spine_docs, image_refs,
    cover_hrefs, css_url_refs, is_mobi_file, is_pdf_file, mobi_records,
    mobi_first_image_index, mobi_images, _jpeg_size, _is_page_jpeg,
    pdf_page_count, pdf_images, dhash, hamming, _thumb, _mad, _find_match,
    TEXT_MIN_AVG, MIN_PAGE_SIDE, K_CAND, MAD_THRESHOLD, DH_THRESHOLD)
from epub_check import (FMT_BY_EXT, STATUS_WAIT, STATUS_OK, STATUS_IN_COLL, STATUS_DIFF, STATUS_PART, STATUS_CANNOT, STATUS_FAIL, STATUS_CANCEL, STATUS_SKIP, ALL_STATUS, STATUS_TEXT, STATUS_COLOR, STATUS_TAG, TEXT_MIN_MEANINGFUL, TEXT_MIN_BOOK, IN_COLL_COVERAGE, PART_COVERAGE, PAGE_DUP_MAD, PAGE_FULL_COVERAGE, PAGE_CONTAIN_COVERAGE, _CACHE, _CACHE_MU, _KEY_MU, _IMG_CACHE, _IMG_CACHE_PAGES, _IMG_MU, _IMG_TOTAL, _result, _dominant_dir, coverage_stats, match_books, page_hashes, _text_chars, _no_meaningful_text, match_pages, _compare_pages, compare_books, book_image_dhashes, book_fulltext, detect_book, book_format, _norm_rel, _text_digest, _cache_key, book_manifest, book_info_cached, TaskRunner, check_single, check_pair, check_collection, toc_structure_problems)
from epub_edit import (_TOC_SEP, _TOC_TARGET, _TOC_NUM, _TITLE_STRIP, TocListError, parse_toc_list, normalize_title, toc_diff, toc_check, pair_toc_items, _esc, _rel_href, _tree_from_entries, build_nav_xhtml, build_ncx, patch_opf, write_epub_variant, verify_epub, COVER_MAX_SIDE, COVER_TARGET_SIDE, COVER_MAX_BYTES, prepare_cover, _opf_meta, default_dst, apply_toc, apply_cover, apply_both, _TOC_LI_PAIR, _TOC_NP_PAIR, _TOC_NAVPOINT, _fix_nav_dups, _fix_ncx_dups, fix_toc_dups, MergeError, _natural_key, sort_merge_inputs, _open_epub_parts, check_merge_inputs, MERGE_OPF, MERGE_NAV, MERGE_NCX, MERGE_COVER, _merge_container, _book_language, _collect_book, _copy_book_into, _merge_manifest_items, _merge_spine, _nest, _nav_ol, build_merged_nav, build_merged_ncx, build_merged_opf, _merge_uid, default_merge_dst, merge_books, _merge_book_prefix, _toc_indent_cols, _fail)

from epub_parse import (  # noqa: F401  解析层搬去了 epub_parse
    local, resolve, rel_to_opf, fmt_size, is_text_media, is_image_media,
    get_opf_path, parse_opf, opf_text_images, normalize_text, extract_toc,
    _nav_tree, _ncx_tree, read_toc_tree, spine_docs, image_refs, cover_hrefs,
    css_url_refs, is_mobi_file, is_pdf_file, mobi_records, mobi_first_image_index,
    mobi_images, _jpeg_size, _is_page_jpeg, pdf_page_count, pdf_images,
    dhash, hamming, _thumb, _mad, _find_match,
    TEXT_MIN_AVG, MIN_PAGE_SIDE, K_CAND, MAD_THRESHOLD, DH_THRESHOLD,
)


# 小书的页有这么多在大书里找到，就算「合集内含」（10 页里 1 页对不上属于正常）

# --- 跨书比对判定（v3.2）---
# 小于此字符数的文本视为样板页(titlepage/版权/封面), 不能当作"这两本书内容相同"的证据




























# 表格文字颜色（浅色, 深色主题下可读）。QColor 不认识 "ok" 这类标签名，
# 所以颜色单独一张表, 不要拿 STATUS_TAG 当颜色用。










































































# 解析缓存: Key = 绝对路径 + 大小 + mtime_ns; 同 Key 并发用单飞锁合并


















# 页面图嵌入式指纹占用小(一页约 1 KB)，按「总页数」限一下就行，
# 否则 12 卷对 2147 页合集时会把合集缓存挤掉、每次重解 2147 页图。














# ============ 合并 EPUB（v3.6） ============









































# ---------- 修复合并类工具产出的重复目录项（v3.10） ----------









