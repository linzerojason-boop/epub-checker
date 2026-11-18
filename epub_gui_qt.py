"""EPUB 检查器 · Qt 界面层（v3.0）。"""
import csv
import os
import posixpath
import sys
import threading
import time
import zipfile

from PySide6.QtCore import (QAbstractListModel, QAbstractTableModel, QEvent, QModelIndex,
                            QObject, QSettings, QSize, Qt, QSortFilterProxyModel,
                            QTimer, Signal)
from PySide6.QtGui import (QAction, QColor, QFont, QIcon, QKeySequence, QPixmap,
                            QShortcut)  # noqa: F401
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QDialog,
                               QFileDialog,
                               QFrame, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListView, QListWidget,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton, QStackedWidget, QStyle,
                               QStyledItemDelegate, QTableView, QTabWidget,
                               QVBoxLayout, QWidget, QGroupBox)

import epub_core as core
import epub_i18n as i18n

VERSION = "3.18"


def settings_path():
    """打包成 exe 后，配置放 exe 同目录；跑源码时放脚本同目录。"""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                            "epub_checker.ini")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "epub_checker.ini")


SETTINGS_FILE = settings_path()

# 文件对话框的起始目录（进程内记忆，跟着用过的文件走）
LAST_DIR = {"path": ""}


def start_dir():
    """本次文件对话框从哪个目录开始。"""
    p = LAST_DIR.get("path") or ""
    return p if p and os.path.isdir(p) else ""


def remember_dir(path):
    """记住某个文件所在目录。"""
    if path:
        LAST_DIR["path"] = os.path.dirname(os.path.abspath(path))


# ---------- 列表模型 ----------


class BookListModel(QAbstractListModel):
    """存文件路径的一列列表。"""

    def __init__(self, paths=None, parent=None):
        super().__init__(parent)
        self._paths = list(paths or [])

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._paths)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        p = self._paths[index.row()]
        if role == Qt.DisplayRole:
            return os.path.basename(p)
        if role == Qt.ToolTipRole:
            return p
        if role == Qt.UserRole:
            return p
        return None

    # 便捷接口
    def paths(self):
        return list(self._paths)

    def add_paths(self, paths):
        if not paths:
            return
        start = len(self._paths)
        self.beginInsertRows(QModelIndex(), start, start + len(paths) - 1)
        self._paths.extend(paths)
        self.endInsertRows()

    def remove_rows(self, rows):
        for r in sorted(set(rows), reverse=True):
            if 0 <= r < len(self._paths):
                self.beginRemoveRows(QModelIndex(), r, r)
                del self._paths[r]
                self.endRemoveRows()

    def clear(self):
        if not self._paths:
            return
        self.beginRemoveRows(QModelIndex(), 0, len(self._paths) - 1)
        self._paths = []
        self.endRemoveRows()

    def replace(self, paths):
        self.beginResetModel()
        self._paths = list(paths)
        self.endResetModel()


class BookDelegate(QStyledItemDelegate):
    """文件名 + 格式徽标 + 大小。"""

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QSize(s.width(), max(s.height(), 26))

    def paint(self, painter, option, index):
        # 交给系统画选中态，再叠自己的内容
        opt = option
        style = QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        path = index.data(Qt.UserRole)
        if not path:
            return
        fmt = core.book_format(path)
        try:
            size = os.path.getsize(path)
            size_txt = core.fmt_size(size)
        except OSError:
            size_txt = "?"
        painter.save()
        r = opt.rect.adjusted(6, 0, -6, 0)
        # 徽标
        color = {"EPUB": QColor("#1565c0"), "MOBI": QColor("#ef6c00"),
                 "PDF": QColor("#c62828")}.get(fmt, QColor("#546e7a"))
        badge = r.adjusted(0, 7, 0, -7)
        badge.setWidth(46)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(badge, 3, 3)
        painter.setPen(QColor("#ffffff"))
        f = QFont(opt.font)
        f.setPointSize(8)
        f.setBold(True)
        painter.setFont(f)
        painter.drawText(badge, Qt.AlignCenter, fmt)
        # 文件名
        painter.setPen(opt.palette.text().color())
        f2 = QFont(opt.font)
        painter.setFont(f2)
        name_rect = r.adjusted(badge.width() + 8, 0, -70, 0)
        name = os.path.basename(path)
        painter.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft,
                         painter.fontMetrics().elidedText(name, Qt.ElideMiddle, name_rect.width()))
        # 大小
        painter.setPen(QColor("#757575"))
        painter.drawText(r.adjusted(r.width() - 66, 0, 0, 0), Qt.AlignVCenter | Qt.AlignRight, size_txt)
        painter.restore()


class StatusFilterProxy(QSortFilterProxyModel):
    """按「结论」列的原始状态精确过滤（下拉里是状态名，单元格是带图标文案）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.status = ""

    def set_status(self, text):
        self.status = "" if text in ("", "全部") else text
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        if not self.status:
            return True
        m = self.sourceModel()
        if m is None:
            return True
        idx = m.index(row, 4, parent)
        return m.data(idx, Qt.UserRole) == self.status


# ---------- 任务表模型 ----------

COLUMNS = ["序号", "基准（A）", "待检（B）", "格式关系", "结论", "差异摘要", "备注"]
COLUMN_KEYS = list(COLUMNS)   # 原始中文 key，显示时过 i18n.tr()


class TaskTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []          # list[dict]
        self.headers = [i18n.tr(k) for k in COLUMN_KEYS]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.headers[section]
        return None

    def set_headers(self, heads):
        """换语言时整表重设表头。"""
        self.headers = list(heads)
        self.headerDataChanged.emit(Qt.Horizontal, 0, len(self.headers) - 1)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                return row["idx"]
            if col == 1:
                return os.path.basename(row["base"])
            if col == 2:
                return os.path.basename(row["check"])
            if col == 3:
                return "同格式" if row.get("fmt_rel") == "same" else "跨格式"
            if col == 4:
                return i18n.tr(core.STATUS_TEXT.get(row["status"], row["status"]))
            if col == 5:
                return row.get("summary", "")
            if col == 6:
                return row.get("reason", "")
            return ""
        if role == Qt.UserRole:
            return row["status"] if col == 4 else None
        if role == Qt.ToolTipRole:
            return f'{row["base"]}\n{row["check"]}\n{row.get("reason", "")}'
        if role == Qt.ForegroundRole:
            c = core.STATUS_COLOR.get(row["status"])
            return QColor(c) if c else None
        if role == Qt.BackgroundRole:
            if row["status"] == core.STATUS_FAIL:
                return QColor("#4a1f24")   # 深红, 浅色文字仍可读
            return None
        if role == Qt.UserRole:
            return row["status"]
        return None

    def add_row(self, row):
        self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows))
        self.rows.append(row)
        self.endInsertRows()

    def update_row(self, pos, **kw):
        self.rows[pos].update(kw)
        self.dataChanged.emit(self.index(pos, 0),
                              self.index(pos, len(COLUMNS) - 1))

    def clear(self):
        self.beginResetModel()
        self.rows = []
        self.endResetModel()


# ---------- 线程桥 ----------


class Bridge(QObject):
    row_ready = Signal(dict)
    stage_changed = Signal(dict)
    finished = Signal(dict)
    detail_ready = Signal(dict)


# ---------- 主窗口 ----------


def run_bg(fn, name="bg"):
    """把 fn() 丢到后台线程跑。界面线程只负责收回结果。
    fn 内部自己 try/except 并把结果用信号发回（Qt 会自动排队到主线程）。"""
    threading.Thread(target=fn, daemon=True, name=name).start()


def _default_merge_title(n):
    """默认合并书名：中文「合并_N本」，英文 Merged_N。"""
    return ("Merged_%d" if i18n.current() == "en" else "合并_%d本") % n


def _retr(items):
    """按 (控件, 方式, 原文) 重新设置文字；缺翻译时原样显示中文。"""
    for w, kind, key in items:
        text = i18n.tr(key)
        if kind == "text":
            w.setText(text)
        elif kind == "title":
            w.setTitle(text)


_HEADERS = []  # [(QLabel, 原文)]，供切语言时重新设置


def page_header(title, desc):
    """每页顶部的标题 + 一句话说明（文字可随语言切换）。"""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 6)
    lay.setSpacing(2)
    t = QLabel(i18n.tr(title))
    f = QFont()
    f.setBold(True)
    f.setPointSize(f.pointSize() + 3)
    t.setFont(f)
    d = QLabel(i18n.tr(desc))
    d.setWordWrap(True)
    d.setStyleSheet("color: #9e9e9e;")
    lay.addWidget(t)
    lay.addWidget(d)
    _HEADERS.append((t, title))
    _HEADERS.append((d, desc))
    return box


class ClickLabel(QLabel):
    """可点击的标签，用来当大块拖放区/预览区。"""

    clicked = Signal()

    def mouseReleaseEvent(self, ev):
        super().mouseReleaseEvent(ev)
        self.clicked.emit()


class BookPanel(QWidget):
    """一个上传面板：标题 + 列表 + 按钮行。"""

    add_paths = Signal(list)
    remove_selected = Signal()
    clear_all = Signal()
    pick_files = Signal()
    pick_dir = Signal()

    def __init__(self, title, role, parent=None):
        super().__init__(parent)
        self.role = role
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self._title_key = title
        self.lbl_title = QLabel(i18n.tr(title))
        f = QFont()
        f.setBold(True)
        self.lbl_title.setFont(f)
        self.lbl_count = QLabel("0")
        head.addWidget(self.lbl_title)
        head.addWidget(self.lbl_count)
        head.addStretch(1)
        lay.addLayout(head)

        self.view = QListView()
        self.view.setItemDelegate(BookDelegate(self.view))
        self.view.setUniformItemSizes(True)
        self.view.setSelectionMode(QListView.ExtendedSelection)
        self.view.setAcceptDrops(False)  # 拖放交给面板，避免列表吞事件
        lay.addWidget(self.view, 1)

        self.hint = QLabel(i18n.tr("把文件或文件夹拖到这里"), self.view)
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.hint.setStyleSheet("color: #8a8a8a;")
        self.view.viewport().installEventFilter(self)

        btns = QHBoxLayout()
        self.btn_add_file = QPushButton(i18n.tr("添加文件"))
        self.btn_add_dir = QPushButton(i18n.tr("添加文件夹"))
        self.btn_remove = QPushButton(i18n.tr("移除选中"))
        self.btn_clear = QPushButton(i18n.tr("清空"))
        for b in (self.btn_add_file, self.btn_add_dir, self.btn_remove, self.btn_clear):
            btns.addWidget(b)
        lay.addLayout(btns)

        self.btn_add_file.clicked.connect(self.pick_files.emit)
        self.btn_add_dir.clicked.connect(self.pick_dir.emit)
        self.btn_remove.clicked.connect(self.remove_selected.emit)
        self.btn_clear.clicked.connect(self.clear_all.emit)

    def eventFilter(self, obj, ev):
        """点列表空白处 = 添加文件；拖到列表上 = 加到本面板。

        QListView 的 viewport 会把拖放事件吃掉，不传回面板，所以这里必须自己接。
        """
        if obj is self.view.viewport():
            t = ev.type()
            if t in (QEvent.DragEnter, QEvent.DragMove):
                if ev.mimeData().hasUrls():
                    ev.acceptProposedAction()
                    return True
            elif t == QEvent.Drop:
                paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
                if paths:
                    self.add_paths.emit(paths)
                    ev.acceptProposedAction()
                    return True
            elif t == QEvent.MouseButtonRelease:
                if not self.view.indexAt(ev.position().toPoint()).isValid():
                    self.pick_files.emit()
        return super().eventFilter(obj, ev)

    def bind_model(self, model):
        self.view.setModel(model)
        for sig in (model.rowsInserted, model.rowsRemoved, model.modelReset):
            sig.connect(lambda *a: self._refresh_hint())
        self._refresh_hint()

    def apply_lang(self):
        """切语言时重设面板上自己的文字（标题 / 提示 / 四个按钮）。"""
        self.lbl_title.setText(i18n.tr(self._title_key))
        self.hint.setText(i18n.tr("把文件或文件夹拖到这里"))
        self.btn_add_file.setText(i18n.tr("添加文件"))
        self.btn_add_dir.setText(i18n.tr("添加文件夹"))
        self.btn_remove.setText(i18n.tr("移除选中"))
        self.btn_clear.setText(i18n.tr("清空"))

    def _refresh_hint(self):
        model = self.view.model()
        empty = model is None or model.rowCount() == 0
        self.hint.setVisible(empty)
        self.lbl_count.setText(str(0 if model is None else model.rowCount()))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        v = self.view
        w = QLabel.sizeHint(self.hint).width()
        self.hint.setGeometry((v.width() - w) // 2 if v.width() > w else 0,
                              max(0, v.height() // 2 - 12),
                              min(w, v.width()), 24)

    # -- 拖放（面板级） --
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.add_paths.emit(paths)
            ev.acceptProposedAction()


class CollectionDialog(QWidget):
    """合集检查：一个合集文件 + 若干源分册。"""

    report_ready = Signal(str)   # 工作线程 → 主线程（QTimer.singleShot 跨线程不可靠）


    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setWindowTitle("合集检查")
        self.resize(820, 620)
        self._busy = False
        lay = QVBoxLayout(self)

        row1 = QHBoxLayout()
        self.ed_collection = QLineEdit()
        self.ed_collection.setPlaceholderText("合集文件（EPUB/MOBI/PDF）")
        self.btn_browse = b1 = QPushButton(i18n.tr("浏览…"))
        b1.clicked.connect(self._pick_collection)
        self.lbl_coll = QLabel(i18n.tr("合集："))
        row1.addWidget(self.lbl_coll)
        row1.addWidget(self.ed_collection, 1)
        row1.addWidget(b1)
        lay.addLayout(row1)

        self.list_src = QListWidget()
        row2 = QHBoxLayout()
        self.btn_add_src = b2 = QPushButton(i18n.tr("添加分册…"))
        self.btn_rm_src = b3 = QPushButton(i18n.tr("移除选中"))
        b2.clicked.connect(self._add_sources)
        b3.clicked.connect(self._remove_sources)
        row2.addWidget(b2)
        row2.addWidget(b3)
        row2.addStretch(1)
        self.lbl_src = QLabel(i18n.tr("源分册："))
        lay.addWidget(self.lbl_src)
        lay.addWidget(self.list_src, 1)
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        self.btn_start = QPushButton(i18n.tr("开始检查"))
        self.btn_start.setEnabled(False)
        self.btn_start.clicked.connect(self._run)
        self.btn_cancel = QPushButton(i18n.tr("取消"))
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        row3.addWidget(self.btn_start)
        row3.addWidget(self.btn_cancel)
        row3.addWidget(self.progress, 1)
        lay.addLayout(row3)

        self.txt_result = QPlainTextEdit()
        self.txt_result.setReadOnly(True)
        lay.addWidget(self.txt_result, 2)

        self.ed_collection.textChanged.connect(self._refresh_start)
        self.list_src.model().rowsInserted.connect(self._refresh_start)
        self.list_src.model().rowsRemoved.connect(self._refresh_start)
        self.report_ready.connect(self._finish)

    # -- 拖入：第一个文件当合集，其余当分册 --
        self._tr_items = [
            (self.lbl_coll, "text", "合集："),
            (self.btn_browse, "text", "浏览…"),
            (self.lbl_src, "text", "源分册："),
            (self.btn_add_src, "text", "添加分册…"),
            (self.btn_rm_src, "text", "移除选中"),
            (self.btn_start, "text", "开始检查"),
            (self.btn_cancel, "text", "取消"),
        ]

    def apply_lang(self):
        _retr(self._tr_items)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        if not self.ed_collection.text().strip():
            self.ed_collection.setText(paths[0])
            paths = paths[1:]
        for p in paths:
            self._add_source(p)
        self._refresh_start()

    def _pick_collection(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选择合集文件", "",
            "电子书 (*.epub *.mobi *.azw *.azw3 *.prc *.pdf)")
        if p:
            self.ed_collection.setText(p)

    def _add_source(self, path):
        path = os.path.abspath(path)
        if not self.list_src.findItems(path, Qt.MatchExactly):
            self.list_src.addItem(path)

    def _add_sources(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择分册", "",
            "电子书 (*.epub *.mobi *.azw *.azw3 *.prc *.pdf)")
        for f in files:
            if not self.list_src.findItems(f, Qt.MatchExactly):
                self.list_src.addItem(f)
        self._refresh_start()

    def _remove_sources(self):
        for it in self.list_src.selectedItems():
            self.list_src.takeItem(self.list_src.row(it))
        self._refresh_start()

    def _refresh_start(self):
        ok = bool(self.ed_collection.text().strip()) and self.list_src.count() > 0 and not self._busy
        self.btn_start.setEnabled(ok)

    def _cancel(self):
        # 合集检查是 core 里的单次同步调用，无法中途打断，只能停止等待结果
        self._cancelled = True
        self.btn_cancel.setEnabled(False)
        self.txt_result.appendPlainText("\n[正在取消，等待当前步骤结束…]")

    def _run(self):
        coll = self.ed_collection.text().strip()
        srcs = [self.list_src.item(i).text() for i in range(self.list_src.count())]
        if not coll or not srcs:
            return
        self._busy = True
        self._cancelled = False
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.txt_result.setPlainText(f"合集：{coll}\n分册：{len(srcs)} 本\n\n检查中…")

        def work():
            try:
                rep = core.check_collection(coll, srcs)
            except Exception as e:
                rep = f"检查失败：{type(e).__name__}: {e}"
            self.report_ready.emit(rep)

        run_bg(work)

    def _finish(self, report):
        self._busy = False
        self.btn_cancel.setEnabled(False)
        self.progress.setVisible(False)
        self.txt_result.setPlainText(i18n.tr_report(report))
        self._refresh_start()


class EditDialog(QWidget):
    """EPUB 编辑工具：目录检查 / 生成目录 / 添加封面。"""

    check_ready = Signal(dict)
    apply_ready = Signal(dict)


    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle("EPUB 编辑工具")
        self.resize(880, 640)
        self._busy = False
        self._cover_path = None
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_toc_tab(), i18n.tr("目录"))
        self.tabs.addTab(self._build_cover_tab(), i18n.tr("封面"))
        self.btn_swap = QPushButton(i18n.tr("换一本…"))
        self.lbl_path = ClickLabel()
        self.lbl_path.setMinimumHeight(56)
        self.lbl_path.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.lbl_path.setStyleSheet(
            "border: 1px dashed #6b6b6b; border-radius: 6px; padding: 8px;")
        top = QHBoxLayout()
        top.addWidget(self.lbl_path, 1)
        top.addWidget(self.btn_swap)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.tabs, 1)
        self.lbl_path.clicked.connect(self._swap)
        self._show_book_line()
        self.check_ready.connect(self._on_check)
        self.apply_ready.connect(self._on_apply)
        self.btn_swap.clicked.connect(self._swap)
        self._prefill_from_book(path)
        self._refresh_enabled()

    # ---- 目录页 ----
        self._tr_items = [
            (self.btn_swap, "text", "换一本…"),
            (self.grp_list, "title", "第 1 步　整理章节清单"),
            (self.tip1, "text", "每行一个章节；行首缩进两格或写 1.1 表示层级；行尾写 -> 文件名 可指定目标。"
                               "选中书以后这里已经按书内目录填好，你只要增删改。"),
            (self.btn_import, "text", "从 txt / md / csv 导入…"),
            (self.grp_check, "title", "第 2 步　检查目录（只看，不动原书）"),
            (self.btn_check, "text", "检查目录结构"),
            (self.grp_gen, "title", "第 3 步　写进新文件（原书不动，另存一本）"),
            (self.btn_apply_toc, "text", "生成目录并另存…"),
            (self.tip3, "text", "同时写 nav.xhtml 和 toc.ncx，写完自动自检。"),
            (self.grp_fix, "title", "第 4 步　修复合并后重复的目录项（父项与唯一子项同标题同链接）"),
            (self.btn_fix_dups, "text", "修复目录重复并另存…"),
            (self.tip4, "text", "顺手把 ncx 的 playOrder 重排、dtb:depth 归 1；没有重复就不写文件。"),
            (self.grp_cover, "title", "封面图（EPUB 只允许一张；原书已有会先问你）"),
            (self.btn_pick_cover, "text", "选择图片…"),
            (self.btn_clear_cover, "text", "清除"),
            (self.btn_apply_cover, "text", "添加封面并另存…"),
        ]

    def apply_lang(self):
        _retr(self._tr_items)
        self.tabs.setTabText(0, i18n.tr("目录"))
        self.tabs.setTabText(1, i18n.tr("封面"))

    def _build_toc_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(8)

        self.grp_list = QGroupBox(i18n.tr("第 1 步　整理章节清单"))
        g1 = QVBoxLayout(self.grp_list)
        self.tip1 = QLabel(i18n.tr("每行一个章节；行首缩进两格或写 1.1 表示层级；行尾写 -> 文件名 可指定目标。"
                      "选中书以后这里已经按书内目录填好，你只要增删改。"))
        self.tip1.setWordWrap(True)
        self.tip1.setStyleSheet("color: #9e9e9e;")
        g1.addWidget(self.tip1)
        self.txt_list = QPlainTextEdit()
        self.txt_list.setPlaceholderText(
            "第1章 开端 -> chapter1.xhtml\n  1.1 小标题 -> chapter1-1.xhtml")
        g1.addWidget(self.txt_list, 1)
        self.btn_import = QPushButton(i18n.tr("从 txt / md / csv 导入…"))
        g1.addWidget(self.btn_import, 0, Qt.AlignLeft)
        lay.addWidget(self.grp_list, 2)

        self.grp_check = QGroupBox(i18n.tr("第 2 步　检查目录（只看，不动原书）"))
        g2 = QVBoxLayout(self.grp_check)
        self.btn_check = QPushButton(i18n.tr("检查目录结构"))
        g2.addWidget(self.btn_check, 0, Qt.AlignLeft)
        self.txt_report = QPlainTextEdit()
        self.txt_report.setReadOnly(True)
        self.txt_report.setMaximumBlockCount(20000)
        g2.addWidget(self.txt_report, 1)
        lay.addWidget(self.grp_check, 3)

        self.grp_gen = QGroupBox(i18n.tr("第 3 步　写进新文件（原书不动，另存一本）"))
        g3 = QHBoxLayout(self.grp_gen)
        self.btn_apply_toc = QPushButton(i18n.tr("生成目录并另存…"))
        self.tip3 = QLabel(i18n.tr("同时写 nav.xhtml 和 toc.ncx，写完自动自检。"))
        self.tip3.setStyleSheet("color: #9e9e9e;")
        g3.addWidget(self.btn_apply_toc)
        g3.addWidget(self.tip3, 1)
        lay.addWidget(self.grp_gen)

        self.grp_fix = QGroupBox(i18n.tr("第 4 步　修复合并后重复的目录项（父项与唯一子项同标题同链接）"))
        g4 = QHBoxLayout(self.grp_fix)
        self.btn_fix_dups = QPushButton(i18n.tr("修复目录重复并另存…"))
        self.tip4 = QLabel(i18n.tr("顺手把 ncx 的 playOrder 重排、dtb:depth 归 1；没有重复就不写文件。"))
        self.tip4.setStyleSheet("color: #9e9e9e;")
        g4.addWidget(self.btn_fix_dups)
        g4.addWidget(self.tip4, 1)
        lay.addWidget(self.grp_fix)

        self.btn_import.clicked.connect(self._import_list)
        self.btn_check.clicked.connect(self._do_check)
        self.btn_apply_toc.clicked.connect(self._do_apply_toc)
        self.btn_fix_dups.clicked.connect(self._do_fix_dups)
        return w

    def _import_list(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "导入章节清单", "", "清单 (*.txt *.md *.csv);;所有文件 (*.*)")
        if not p:
            return
        for enc in ("utf-8-sig", "gbk", "utf-16"):
            try:
                with open(p, "r", encoding=enc) as f:
                    self.txt_list.setPlainText(f.read())
                return
            except UnicodeDecodeError:
                continue
        QMessageBox.warning(self, "导入失败", "编码无法识别，请另存为 UTF-8 后重试")

    def _items(self):
        try:
            return core.parse_toc_list(self.txt_list.toPlainText()), None
        except core.TocListError as exc:
            return None, str(exc)

    def _do_check(self):
        items, err = self._items()
        if err:
            QMessageBox.warning(self, "清单有问题", err)
            return
        self._busy = True
        self._refresh_enabled()
        path = self.path

        def work():
            try:
                report, probs = core.toc_check(path, items)
            except Exception as exc:
                report, probs = "检查失败：%s: %s" % (type(exc).__name__, exc), []
            self.check_ready.emit({"report": report, "problems": probs})

        run_bg(work)

    def _on_check(self, ev):
        self._busy = False
        self.txt_report.setPlainText(i18n.tr_report(ev.get("report") or ""))
        self._refresh_enabled()

    def _do_apply_toc(self):
        items, err = self._items()
        if err:
            QMessageBox.warning(self, "清单有问题", err)
            return
        dst, _ = QFileDialog.getSaveFileName(
            self, "另存为", core.default_dst(self.path, "加目录"), "EPUB (*.epub)")
        if not dst:
            return
        self._busy = True
        self._refresh_enabled()
        src = self.path

        def work():
            try:
                out, probs = core.apply_toc(src, items, dst=dst)
            except Exception as exc:
                out, probs = None, ["%s: %s" % (type(exc).__name__, exc)]
            self.apply_ready.emit({"out": out, "problems": probs})

        run_bg(work)

    # ---- 封面页 ----
    def _build_cover_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(8)

        self.grp_cover = QGroupBox(i18n.tr("封面图（EPUB 只允许一张；原书已有会先问你）"))
        box = QVBoxLayout(self.grp_cover)
        self.lbl_preview = ClickLabel("把封面图片拖到这里\n或点一下选择")
        self.lbl_preview.setAlignment(Qt.AlignCenter)
        self.lbl_preview.setMinimumSize(240, 240)
        self.lbl_preview.setMaximumHeight(300)
        self.lbl_preview.setStyleSheet(
            "border: 1px dashed #6b6b6b; border-radius: 6px; color: #9e9e9e;")
        self.lbl_cover = QLabel(i18n.tr("（未选择图片）"))
        self.lbl_cover.setWordWrap(True)
        row = QHBoxLayout()
        self.btn_pick_cover = QPushButton(i18n.tr("选择图片…"))
        self.btn_clear_cover = QPushButton(i18n.tr("清除"))
        self.btn_apply_cover = QPushButton(i18n.tr("添加封面并另存…"))
        row.addWidget(self.btn_pick_cover)
        row.addWidget(self.btn_clear_cover)
        row.addStretch(1)
        row.addWidget(self.btn_apply_cover)
        box.addWidget(self.lbl_preview, 0, Qt.AlignHCenter)
        box.addWidget(self.lbl_cover)
        box.addLayout(row)
        lay.addWidget(self.grp_cover, 1)
        self.lbl_preview.clicked.connect(self._pick_cover)
        self.btn_pick_cover.clicked.connect(self._pick_cover)
        self.btn_clear_cover.clicked.connect(self._clear_cover)
        self.btn_apply_cover.clicked.connect(self._do_apply_cover)
        return w

    COVER_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")

    def _set_cover_path(self, p):
        self._cover_path = p
        if not p:
            self.lbl_cover.setText("（未选择图片）")
            self.lbl_preview.setPixmap(QPixmap())
            self.lbl_preview.setText("把封面图片拖到这里\n或点一下选择")
            return
        remember_dir(p)
        self.lbl_cover.setText("已选：%s" % p)
        pix = QPixmap(p)
        if pix.isNull():
            self.lbl_preview.setText("图片读不出来：\n%s" % os.path.basename(p))
            return
        self.lbl_preview.setText("")
        self.lbl_preview.setPixmap(
            pix.scaled(240, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _clear_cover(self):
        self._set_cover_path(None)

    def _pick_cover(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选择封面图", start_dir(), "图片 (*.png *.jpg *.jpeg *.webp)")
        if p:
            self._set_cover_path(p)

    def _do_apply_cover(self):
        if not self._cover_path:
            QMessageBox.warning(self, "没有封面", "请先选择封面图片")
            return
        has = self._has_cover()
        if has:
            box = QMessageBox(self)
            box.setWindowTitle("已有封面")
            box.setText("这本书已有封面（%s）。" % has)
            box.addButton("跳过", QMessageBox.RejectRole)
            rep = box.addButton("替换", QMessageBox.AcceptRole)
            box.addButton("取消", QMessageBox.DestructiveRole)
            box.exec()
            if box.clickedButton() is not rep:
                return
        dst, _ = QFileDialog.getSaveFileName(
            self, "另存为", core.default_dst(self.path, "加封面"), "EPUB (*.epub)")
        if not dst:
            return
        self._busy = True
        self._refresh_enabled()
        src = self.path
        cover = self._cover_path

        def work():
            try:
                out, probs = core.apply_cover(src, cover, dst=dst,
                                              replace_cover=bool(has))
            except Exception as exc:
                out, probs = None, ["%s: %s" % (type(exc).__name__, exc)]
            self.apply_ready.emit({"out": out, "problems": probs})

        run_bg(work)

    def _do_fix_dups(self):
        if not self.path:
            QMessageBox.warning(self, "没有书", "请先选一本 EPUB")
            return
        src = self.path
        self._busy = True
        self._refresh_enabled()

        def work():
            try:
                out, nav_n, ncx_n = core.fix_toc_dups(src)
                if out is None:
                    msg = "没有发现重复目录项，未写新文件。"
                else:
                    msg = ("已折叠 nav %d 条 / ncx %d 条，输出：%s（%.1f MB）"
                           % (nav_n, ncx_n, out,
                              (os.path.getsize(out) / 1048576.0)
                              if os.path.exists(out) else 0.0))
            except Exception as exc:
                msg = "修复失败：%s: %s" % (type(exc).__name__, exc)
            self.check_ready.emit({"report": msg, "problems": []})

        run_bg(work)

    def _has_cover(self):
        import zipfile
        if not self.path:
            return ""
        try:
            with zipfile.ZipFile(self.path) as z:
                opf = core.parse_opf(z, core.get_opf_path(z))
                covers = core.cover_hrefs(z, opf)
            return "、".join(sorted(os.path.basename(c) for c in covers))
        except Exception:
            return ""

    # ---- 共用 ----
    def _on_apply(self, ev):
        self._busy = False
        if ev.get("problems"):
            QMessageBox.warning(self, "没有写成功", "\n".join(ev["problems"][:10]))
            self.txt_report.appendPlainText(
                "\n写入失败：%s" % "；".join(ev["problems"][:5]))
        else:
            self.txt_report.appendPlainText("\n已另存：%s" % ev.get("out"))
        self._refresh_enabled()

    def _refresh_enabled(self):
        ok = bool(self.path) and core.book_format(self.path) == "EPUB" and not self._busy
        for b in (getattr(self, "btn_check", None),
                  getattr(self, "btn_apply_toc", None),
                  getattr(self, "btn_fix_dups", None),
                  getattr(self, "btn_apply_cover", None),
                  getattr(self, "btn_import", None),
                  getattr(self, "btn_pick_cover", None)):
            if b is not None:
                b.setEnabled(ok)

    def _swap(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选择 EPUB", start_dir(), "EPUB (*.epub)")
        if p:
            self._set_path(p)

    def _show_book_line(self):
        """顶部那块显示当前书（没选书就是提示语）。"""
        if not self.path:
            self.lbl_path.setText("把 EPUB 拖到这里，或点一下选择")
            self.lbl_path.setToolTip("")
            return
        try:
            size = core.fmt_size(os.path.getsize(self.path))
        except OSError:
            size = "?"
        self.lbl_path.setText("%s\n%s" % (posixpath.basename(self.path), size))
        self.lbl_path.setToolTip(self.path)

    def _set_path(self, p):
        self.path = p
        remember_dir(p)
        self._show_book_line()
        self._prefill_from_book(p, force=True)
        self._refresh_enabled()

    def _prefill_from_book(self, path, force=False):
        """用书里现有目录填一份可编辑的草稿；换书时强制刷新。"""
        if not force and self.txt_list.toPlainText().strip():
            return
        tree = []
        try:
            with zipfile.ZipFile(path) as z:
                opf = core.parse_opf(z, core.get_opf_path(z))
                tree = core.read_toc_tree(z, opf)
        except Exception:
            tree = []
        lines = []
        for it in tree:
            href = (it.get("href") or "").strip()
            if not href:
                continue
            low = posixpath.basename(href).lower()
            if low.startswith("nav.") or low.startswith("toc.") or low.startswith("cover."):
                continue
            lv = max(1, min(int(it.get("level") or 1), 4))
            title = (it.get("title") or "").strip() or posixpath.basename(href)
            lines.append("  " * (lv - 1) + "%s -> %s" % (title, posixpath.basename(href)))
        if lines:
            self.txt_list.setPlainText("\n".join(lines))

    # -- 拖放：拖进来的第一本当作要编辑的书 --
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        p = paths[0]
        is_img = p.lower().endswith(self.COVER_EXTS)
        if is_img and self.path:
            # 在封面页（或选中书以后）拖入图片 = 设封面
            self._set_cover_path(p)
            self.tabs.setCurrentIndex(1)
            return
        self._set_path(p)


class MergeDialog(QWidget):
    """合并 EPUB：按列表顺序合成一本，另存新文件。"""

    merge_done = Signal(dict)
    stage_changed = Signal(str)


    def __init__(self, parent=None, paths=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setWindowTitle("合并 EPUB")
        self.resize(760, 600)
        self.model = BookListModel(None, self)
        self._busy = False
        self._cover_file = ""
        self._out_touched = False

        lay = QVBoxLayout(self)

        row_title = QHBoxLayout()
        self.lbl_title_key = QLabel(i18n.tr("书名："))
        row_title.addWidget(self.lbl_title_key)
        self.ed_title = QLineEdit()
        self.ed_title.setText("合并_2本")
        self.ed_title.setPlaceholderText("合并_2本")
        self.ed_title.setAcceptDrops(False)  # 拖文件到书名框时交给整页处理，别当文本吞掉
        row_title.addWidget(self.ed_title, 1)
        lay.addLayout(row_title)

        self.panel = BookPanel("待合并（按此顺序）", "merge")
        self.panel.view.setModel(self.model)
        self.panel.bind_model(self.model)  # 让「拖到这里」提示与文件数跟着列表变
        self.panel.add_paths.connect(self._add_paths)
        self.panel.remove_selected.connect(self._remove_selected)
        self.panel.clear_all.connect(self._clear)
        self.panel.pick_files.connect(self._pick_files)
        self.panel.pick_dir.connect(self._pick_dir)
        lay.addWidget(self.panel, 1)

        row_order = QHBoxLayout()
        self.btn_up = QPushButton(i18n.tr("上移"))
        self.btn_down = QPushButton(i18n.tr("下移"))
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down.clicked.connect(lambda: self._move(1))
        row_order.addWidget(self.btn_up)
        row_order.addWidget(self.btn_down)
        row_order.addStretch(1)
        self.lbl_cover_key = QLabel(i18n.tr("封面："))
        row_order.addWidget(self.lbl_cover_key)
        self.rb_auto = QRadioButton(i18n.tr("取第一本"))
        self.rb_none = QRadioButton(i18n.tr("不要封面"))
        self.rb_file = QRadioButton(i18n.tr("指定图片"))
        self.rb_auto.setChecked(True)
        for rb in (self.rb_auto, self.rb_none, self.rb_file):
            rb.toggled.connect(self._refresh_start)
            row_order.addWidget(rb)
        self.btn_pick_cover = QPushButton(i18n.tr("选图…"))
        self.btn_pick_cover.clicked.connect(self._pick_cover)
        row_order.addWidget(self.btn_pick_cover)
        lay.addLayout(row_order)

        self.lbl_cover = QLabel(i18n.tr("未选封面图片"))
        lay.addWidget(self.lbl_cover)

        row_out = QHBoxLayout()
        self.lbl_out_key = QLabel(i18n.tr("输出："))
        row_out.addWidget(self.lbl_out_key)
        self.ed_out = QLineEdit()
        self.ed_out.textChanged.connect(self._out_changed)
        row_out.addWidget(self.ed_out, 1)
        self.btn_out = QPushButton(i18n.tr("浏览…"))
        self.btn_out.clicked.connect(self._pick_out)
        row_out.addWidget(self.btn_out)
        lay.addLayout(row_out)

        row_go = QHBoxLayout()
        self.btn_start = QPushButton(i18n.tr("开始合并"))
        self.btn_start.setObjectName("Accent")
        self.btn_start.clicked.connect(self._run)
        self.btn_cancel = QPushButton(i18n.tr("取消"))
        self.btn_cancel.clicked.connect(self._cancel_merge)
        self.btn_open = QPushButton(i18n.tr("打开所在文件夹"))
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self._open_dir)
        row_go.addWidget(self.btn_start)
        row_go.addWidget(self.btn_cancel)
        row_go.addWidget(self.btn_open)
        row_go.addStretch(1)
        lay.addLayout(row_go)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        lay.addWidget(self.progress)
        self.stage = QLabel(i18n.tr("就绪"))
        lay.addWidget(self.stage)

        self.txt_result = QPlainTextEdit()
        self.txt_result.setReadOnly(True)
        self.txt_result.setMaximumBlockCount(5000)
        lay.addWidget(self.txt_result, 1)

        self.merge_done.connect(self._on_done)
        self.stage_changed.connect(self.stage.setText)

        if paths:
            self._add_paths(list(paths))
        self._refresh_start()

    # ---------- 列表 ----------
        self._tr_items = [
            (self.lbl_title_key, "text", "书名："),
            (self.btn_up, "text", "上移"),
            (self.btn_down, "text", "下移"),
            (self.lbl_cover_key, "text", "封面："),
            (self.rb_auto, "text", "取第一本"),
            (self.rb_none, "text", "不要封面"),
            (self.rb_file, "text", "指定图片"),
            (self.btn_pick_cover, "text", "选图…"),
            (self.lbl_out_key, "text", "输出："),
            (self.btn_out, "text", "浏览…"),
            (self.btn_start, "text", "开始合并"),
            (self.btn_cancel, "text", "取消"),
            (self.btn_open, "text", "打开所在文件夹"),
        ]

    def apply_lang(self):
        _retr(self._tr_items)
        self.panel.apply_lang()
        if not getattr(self, "_running", False) and not getattr(self, "_busy", False):
            self.stage.setText(i18n.tr("就绪"))
        if not getattr(self, "_cover_file", ""):
            self.lbl_cover.setText(i18n.tr("未选封面图片"))

    def _cancel_merge(self):
        """合并是单次同步调用，没有中断点；这里只停掉等待状态。"""
        self.stage.setText("已取消（当前步骤结束后停止）")
        self.btn_cancel.setEnabled(False)

    # -- 拖入：按拖入顺序追加 --
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self._add_paths(paths)
            self._refresh_start()

    def _add_paths(self, paths):
        added = 0
        rejected = []
        for raw in paths:
            p = os.path.abspath(raw)
            if not os.path.exists(p):
                rejected.append("%s：路径不存在" % p)
                continue
            if os.path.isdir(p):
                for name in sorted(os.listdir(p)):
                    sub = os.path.join(p, name)
                    if os.path.isfile(sub) and core.book_format(sub) == "EPUB":
                        if core.book_format(sub) == "EPUB" and sub not in self.model.paths():
                            self.model.add_paths([sub])
                            added += 1
                continue
            if core.book_format(p) != "EPUB":
                rejected.append("%s：只能合并 EPUB" % os.path.basename(p))
                continue
            if os.path.getsize(p) == 0:
                rejected.append("%s：0 字节文件" % os.path.basename(p))
                continue
            if p in self.model.paths():
                continue
            self.model.add_paths([p])
            added += 1
        self.panel.lbl_count.setText(str(self.model.rowCount()))
        if not self.ed_title.text().strip() or not self._out_touched:
            self.ed_title.setText(_default_merge_title(max(self.model.rowCount(), 2)))
            self._out_touched = False
            self._sync_out()
        if rejected:
            self.txt_result.setPlainText("\n".join(rejected))
        self._refresh_start()

    def _remove_selected(self):
        idxs = [i.row() for i in self.panel.view.selectionModel().selectedIndexes()]
        if idxs:
            self.model.remove_rows(idxs)
        self.panel.lbl_count.setText(str(self.model.rowCount()))
        self._sync_out()
        self._refresh_start()

    def _clear(self):
        self.model.clear()
        self.panel.lbl_count.setText("0")
        self._sync_out()
        self._refresh_start()

    def _move(self, delta):
        rows = [i.row() for i in self.panel.view.selectionModel().selectedIndexes()]
        if len(rows) != 1:
            return
        i = rows[0]
        j = i + delta
        ps = self.model.paths()
        if 0 <= j < len(ps):
            ps[i], ps[j] = ps[j], ps[i]
            self.model.replace(ps)
            self._sync_out()

    def _pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "添加 EPUB", "", "电子书 (*.epub)")
        if files:
            self._add_paths(files)

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "添加文件夹")
        if d:
            self._add_paths([d])

    # ---------- 封面 ----------
    def _pick_cover(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择封面图片", "", "图片 (*.jpg *.jpeg *.png *.webp)")
        if not path:
            return
        self._cover_file = path
        self.rb_file.setChecked(True)
        pix = QPixmap(path)
        if not pix.isNull():
            self.lbl_cover.setPixmap(pix.scaledToHeight(90, Qt.SmoothTransformation))
        self.lbl_cover.setToolTip(path)
        self._refresh_start()

    def _cover_arg(self):
        if self.rb_none.isChecked():
            return "none"
        if self.rb_file.isChecked():
            return self._cover_file or None
        return None

    # ---------- 输出路径 ----------
    def _out_changed(self, text):
        if text.strip():
            self._out_touched = True

    def _sync_out(self):
        ps = self.model.paths()
        if not ps:
            self.ed_out.setText("")
            self._out_touched = False
            return
        title = self.ed_title.text().strip() or _default_merge_title(len(ps))
        self.ed_out.setText(core.default_merge_dst(ps, title))
        self._out_touched = False

    def _pick_out(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "另存为", self.ed_out.text() or "", "EPUB (*.epub)")
        if path:
            if not path.lower().endswith(".epub"):
                path += ".epub"
            self.ed_out.setText(path)
            self._out_touched = True
            self._refresh_start()

    # ---------- 运行 ----------
    def _refresh_start(self):
        ok = (not self._busy and self.model.rowCount() >= 2
              and all(core.book_format(p) == "EPUB" for p in self.model.paths())
              and bool(self.ed_out.text().strip())
              and (not self.rb_file.isChecked() or bool(self._cover_file)))
        self.btn_start.setEnabled(ok)
        self.btn_pick_cover.setEnabled(self.rb_file.isChecked())

    def _lock(self, busy):
        self._busy = busy
        for w in (self.panel, self.ed_title, self.ed_out, self.btn_out,
                  self.btn_up, self.btn_down, self.rb_auto, self.rb_none,
                  self.rb_file, self.btn_pick_cover, self.btn_cancel):
            w.setEnabled(not busy)
        if busy:
            self.btn_start.setEnabled(False)
        else:
            self._refresh_start()

    def _run(self):
        ps = self.model.paths()
        if len(ps) < 2:
            return
        dst = self.ed_out.text().strip()
        title = self.ed_title.text().strip() or _default_merge_title(len(ps))
        cover = self._cover_arg()
        self._lock(True)
        self.progress.setValue(0)
        self.progress.setMaximum(len(ps) + 1)
        self.stage.setText("开始合并…")
        self.txt_result.clear()

        def work():
            def prog(i, n, text):
                self.stage_changed.emit("第 %d/%d 本：%s" % (i, n, text))
            try:
                out = core.merge_books(ps, title, dst=dst or None, cover=cover,
                                       progress=prog)
                self.merge_done.emit({"ok": True, "path": out})
            except Exception as exc:  # noqa: BLE001 - 交给界面显示
                self.merge_done.emit({"ok": False, "err": "%s: %s" % (type(exc).__name__, exc)})

        run_bg(work)

    def _on_done(self, ev):
        self._lock(False)
        if ev.get("ok"):
            self.progress.setValue(self.progress.maximum())
            self.stage.setText("完成")
            size = core.fmt_size(os.path.getsize(ev["path"]))
            self.txt_result.setPlainText(i18n.tr_report("合并成功：%s\n大小：%s" % (ev["path"], size)))
            self.btn_open.setEnabled(True)
        else:
            self.stage.setText("失败")
            self.txt_result.setPlainText(i18n.tr_report("合并失败：%s" % ev.get("err", "")))

    def _open_dir(self):
        path = self.ed_out.text().strip()
        if not path:
            return
        try:
            os.startfile(os.path.dirname(os.path.abspath(path)))  # noqa: S606
        except OSError as exc:
            QMessageBox.warning(self, "打开失败", "无法打开文件夹：%s" % exc)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"EPUB 检查器 v{VERSION}")
        self.resize(1180, 760)
        self.model_a = BookListModel()
        self.model_b = BookListModel()
        self.task_model = TaskTableModel()
        self.proxy = StatusFilterProxy(self)
        self.proxy.setSourceModel(self.task_model)
        self.proxy.setFilterKeyColumn(4)
        self.bridge = Bridge(self)
        self._running = False
        self.runner = None
        self._done_count = 0
        self._cancel_requested = False
        self._done_ev = threading.Event()
        self._shortcuts = {}
        self._build()
        self._setup_shortcuts()
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_row_menu)
        self._load_settings()
        self._apply_lang(str(self._settings().value("lang") or "auto"), save=False)
        self.bridge.row_ready.connect(self._on_row)
        self.bridge.stage_changed.connect(self._on_stage)
        self.bridge.finished.connect(self._on_done)
        self.bridge.detail_ready.connect(self._on_detail)
        self.btn_start.clicked.connect(self.start_compare)
        self.btn_cancel.clicked.connect(self.cancel_compare)
        self.btn_collection.clicked.connect(self.open_collection_dialog)
        self.btn_edit.clicked.connect(self.open_edit_dialog)

    # ---------- 语言 ----------

    def _apply_lang(self, lang, save=True):
        """lang: 'zh' | 'en' | 'auto'。切完立即重刷样板页的文案。"""
        real = i18n.set_lang(lang)
        want = lang if lang in ("zh", "en", "auto") else "auto"
        if hasattr(self, "combo_lang"):
            j = self.combo_lang.findData(want)
            if j >= 0 and self.combo_lang.currentIndex() != j:
                self.combo_lang.blockSignals(True)
                self.combo_lang.setCurrentIndex(j)
                self.combo_lang.blockSignals(False)
        if save:
            s = self._settings()
            s.setValue("lang", lang)
            s.sync()
        self._retranslate()
        return real

    def _on_lang_changed(self, _idx):
        self._apply_lang(str(self.combo_lang.currentData() or "auto"))

    def _retranslate(self):
        """把已翻好的部分（侧栏 + 比对页）按当前语言重设一遍。"""
        if hasattr(self, "lbl_app_title"):
            self.lbl_app_title.setText(i18n.tr("EPUB 检查器"))
        for btn, key, tip in (
            (getattr(self, "btn_mode_compare", None), "比对",
             "当前页：基准（A）与待检（B）的文件比对"),
            (getattr(self, "btn_edit", None), "编辑工具…",
             "当前页：检查目录、生成目录、加封面"),
            (getattr(self, "btn_merge", None), "合并 EPUB…", "当前页：把多本 EPUB 合并成一册"),
            (getattr(self, "btn_collection", None), "合集检查…",
             "当前页：核查分册是否都在合集里"),
        ):
            if btn is not None:
                btn.setText(i18n.tr(key))
                btn.setToolTip(i18n.tr(tip))
        if hasattr(self, "lbl_lang"):
            self.lbl_lang.setText(i18n.tr("语言"))
            for i, (k, _d) in enumerate((("中文", "zh"), ("English", "en"),
                                         ("跟随系统", "auto"))):
                self.combo_lang.setItemText(i, k if k == "English" else i18n.tr(k))
        for panel in (getattr(self, "panel_a", None), getattr(self, "panel_b", None)):
            if panel is None:
                continue
            panel.lbl_title.setText(i18n.tr(panel._title_key))
            panel.hint.setText(i18n.tr("把文件或文件夹拖到这里"))
            panel.btn_add_file.setText(i18n.tr("添加文件"))
            panel.btn_add_dir.setText(i18n.tr("添加文件夹"))
            panel.btn_remove.setText(i18n.tr("移除选中"))
            panel.btn_clear.setText(i18n.tr("清空"))
        for name, key in (("btn_start", "开始比对"), ("btn_cancel", "取消"),
                          ("btn_export", "导出 CSV")):
            w = getattr(self, name, None)
            if w is not None:
                w.setText(i18n.tr(key))
        if hasattr(self, "lbl_filter"):
            self.lbl_filter.setText(i18n.tr("只看"))
        if hasattr(self, "task_model"):
            self.task_model.set_headers([i18n.tr(k) for k in COLUMN_KEYS])
        if hasattr(self, "tabs"):
            self.tabs.setTabText(0, i18n.tr("任务表"))
            if self.tabs.count() > 1:
                self.tabs.setTabText(1, i18n.tr("详细报告"))
        if hasattr(self, "detail_bar"):
            self.detail_bar.setText(i18n.tr("双击任务表某一行查看该文件的详细报告"))
        if hasattr(self, "combo_filter"):
            # 过滤项的文字也要跟着换；重建后按 itemData 还原选中项
            cur = str(self.combo_filter.currentData() or "")
            old = self.combo_filter.blockSignals(True)
            self.combo_filter.clear()
            self.combo_filter.addItem(i18n.tr("全部"), "")
            for _st in core.ALL_STATUS:
                self.combo_filter.addItem(i18n.tr(core.STATUS_TEXT.get(_st, _st)), _st)
            j = self.combo_filter.findData(cur)
            self.combo_filter.setCurrentIndex(j if j >= 0 else 0)
            self.combo_filter.blockSignals(bool(old))
        for _lbl, _key in _HEADERS:
            _lbl.setText(i18n.tr(_key))
        for _p in (getattr(self, "page_edit", None), getattr(self, "page_merge", None),
                   getattr(self, "page_coll", None)):
            if _p is not None and hasattr(_p, "apply_lang"):
                _p.apply_lang()
        if hasattr(self, "stage"):
            self.stage.setText(i18n.tr("就绪"))

    def _goto(self, index):
        """切到右侧第 index 页，并同步侧栏按钮选中态。"""
        if not hasattr(self, "stack"):
            return
        index = max(0, min(int(index), self.stack.count() - 1))
        self.stack.setCurrentIndex(index)
        btn = self.mode_group.button(index)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)

    def open_collection_dialog(self):
        self._goto(3)

    def open_merge_dialog(self):
        self._goto(2)

    def open_edit_dialog(self):
        path = self.model_b.paths()[0] if self.model_b.rowCount() else ""
        self.page_edit._set_path(path)
        self._goto(1)

    def _setup_shortcuts(self):
        """快捷键绑在各自面板上（WidgetWithChildren），焦点在哪个面板就作用于哪个。"""
        for panel in (self.panel_a, self.panel_b):
            role = panel.role
            for key, seq, fn in (
                ("del_" + role, "Delete", lambda p=panel: self._remove_selected(p)),
                ("open_" + role, "Ctrl+O", lambda p=panel: self._pick_files(p)),
                ("dir_" + role, "Ctrl+Shift+O", lambda p=panel: self._pick_dir(p)),
            ):
                sc = QShortcut(QKeySequence(seq), panel)
                sc.setContext(Qt.WidgetWithChildrenShortcut)
                sc.activated.connect(fn)
                self._shortcuts[key] = sc

    # ---- 设置持久化与关窗 ----

    def _settings(self):
        return QSettings(SETTINGS_FILE, QSettings.IniFormat)

    def _save_settings(self):
        s = self._settings()
        s.setValue("geometry", self.saveGeometry())
        s.setValue("columns", [self.table.columnWidth(i) for i in range(len(COLUMNS))])
        s.setValue("filter", str(self.combo_filter.currentData() or ""))
        s.setValue("ui_version", VERSION)
        s.sync()

    def _load_settings(self):
        s = self._settings()
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        # 列宽只在同一版本内恢复: 旧版默认列宽太窄, 升级后要拿到新默认值
        cols = s.value("columns") if str(s.value("ui_version") or "") == VERSION else None
        if cols:
            for i, w in enumerate(cols):
                self.table.setColumnWidth(i, int(w))
        f = s.value("filter")
        if f is not None and str(f) != "":
            j = self.combo_filter.findData(str(f))
            if j >= 0:
                self.combo_filter.setCurrentIndex(j)

    def closeEvent(self, ev):
        self._save_settings()
        if self._running and self.runner:
            self.runner.cancel()
            deadline = time.time() + 2.0
            while time.time() < deadline and not self._done_ev.is_set():
                QApplication.processEvents()
                time.sleep(0.02)
        ev.accept()

    # ---- 表格右键菜单 ----

    def _on_row_menu(self, pos):
        row = self.table.indexAt(pos).row()
        if row >= 0:
            self._row_menu(row).exec(self.table.viewport().mapToGlobal(pos))

    def _row_menu(self, proxy_row):
        menu = QMenu(self)
        for text, fn in (
            ("复制该行", lambda: self._copy_row(proxy_row)),
            ("查看详情", lambda: self._show_detail(proxy_row)),
            ("打开所在文件夹", lambda: self._open_row_dir(proxy_row)),
        ):
            menu.addAction(text).triggered.connect(fn)
        return menu

    def _row_of(self, proxy_row):
        src = self.proxy.mapToSource(self.proxy.index(proxy_row, 0))
        if not src.isValid() or not (0 <= src.row() < len(self.task_model.rows)):
            return None
        return self.task_model.rows[src.row()]

    def _copy_row(self, proxy_row):
        row = self._row_of(proxy_row)
        if not row:
            return
        text = "\t".join([row["base"], row["check"], core.STATUS_TEXT.get(row["status"], ""), row.get("summary", "")])
        QApplication.clipboard().setText(text)
        self.statusBar().showMessage("已复制该行到剪贴板", 3000)

    def _open_row_dir(self, proxy_row):
        row = self._row_of(proxy_row)
        if not row:
            return
        path = os.path.dirname(row["check"])
        try:
            os.startfile(path)  # noqa: S606 - Windows 原生打开资源管理器
        except OSError as exc:
            QMessageBox.warning(self, "打开失败", f"无法打开文件夹：{exc}")

    def _build_sidebar(self):
        """左侧功能栏：四个入口各占一个按钮，外加导出/过滤/进度。"""
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFrameShape(QFrame.StyledPanel)
        side.setFixedWidth(176)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(10, 12, 10, 12)
        lay.setSpacing(8)

        self.lbl_app_title = QLabel(i18n.tr("EPUB 检查器"))
        title = self.lbl_app_title
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)

        self.btn_mode_compare = QPushButton(i18n.tr("比对"))
        self.btn_mode_compare.setCheckable(True)
        self.btn_mode_compare.setChecked(True)
        self.btn_mode_compare.setToolTip(i18n.tr("当前页：基准（A）与待检（B）的文件比对"))
        lay.addWidget(self.btn_mode_compare)

        self.btn_edit = QPushButton(i18n.tr("编辑工具…"))
        self.btn_edit.setToolTip("给单本 EPUB 检查/生成目录、添加封面")
        lay.addWidget(self.btn_edit)

        self.btn_merge = QPushButton(i18n.tr("合并 EPUB…"))
        self.btn_merge.setToolTip("把多本 EPUB 合成一本合集")
        lay.addWidget(self.btn_merge)

        self.btn_collection = QPushButton(i18n.tr("合集检查…"))
        self.btn_collection.setToolTip("检查合集里是否缺页/重复页")
        lay.addWidget(self.btn_collection)

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for idx, b in enumerate((self.btn_mode_compare, self.btn_edit,
                                  self.btn_merge, self.btn_collection)):
            b.setCheckable(True)
            self.mode_group.addButton(b, idx)
        self.mode_group.idClicked.connect(self._goto)

        line1 = QFrame()
        line1.setFrameShape(QFrame.HLine)
        lay.addWidget(line1)

        self.progress = QProgressBar()
        self.progress.setFormat("%v/%m")
        lay.addWidget(self.progress)

        self.stage = QLabel(i18n.tr("就绪"))
        self.stage.setWordWrap(True)
        lay.addWidget(self.stage)

        self.lbl_lang = QLabel(i18n.tr("语言"))
        lay.addWidget(self.lbl_lang)
        self.combo_lang = QComboBox()
        self.combo_lang.addItem(i18n.tr("中文"), "zh")
        self.combo_lang.addItem("English", "en")
        self.combo_lang.addItem(i18n.tr("跟随系统"), "auto")
        self.combo_lang.currentIndexChanged.connect(self._on_lang_changed)
        lay.addWidget(self.combo_lang)
        lay.addStretch(1)
        return side

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.sidebar = self._build_sidebar()
        outer.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        self.page_compare = self._build_compare_page()
        self.stack.addWidget(self.page_compare)

        # -- 其余三页（原弹窗，现改成可切换的页） --
        self.page_edit = EditDialog("", self)
        self.page_merge = MergeDialog(self)
        self.page_coll = CollectionDialog(self)
        for page, title, desc in (
            (self.page_edit, "编辑工具", "只针对 EPUB：检查/生成目录、添加封面。把书拖进来即可。"),
            (self.page_merge, "合并 EPUB", "按列表顺序把多本 EPUB 合成一本，另存新文件。"),
            (self.page_coll, "合集检查", "检查合集里是否缺页、重复页、降质页。第一个文件当合集。"),
        ):
            lay = page.layout()
            if lay is not None:
                lay.insertWidget(0, page_header(title, desc))
            page.setAcceptDrops(True)
            self.stack.addWidget(page)

        self.statusBar().showMessage("就绪")
        self._update_start_state()
        self._goto(0)

    def _build_compare_page(self):
        compare = QWidget()
        compare.setAcceptDrops(True)
        root = QVBoxLayout(compare)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(page_header(
            "比对", "左：基准（A），右：待检（B）。点「开始比对」按 A×B 逐个比对并列出结论。"))

        self.panel_a = BookPanel("基准（A）", "a")
        self.panel_b = BookPanel("待检（B）", "b")
        self.list_a = self.panel_a.view
        self.list_b = self.panel_b.view
        self.panel_a.bind_model(self.model_a)
        self.panel_b.bind_model(self.model_b)
        for panel, model in ((self.panel_a, self.model_a), (self.panel_b, self.model_b)):
            panel.add_paths.connect(lambda ps, m=model: self._add_paths(m, ps))
            panel.remove_selected.connect(lambda p=panel: self._remove_selected(p))
            panel.clear_all.connect(lambda p=panel: self._clear_panel(p))
            panel.pick_files.connect(lambda p=panel: self._pick_files(p))
            panel.pick_dir.connect(lambda p=panel: self._pick_dir(p))

        top = QHBoxLayout()
        top.addWidget(self.panel_a, 1)
        top.addWidget(self.panel_b, 1)
        root.addLayout(top)

        bar = QHBoxLayout()
        self.btn_start = QPushButton(i18n.tr("开始比对"))
        self.btn_cancel = QPushButton(i18n.tr("取消"))
        self.btn_cancel.setEnabled(False)
        bar.addWidget(self.btn_start)
        bar.addWidget(self.btn_cancel)
        bar.addStretch(1)
        root.addLayout(bar)

        # 结论过滤 + 导出：属于比对结果，放在比对页里（不再占侧栏）
        frow = QHBoxLayout()
        self.lbl_filter = QLabel(i18n.tr("只看"))
        frow.addWidget(self.lbl_filter)
        self.combo_filter = QComboBox()
        self.combo_filter.addItem(i18n.tr("全部"), "")
        for _st in core.ALL_STATUS:
            self.combo_filter.addItem(i18n.tr(core.STATUS_TEXT.get(_st, _st)), _st)
        self.combo_filter.setMinimumWidth(150)
        frow.addWidget(self.combo_filter)
        frow.addStretch(1)
        self.btn_export = QPushButton(i18n.tr("导出 CSV"))
        frow.addWidget(self.btn_export)
        root.addLayout(frow)

        self._build_tabs(root)
        return compare

    def _build_tabs(self, root):
        self.tabs = QTabWidget()
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.AscendingOrder)  # 默认按序号升序，任务顺序与建行顺序一致
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        # 摘要/备注列要看全，默认给足宽度（用户可在界面上拖，也会被 ini 记住）
        for col, w in ((0, 60), (1, 200), (2, 200), (3, 80), (4, 150), (5, 340), (6, 200)):
            self.table.setColumnWidth(col, w)
        self.tabs.addTab(self.table, i18n.tr("任务表"))

        detail = QWidget()
        dlay = QVBoxLayout(detail)
        self.detail_bar = QLabel(i18n.tr("双击任务表某一行查看该文件的详细报告"))
        self.detail_text = QPlainTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setMaximumBlockCount(20000)
        dlay.addWidget(self.detail_bar)
        dlay.addWidget(self.detail_text, 1)
        self.tabs.addTab(detail, i18n.tr("详细报告"))
        root.addWidget(self.tabs, 1)

        self.combo_filter.currentTextChanged.connect(self._apply_filter)
        self.btn_export.clicked.connect(self._export_clicked)
        self.table.doubleClicked.connect(lambda i: self._show_detail(i.row()))

    # -- 状态联动 --
    def _update_start_state(self):
        ok = self.model_a.rowCount() > 0 and self.model_b.rowCount() > 0
        self.btn_start.setEnabled(ok)

    # -- 文件增删 --
    _EXTS = set(core.FMT_BY_EXT.keys())

    def _panel_of(self, model):
        return self.panel_a if model is self.model_a else self.panel_b

    def _add_paths(self, model, paths):
        if paths:
            remember_dir(paths[0])
        """文件夹展开 + 校验 + 去重 + 写入模型。返回 (added, dup, rejected)。"""
        added, dup, rejected = 0, 0, []
        pending = list(paths)
        have = {os.path.normcase(p) for p in model.paths()}
        new = []
        while pending:  # 目录展开项回填队列，避免边迭代边追加
            p = os.path.abspath(pending.pop(0))
            if os.path.isdir(p):
                for base, _dirs, files in os.walk(p):
                    for fn in sorted(files):
                        if os.path.splitext(fn)[1].lower() in self._EXTS:
                            pending.append(os.path.join(base, fn))
                continue
            if not os.path.exists(p):
                rejected.append(f"{os.path.basename(p)}：路径不存在")
                continue
            ext = os.path.splitext(p)[1].lower()
            if ext not in self._EXTS:
                rejected.append(f"{os.path.basename(p)}：不支持的格式 {ext}")
                continue
            try:
                if os.path.getsize(p) == 0:
                    rejected.append(f"{os.path.basename(p)}：0 字节文件")
                    continue
            except OSError as e:
                rejected.append(f"{os.path.basename(p)}：{e}")
                continue
            key = os.path.normcase(p)
            if key in have:
                dup += 1
                continue
            have.add(key)
            new.append(p)
        if new:
            model.add_paths(new)
            added = len(new)
        self._update_start_state()
        self._update_count(self._panel_of(model))
        if added or dup or rejected:
            msg = f"已添加 {added} 个文件"
            if dup:
                msg += f"，忽略 {dup} 个重复文件"
            if rejected:
                msg += f"，拒绝 {len(rejected)} 个（{rejected[0]}）"
            self.statusBar().showMessage(msg, 5000)
        return added, dup, rejected

    def _update_count(self, panel):
        model = panel.view.model()
        panel.lbl_count.setText(str(model.rowCount() if model else 0))

    def _remove_selected(self, panel):
        model = panel.view.model()
        if model is None:
            return
        rows = [i.row() for i in panel.view.selectionModel().selectedIndexes()]
        model.remove_rows(rows)
        self._update_count(panel)
        self._update_start_state()

    def _clear_panel(self, panel):
        model = panel.view.model()
        if model is not None:
            model.clear()
        self._update_count(panel)
        self._update_start_state()

    def _pick_files(self, panel):
        exts = " ".join("*" + e for e in sorted(self._EXTS))
        files, _ = QFileDialog.getOpenFileNames(self, "选择电子书", start_dir(), f"电子书 ({exts})")
        if files:
            self._add_paths(panel.view.model(), files)

    def _pick_dir(self, panel):
        d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if d:
            self._add_paths(panel.view.model(), [d])

    # -- 任务调度 --
    def start_compare(self):
        if self._running:
            return
        a, b = self.model_a.paths(), self.model_b.paths()
        if not a or not b:
            return
        total = len(a) * len(b)
        if not self._confirm_big(total):
            return
        self.task_model.clear()
        self._cancel_requested = False
        tasks = []
        idx = 0
        for base in a:
            for chk in b:
                idx += 1
                tasks.append((idx, base, chk, os.path.normcase(base) == os.path.normcase(chk)))
                self.task_model.add_row({
                    "idx": idx, "base": base, "check": chk,
                    "fmt_rel": "same" if core.book_format(base) == core.book_format(chk) else "cross",
                    "status": core.STATUS_WAIT, "summary": "", "reason": "",
                })
        self.progress.setMaximum(total)
        self.progress.setValue(0)
        self._done_count = 0
        self._done_ev.clear()
        self._running = True
        self._lock_ui(True)
        self.stage.setText(f"共 {total} 个任务，开始执行")
        self.runner = core.TaskRunner(tasks, emit=self._emit_event)
        self.runner.start()

    def _confirm_big(self, total):
        if total > 5000:
            QMessageBox.warning(self, "任务过多", f"共 {total} 个任务，超过 5000 上限，请减少文件数。")
            return False
        if total > 1000:
            r = QMessageBox.question(
                self, "任务较多",
                f"将执行 {total} 个比对任务（每本约 0.5-2 秒，共约 "
                f"{total * 0.5 / 60:.0f}-{total * 2 / 60:.0f} 分钟），确定继续？")
            return r == QMessageBox.Yes
        return True

    def _emit_event(self, ev):
        """核心层发的是元组事件，这里翻译成字典并转发到主线程。"""
        kind = ev[0]
        if kind == "stage":
            self.bridge.stage_changed.emit({"kind": "stage", "idx": ev[1], "text": ev[2]})
        elif kind == "row":
            _, idx, status, summary, reason, elapsed, done, total = ev
            self.bridge.row_ready.emit({
                "kind": "row", "idx": idx, "status": status, "summary": summary,
                "reason": reason, "elapsed": elapsed, "done": done, "total": total,
            })
            if done >= total:
                self.bridge.finished.emit({"kind": "done", "total": total})

    def _on_row(self, ev):
        pos = ev["idx"] - 1
        if 0 <= pos < self.task_model.rowCount():
            self.task_model.update_row(
                pos, status=ev["status"], summary=ev.get("summary", ""),
                reason=ev.get("reason", ""))
        self._done_count = ev.get("done", self._done_count + 1)
        self.progress.setValue(self._done_count)

    def _on_stage(self, ev):
        if self._running:
            self.stage.setText(ev.get("text", ""))

    # -- 过滤 / 导出 / 详情 --
    def _apply_filter(self, status=None):
        # 下拉项的显示文字随语言变化，真正参与过滤的是 itemData 里的状态名；
        # 传参时兼容「原始状态名」与「全部」两种旧写法。
        if status in ("全部", "All"):
            status = ""
        if not isinstance(status, str) or status not in [""] + list(core.ALL_STATUS):
            status = str(self.combo_filter.currentData() or "")
        text = status
        self.proxy.set_status(text)
        self.statusBar().showMessage(
            f"显示 {self.proxy.rowCount()} / 共 {self.task_model.rowCount()} 行", 4000)

    def _export_clicked(self):
        p, _ = QFileDialog.getSaveFileName(self, "导出 CSV", "epub_check.csv", "CSV (*.csv)")
        if p:
            try:
                self._export_csv(p)
            except OSError as e:
                QMessageBox.warning(self, "导出失败", str(e))
            else:
                self.statusBar().showMessage(f"已导出：{p}", 5000)

    def _export_csv(self, path):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["序号", "基准文件", "待检文件", "格式关系", "结论", "差异摘要", "备注"])
            for r in self.task_model.rows:
                w.writerow([r["idx"], r["base"], r["check"],
                            "同格式" if r.get("fmt_rel") == "same" else "跨格式",
                            core.STATUS_TEXT.get(r["status"], r["status"]),
                            r.get("summary", ""), r.get("reason", "")])

    def _show_detail(self, proxy_row):
        src = self.proxy.mapToSource(self.proxy.index(proxy_row, 0))
        if not src.isValid() or src.row() >= self.task_model.rowCount():
            return
        row = self.task_model.rows[src.row()]
        path = row["check"]
        self.tabs.setCurrentIndex(1)
        self.detail_bar.setText(f"正在解析：{path}")
        self.detail_text.setPlainText("")

        def work():
            try:
                rep, _probs = core.check_single(path)
            except Exception as e:  # 单文件解析失败不该炸界面
                rep = f"解析失败：{type(e).__name__}: {e}"
            base = row["base"]
            if os.path.normcase(base) != os.path.normcase(path):
                try:
                    r = core.compare_books(base, path)
                    detail = r.get("detail") or []
                    if detail:
                        rep += "\n\n" + ("--- Differences against「%s」---" if i18n.current() == "en" else "—— 与「%s」的逐条差异 ——") % os.path.basename(base) + "\n"
                        rep += ("Conclusion: %s  %s\n\n" if i18n.current() == "en" else "结论：%s  %s\n\n") % (
                            i18n.tr(core.STATUS_TEXT.get(r["status"], r["status"])),
                            r.get("summary", ""))
                        rep += "\n".join(detail)
                except Exception as e:
                    rep += "\n\n逐条差异生成失败：%s: %s" % (type(e).__name__, e)
            self.bridge.detail_ready.emit(
                {"path": path, "base": base, "report": rep})

        run_bg(work)

    def _on_detail(self, ev):
        # 报告主体是【待检 B】, 基准 A 只在文末做对照
        fmt = ("Details  Checked (B): %s  |  Baseline (A): %s"
               if i18n.current() == "en" else
               "详细报告　待检（B）：%s　｜　基准（A）：%s")
        self.detail_bar.setText(
            fmt % (os.path.basename(ev["path"]), os.path.basename(ev.get("base") or "")))
        self.detail_text.setPlainText(i18n.tr_report(ev["report"]))
        # 有逐条差异时直接滚到那一段, 免得用户自己往下翻
        cur = self.detail_text.textCursor()
        pos = ev["report"].find("—— 与「")
        if pos >= 0:
            cur.setPosition(pos)
            self.detail_text.setTextCursor(cur)
            self.detail_text.centerCursor()  # 让差异正文落在视野中间

    def _on_done(self, ev):
        # 跑完直接把第一条的待检报告显示出来, 免得用户以为没有结果
        if self.task_model.rowCount() and not self.detail_text.toPlainText().strip():
            self._show_detail(0)
        self._running = False
        self._done_ev.set()
        self._lock_ui(False)
        self.stage.setText("已取消" if self._cancel_requested else "完成")
        self._update_start_state()
        self.statusBar().showMessage(
            f"比对完成：{ev.get('total', self.task_model.rowCount())} 个任务", 8000)

    def cancel_compare(self):
        # 先记下取消意图：点得很快时 self.runner 可能还没赋值，不能因此吞掉这次取消
        self._cancel_requested = True
        if self.runner:
            self.runner.cancel()
        self.btn_cancel.setEnabled(False)
        self.stage.setText("正在取消…")

    def _lock_ui(self, busy):
        for p in (self.panel_a, self.panel_b):
            p.setEnabled(not busy)
        self.btn_collection.setEnabled(not busy)
        self.btn_edit.setEnabled(not busy)
        self.btn_merge.setEnabled(not busy)
        self.btn_export.setEnabled(not busy)
        self.combo_filter.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)
        if busy:
            self.btn_start.setEnabled(False)
        else:
            self._update_start_state()


def app_icon_path():
    """exe 自我定位 app.ico（frozen 时在解包目录，源码时在脚本目录）。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, "app.ico")
    return p if os.path.exists(p) else ""


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("EPUB 检查器")
    ico = app_icon_path()
    if ico:
        app.setWindowIcon(QIcon(ico))
    app.setOrganizationName("epub_checker")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
