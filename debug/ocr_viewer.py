#!/usr/bin/env python3
"""Debug OCR viewer - browse submissions, pages, and line detections."""

import csv
import json
import os
import sys
import tempfile
from pathlib import Path

import threading

from PySide6.QtCore import Qt, QPointF, QRectF, QTimer, QEvent, QSettings
from PySide6.QtGui import QImage, QColor, QPen, QPainter, QPalette, QPolygonF, QMouseEvent, QIcon, QPainterPath
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeWidget, QTreeWidgetItem, QTableWidget, QTableWidgetItem,
    QLabel, QScrollArea, QHeaderView, QSlider, QPushButton, QFrame,
    QDoubleSpinBox, QMessageBox,
)

from transcriptor_worker.extraction.lines import init_surya_model, extract_lines


class ImageViewer(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(False)
        self.setBackgroundRole(QPalette.Dark)
        self.setMouseTracking(True)

        self._image = None
        self._lines = []
        self._selected_index = -1
        self._zoom = 1.0
        self._fit_scale = 1.0
        self._show_boxes = False

        self._panning = False
        self._pan_start = None
        self._pan_scroll_start = None

        self._canvas = QWidget()
        self._canvas.setMinimumSize(800, 600)
        self._canvas.paintEvent = self._paint_event
        self._canvas.setMouseTracking(True)
        self._canvas.mousePressEvent = self._on_mouse_press
        self._canvas.mouseMoveEvent = self._on_mouse_move
        self._canvas.mouseReleaseEvent = self._on_mouse_release
        self.setWidget(self._canvas)

        self._on_zoom_change = None

    def set_display_mode(self, show_boxes: bool):
        self._show_boxes = show_boxes
        self._canvas.update()

    def set_image_and_lines(self, image_path: Path, lines: list[dict]):
        if isinstance(image_path, QImage):
            self._image = image_path
        else:
            self._image = QImage(str(image_path))
        self._lines = lines
        self._selected_index = -1
        self._zoom = 1.0
        self._apply_zoom()

    def set_zoom(self, zoom: float):
        self._zoom = max(0.1, min(zoom, 10.0))
        self._apply_zoom()
        if self._on_zoom_change:
            self._on_zoom_change(self._zoom)

    def fit_to_view(self):
        if self._image is None:
            return
        viewport = self.viewport().size()
        img_w = self._image.width()
        img_h = self._image.height()
        scale_w = viewport.width() / img_w
        scale_h = viewport.height() / img_h
        self._zoom = min(scale_w, scale_h)
        self._apply_zoom()
        self.verticalScrollBar().setValue(0)
        self.horizontalScrollBar().setValue(0)

    def fit_width(self):
        if self._image is None:
            return
        viewport = self.viewport().size()
        img_w = self._image.width()
        self._zoom = viewport.width() / img_w
        self._apply_zoom()
        self.verticalScrollBar().setValue(0)
        self.horizontalScrollBar().setValue(0)

    def zoom_in(self):
        self.set_zoom(self._zoom * 1.25)

    def zoom_out(self):
        self.set_zoom(self._zoom / 1.25)

    def zoom_reset(self):
        self.fit_to_view()

    def select_line(self, index: int):
        self._selected_index = index
        self._canvas.update()

    def _apply_zoom(self):
        if self._image is None:
            return
        w = int(self._image.width() * self._zoom)
        h = int(self._image.height() * self._zoom)
        self._canvas.setMinimumSize(w, h)
        self._canvas.setMaximumSize(w, h)
        self._canvas.update()

    def _get_shape_points(self, line: dict) -> list[tuple[float, float]]:
        if self._show_boxes:
            bbox = line.get("bbox", [])
            if len(bbox) != 4:
                return []
            x1, y1, x2, y2 = bbox
            return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        poly = line.get("polygon", [])
        if len(poly) == 4:
            return [(p[0], p[1]) for p in poly]
        return []

    def _draw_shape(self, painter: QPainter, pts: list[tuple[float, float]]):
        if self._show_boxes:
            x = pts[0][0] * self._zoom
            y = pts[0][1] * self._zoom
            w = (pts[2][0] - pts[0][0]) * self._zoom
            h = (pts[2][1] - pts[0][1]) * self._zoom
            painter.drawRect(QRectF(x, y, w, h))
        else:
            qpts = [QPointF(p[0] * self._zoom, p[1] * self._zoom) for p in pts]
            painter.drawPolygon(QPolygonF(qpts))

    def _build_selection_path(self, pts: list[tuple[float, float]]) -> QPainterPath:
        path = QPainterPath()
        if self._show_boxes:
            x = pts[0][0] * self._zoom
            y = pts[0][1] * self._zoom
            w = (pts[2][0] - pts[0][0]) * self._zoom
            h = (pts[2][1] - pts[0][1]) * self._zoom
            path.addRect(QRectF(x, y, w, h))
        else:
            qpts = [QPointF(p[0] * self._zoom, p[1] * self._zoom) for p in pts]
            path.addPolygon(QPolygonF(qpts))
        return path

    def _paint_event(self, event):
        from PySide6.QtWidgets import QStyleOption, QStyle

        # fill background with the widget's default style
        painter = QPainter(self._canvas)
        opt = QStyleOption()
        opt.initFrom(self._canvas)
        self._canvas.style().drawPrimitive(QStyle.PE_Widget, opt, painter, self._canvas)

        if self._image is None:
            return

        # scale image to current zoom level
        scaled = self._image.scaled(
            int(self._image.width() * self._zoom),
            int(self._image.height() * self._zoom),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )

        # ── No line selected: draw everything normally ──
        if self._selected_index < 0:
            painter.drawImage(0, 0, scaled)
            painter.setOpacity(0.5)
            pen_all = QPen(QColor(0, 255, 136), 2)
            painter.setPen(pen_all)
            for line in self._lines:
                pts = self._get_shape_points(line)
                if pts:
                    self._draw_shape(painter, pts)
            return

        # ── A specific line is selected: dim everything outside it ──
        sel_pts = self._get_shape_points(self._lines[self._selected_index])
        if not sel_pts:
            return

        # build a clip path: everything outside the selected shape
        sel_path = self._build_selection_path(sel_pts)

        full_path = QPainterPath()
        full_path.addRect(0, 0, scaled.width(), scaled.height())

        outside_path = full_path - sel_path

        # draw the full image first
        painter.drawImage(0, 0, scaled)

        # clear the outside region to white, then redraw it at 50 % opacity
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        painter.setClipPath(outside_path)
        painter.fillRect(0, 0, scaled.width(), scaled.height(), Qt.white)

        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.setClipPath(outside_path)
        painter.setOpacity(0.5)
        painter.drawImage(0, 0, scaled)

        # restore normal blending for overlay drawing
        painter.setOpacity(1.0)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

        # draw non-selected line shapes (green) – both outside and inside the selection
        painter.setClipPath(outside_path)
        painter.setOpacity(0.3)
        pen_all = QPen(QColor(0, 255, 136), 2)
        painter.setPen(pen_all)
        for i, line in enumerate(self._lines):
            if i == self._selected_index:
                continue
            pts = self._get_shape_points(line)
            if pts:
                self._draw_shape(painter, pts)

        painter.setClipPath(sel_path)
        painter.setOpacity(0.3)
        pen_all_full = QPen(QColor(0, 255, 136), 2)
        painter.setPen(pen_all_full)
        for i, line in enumerate(self._lines):
            if i == self._selected_index:
                continue
            pts = self._get_shape_points(line)
            if pts:
                self._draw_shape(painter, pts)

        # draw the selected line shape (red) – both outside and inside
        painter.setClipPath(outside_path)
        painter.setOpacity(0.2)
        painter.setPen(QPen(QColor(255, 68, 68), 3))
        self._draw_shape(painter, sel_pts)

        painter.setClipPath(sel_path)
        painter.setOpacity(0.2)
        painter.setPen(QPen(QColor(255, 68, 68), 3))
        self._draw_shape(painter, sel_pts)

    def _on_mouse_press(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            click_pos = event.pos()
            hit_index = self._hit_test_lines(click_pos)
            if hit_index == self._selected_index:
                self._selected_index = -1
                self._canvas.update()
                return
            if hit_index >= 0:
                self._selected_index = hit_index
                self._canvas.update()
                return

            self._panning = True
            self._pan_start = click_pos
            self._pan_scroll_start = (
                self.horizontalScrollBar().value(),
                self.verticalScrollBar().value(),
            )
            self._canvas.setCursor(Qt.ClosedHandCursor)
            event.accept()

    def _hit_test_lines(self, pos: QPointF) -> int:
        for i, line in enumerate(self._lines):
            pts = self._get_shape_points(line)
            if not pts:
                continue
            if self._show_boxes:
                x1, y1, x2, y2 = pts[0][0], pts[0][1], pts[2][0], pts[2][1]
                if x1 * self._zoom <= pos.x() <= x2 * self._zoom and y1 * self._zoom <= pos.y() <= y2 * self._zoom:
                    return i
            else:
                scaled_pts = [(p[0] * self._zoom, p[1] * self._zoom) for p in pts]
                if self._point_in_polygon(pos.x(), pos.y(), scaled_pts):
                    return i
        return -1

    def _point_in_polygon(self, x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
        n = len(polygon)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = polygon[i]
            xj, yj = polygon[j]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    def _on_mouse_move(self, event: QMouseEvent):
        if self._panning and self._pan_start and self._pan_scroll_start:
            dx = event.pos().x() - self._pan_start.x()
            dy = event.pos().y() - self._pan_start.y()
            self.horizontalScrollBar().setValue(self._pan_scroll_start[0] - dx)
            self.verticalScrollBar().setValue(self._pan_scroll_start[1] - dy)
            event.accept()

    def _on_mouse_release(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._panning = False
            self._pan_start = None
            self._pan_scroll_start = None
            self._canvas.setCursor(Qt.ArrowCursor)
            event.accept()


class OCRViewer(QMainWindow):
    def __init__(self, output_path: str):
        super().__init__()
        self.output_dir = Path(output_path)
        self.submissions: dict[str, dict] = {}
        self.pages: list[dict] = []
        self.base_lines: list[dict] = []
        self.test_lines: list[dict] = []
        self._use_test = False

        self._surya_model = None
        self._model_lock = threading.Lock()
        self._loading_model = False

        self._original_image = None

        self.setWindowTitle(f"OCR Debug Viewer - {output_path}")

        self._load_data()
        self._build_ui()
        self._restore_settings()
        self._select_first_page()

    def _load_data(self):
        submissions_csv = self.output_dir / "submissions.csv"
        pages_csv = self.output_dir / "pages.csv"

        if submissions_csv.exists():
            with open(submissions_csv, newline="") as f:
                for row in csv.DictReader(f):
                    sid = row["submission_id"]
                    meta_file = self.output_dir / sid / "metadata.json"
                    meta = {}
                    if meta_file.exists():
                        with open(meta_file) as mf:
                            meta = json.load(mf)
                    self.submissions[sid] = {
                        "status": row.get("status", ""),
                        "metadata": meta,
                    }

        if pages_csv.exists():
            with open(pages_csv, newline="") as f:
                for row in csv.DictReader(f):
                    self.pages.append(row)

    def _load_thresh_defaults(self) -> tuple[float, float]:
        text_thresh = 0.6
        blank_thresh = 0.35
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip()
                    if key == "DETECTOR_TEXT_THRESHOLD":
                        try:
                            text_thresh = float(val)
                        except ValueError:
                            pass
                    elif key == "DETECTOR_BLANK_THRESHOLD":
                        try:
                            blank_thresh = float(val)
                        except ValueError:
                            pass
        return text_thresh, blank_thresh

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)

        self.image_viewer = ImageViewer()
        self.image_viewer._on_zoom_change = self._update_zoom_ui_from_viewer

        self._splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(self._splitter)

        left_splitter = QSplitter(Qt.Vertical)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemSelectionChanged.connect(self._on_tree_select)
        left_splitter.addWidget(self.tree)

        self.lines_table = QTableWidget()
        self.lines_table.setColumnCount(3)
        self.lines_table.setHorizontalHeaderLabels(["#", "Confidence", "Bounding Box"])
        self.lines_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.lines_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.lines_table.setSelectionMode(QTableWidget.SingleSelection)
        self.lines_table.itemSelectionChanged.connect(self._on_line_select)
        left_splitter.addWidget(self.lines_table)

        left_splitter.setStretchFactor(0, 2)
        left_splitter.setStretchFactor(1, 1)

        self._splitter.addWidget(left_splitter)

        right_frame = QWidget()
        right_layout = QVBoxLayout(right_frame)
        right_layout.setContentsMargins(0, 0, 0, 0)

        zoom_bar = QFrame()
        zoom_bar.setFrameShape(QFrame.StyledPanel)
        zoom_layout = QHBoxLayout(zoom_bar)
        zoom_layout.setContentsMargins(4, 2, 4, 2)

        btn_zoom_out = QPushButton("-")
        btn_zoom_out.setFixedWidth(30)
        btn_zoom_out.clicked.connect(self.image_viewer.zoom_out)
        zoom_layout.addWidget(btn_zoom_out)

        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(10, 1000)
        self.zoom_slider.setValue(100)
        self.zoom_slider.setFixedWidth(200)
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider)
        zoom_layout.addWidget(self.zoom_slider)

        btn_zoom_in = QPushButton("+")
        btn_zoom_in.setFixedWidth(30)
        btn_zoom_in.clicked.connect(self.image_viewer.zoom_in)
        zoom_layout.addWidget(btn_zoom_in)

        btn_fit = QPushButton("Fit")
        btn_fit.setFixedWidth(40)
        btn_fit.clicked.connect(self.image_viewer.fit_to_view)
        zoom_layout.addWidget(btn_fit)

        btn_fit_width = QPushButton("Fit W")
        btn_fit_width.setFixedWidth(40)
        btn_fit_width.clicked.connect(self.image_viewer.fit_width)
        zoom_layout.addWidget(btn_fit_width)

        self.zoom_label = QLabel("100%")
        self.zoom_label.setFixedWidth(50)
        self.zoom_label.setAlignment(Qt.AlignCenter)
        zoom_layout.addWidget(self.zoom_label)

        zoom_layout.addSpacing(10)

        self.btn_mode = QPushButton("Polygons")
        self.btn_mode.setFixedWidth(80)
        self.btn_mode.setCheckable(True)
        self.btn_mode.clicked.connect(self._toggle_display_mode)
        zoom_layout.addWidget(self.btn_mode)

        zoom_layout.addStretch()

        right_layout.addWidget(zoom_bar)

        default_text_thresh, default_blank_thresh = self._load_thresh_defaults()

        test_panel = QFrame()
        test_panel.setFrameShape(QFrame.StyledPanel)
        test_layout = QHBoxLayout(test_panel)
        test_layout.setContentsMargins(4, 2, 4, 2)

        lbl_thresh = QLabel("Text Thresh:")
        lbl_thresh.setFixedWidth(70)
        test_layout.addWidget(lbl_thresh)
        self.spin_text_thresh = QDoubleSpinBox()
        self.spin_text_thresh.setRange(0.01, 1.0)
        self.spin_text_thresh.setSingleStep(0.01)
        self.spin_text_thresh.setValue(default_text_thresh)
        self.spin_text_thresh.setFixedWidth(70)
        test_layout.addWidget(self.spin_text_thresh)

        lbl_blank = QLabel("Blank Thresh:")
        lbl_blank.setFixedWidth(75)
        test_layout.addWidget(lbl_blank)
        self.spin_blank_thresh = QDoubleSpinBox()
        self.spin_blank_thresh.setRange(0.01, 1.0)
        self.spin_blank_thresh.setSingleStep(0.01)
        self.spin_blank_thresh.setValue(default_blank_thresh)
        self.spin_blank_thresh.setFixedWidth(70)
        test_layout.addWidget(self.spin_blank_thresh)

        lbl_target_width = QLabel("Target Width:")
        lbl_target_width.setFixedWidth(75)
        test_layout.addWidget(lbl_target_width)
        self.spin_target_width = QDoubleSpinBox()
        self.spin_target_width.setDecimals(0)
        self.spin_target_width.setRange(100, 20000)
        self.spin_target_width.setSingleStep(100)
        self.spin_target_width.setValue(3000)
        self.spin_target_width.setFixedWidth(80)
        test_layout.addWidget(self.spin_target_width)

        test_layout.addSpacing(10)

        lbl_joining = QLabel("Joining:")
        lbl_joining.setFixedWidth(50)
        test_layout.addWidget(lbl_joining)
        self.btn_joining_minus = QPushButton("-")
        self.btn_joining_minus.setFixedWidth(25)
        self.btn_joining_minus.clicked.connect(self._less_joining)
        test_layout.addWidget(self.btn_joining_minus)
        self.btn_joining_plus = QPushButton("+")
        self.btn_joining_plus.setFixedWidth(25)
        self.btn_joining_plus.clicked.connect(self._add_joining)
        test_layout.addWidget(self.btn_joining_plus)

        lbl_boxes = QLabel("Small Boxes:")
        lbl_boxes.setFixedWidth(75)
        test_layout.addWidget(lbl_boxes)
        self.btn_boxes_minus = QPushButton("-")
        self.btn_boxes_minus.setFixedWidth(25)
        self.btn_boxes_minus.clicked.connect(self._less_small_boxes)
        test_layout.addWidget(self.btn_boxes_minus)
        self.btn_boxes_plus = QPushButton("+")
        self.btn_boxes_plus.setFixedWidth(25)
        self.btn_boxes_plus.clicked.connect(self._add_small_boxes)
        test_layout.addWidget(self.btn_boxes_plus)

        test_layout.addSpacing(10)

        self.btn_run_detection = QPushButton("Run Detection")
        self.btn_run_detection.setFixedWidth(100)
        self.btn_run_detection.clicked.connect(self._run_surya_detection)
        test_layout.addWidget(self.btn_run_detection)

        self.btn_reset_settings = QPushButton("Reset")
        self.btn_reset_settings.setFixedWidth(60)
        self.btn_reset_settings.clicked.connect(self._reset_settings)
        test_layout.addWidget(self.btn_reset_settings)

        test_layout.addSpacing(10)

        self.btn_toggle_base = QPushButton("Show Base")
        self.btn_toggle_base.setFixedWidth(90)
        self.btn_toggle_base.clicked.connect(self._toggle_base_test)
        test_layout.addWidget(self.btn_toggle_base)

        self.lbl_mode = QLabel("Mode: Base")
        self.lbl_mode.setFixedWidth(80)
        test_layout.addWidget(self.lbl_mode)

        self.help_tooltip = (
            "<b>How it works</b><br>"
            "Surya detects text boxes and groups them into lines.<br>"
            "Both values range from 0 to 1.<br><br>"
            "<b>Text Thresh</b>: Minimum confidence to be recognized as text.<br>"
            "<b>Blank Thresh</b>: Maximum confidence to be considered blank space.<br><br>"
            "<b>Rule</b>: Text Thresh should always be higher than Blank Thresh.<br><br>"
            "<b>Debug tip</b>: Run Surya in debug mode to see a heatmap of detections.<br>"
            "Boxes merging? → Raise thresholds.<br>"
            "Missing faint boxes? → Lower thresholds."
        )

        self.btn_help_thresh = QPushButton("ⓘ")
        self.btn_help_thresh.setFixedWidth(25)
        self.btn_help_thresh.setToolTip(self.help_tooltip)
        self.btn_help_thresh.setCursor(Qt.PointingHandCursor)
        self.btn_help_thresh.setStyleSheet("border: none; background: transparent; color: #aaa; font-size: 13px; padding: 0;")
        self.btn_help_thresh.setMouseTracking(True)
        test_layout.addWidget(self.btn_help_thresh)

        test_layout.addStretch()

        right_layout.addWidget(test_panel)
        right_layout.addWidget(self.image_viewer)

        self._splitter.addWidget(right_frame)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 3)

        self._populate_tree()

    def _toggle_display_mode(self):
        show_boxes = self.btn_mode.isChecked()
        self.btn_mode.setText("Boxes" if show_boxes else "Polygons")
        self.image_viewer.set_display_mode(show_boxes)

    def _on_zoom_slider(self, value):
        zoom = value / 100.0
        self.image_viewer.set_zoom(zoom)
        self.zoom_label.setText(f"{value}%")

    def _update_zoom_ui_from_viewer(self, zoom: float):
        self.zoom_slider.blockSignals(True)
        pct = int(zoom * 100)
        self.zoom_slider.setValue(pct)
        self.zoom_label.setText(f"{pct}%")
        self.zoom_slider.blockSignals(False)

    def _populate_tree(self):
        for sid, sdata in sorted(self.submissions.items()):
            short = sid[:8] + "..."
            snode = QTreeWidgetItem(self.tree, [f"[{sdata['status']}] {short}"])
            snode.setData(0, Qt.UserRole, {"type": "submission", "id": sid})

            doc_pages: dict[str, list[dict]] = {}
            for p in self.pages:
                if p["submission_id"] == sid:
                    doc = p["doc_filename"]
                    doc_pages.setdefault(doc, []).append(p)

            for doc, doc_page_list in sorted(doc_pages.items()):
                dnode = QTreeWidgetItem(snode, [f"doc: {doc}"])
                dnode.setData(0, Qt.UserRole, {"type": "doc"})
                for p in sorted(doc_page_list, key=lambda x: int(x["page_number"])):
                    pnode = QTreeWidgetItem(dnode, [f"page {p['page_number']} ({p['image_filename']})"])
                    pnode.setData(0, Qt.UserRole, {
                        "type": "page",
                        "submission_id": sid,
                        "image_filename": p["image_filename"],
                        "lines_filename": p["lines_filename"],
                    })

        self.tree.expandAll()

    def _select_first_page(self):
        item = self.tree.topLevelItem(0)
        if item is None:
            return
        doc_item = item.child(0)
        if doc_item is None:
            return
        page_item = doc_item.child(0)
        if page_item is None:
            return
        self.tree.setCurrentItem(page_item)
        self._on_tree_select()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.image_viewer.fit_to_view()

    def _on_tree_select(self):
        sel = self.tree.selectedItems()
        if not sel:
            return
        data = sel[0].data(0, Qt.UserRole)
        if data.get("type") == "page":
            self._load_page(data["submission_id"], data["image_filename"], data["lines_filename"])

    def _load_page(self, submission_id: str, image_filename: str, lines_filename: str):
        img_path = self.output_dir / submission_id / image_filename
        lines_path = self.output_dir / submission_id / lines_filename

        if not img_path.exists():
            return

        self._current_img_path = img_path
        self._current_submission_id = submission_id

        self._original_image = QImage(str(img_path))

        self.base_lines = []
        if lines_path.exists():
            with open(lines_path) as f:
                data = json.load(f)
                self.base_lines = data.get("lines", [])

        self.test_lines = []
        self._use_test = False

        self._update_lines_display()
        self.image_viewer.fit_to_view()

    def _get_current_lines(self) -> list[dict]:
        return self.test_lines if self._use_test else self.base_lines

    def _update_lines_display(self):
        lines = self._get_current_lines()
        current_zoom = self.image_viewer._zoom
        if self._original_image is not None:
            self.image_viewer.set_image_and_lines(self._original_image, lines)
        self.image_viewer.set_zoom(current_zoom)
        self._populate_lines()
        mode = "Test" if self._use_test else "Base"
        self.lbl_mode.setText(f"Mode: {mode}")
        self.btn_toggle_base.setText("Show Base" if self._use_test else "Show Test")

    def _toggle_base_test(self):
        if not self.test_lines:
            QMessageBox.information(self, "No Test Result", "Run detection first.")
            return
        self._use_test = not self._use_test
        self._update_lines_display()

    def _run_surya_detection(self):
        if not hasattr(self, '_current_img_path'):
            QMessageBox.information(self, "No Image", "Select a page first.")
            return

        if self._loading_model:
            return
        self._loading_model = True
        self.btn_run_detection.setEnabled(False)
        self.btn_run_detection.setText("Loading...")

        def do_detection():
            try:
                text_thresh = self.spin_text_thresh.value()
                blank_thresh = self.spin_blank_thresh.value()
                target_width = int(self.spin_target_width.value())
                model = init_surya_model(text_threshold=text_thresh, blank_threshold=blank_thresh)
                with self._model_lock:
                    self._surya_model = model

                orig_img = QImage(str(self._current_img_path))
                orig_w = orig_img.width()
                img = orig_img
                if orig_w != target_width:
                    new_h = int(orig_img.height() * target_width / orig_w)
                    img = orig_img.scaled(
                        target_width, new_h,
                        Qt.IgnoreAspectRatio,
                        Qt.SmoothTransformation,
                    )
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                tmp_path = tmp.name
                img.save(tmp_path, "PNG")
                tmp.close()

                result = extract_lines(tmp_path, model)

                scale = orig_w / img.width()
                if scale != 1.0:
                    for line in result["lines"]:
                        bbox = line.get("bbox")
                        if bbox and len(bbox) == 4:
                            line["bbox"] = [v * scale for v in bbox]
                        poly = line.get("polygon")
                        if poly:
                            line["polygon"] = [[p[0] * scale, p[1] * scale] for p in poly]

                from PySide6.QtCore import QCoreApplication
                QCoreApplication.postEvent(self, _DetectionDoneEvent(result["lines"]))
            except Exception as e:
                from PySide6.QtCore import QCoreApplication
                QCoreApplication.postEvent(self, _DetectionErrorEvent(str(e)))

        threading.Thread(target=do_detection, daemon=True).start()

    def _less_joining(self):
        text_val = min(1.0, self.spin_text_thresh.value() + 0.1)
        blank_val = min(0.99, self.spin_blank_thresh.value() + 0.1)
        if text_val > blank_val:
            self.spin_text_thresh.setValue(text_val)
            self.spin_blank_thresh.setValue(blank_val)

    def _add_joining(self):
        text_val = max(0.01, self.spin_text_thresh.value() - 0.1)
        blank_val = max(0.01, self.spin_blank_thresh.value() - 0.1)
        if text_val > blank_val:
            self.spin_text_thresh.setValue(text_val)
            self.spin_blank_thresh.setValue(blank_val)

    def _less_small_boxes(self):
        text_val = min(1.0, self.spin_text_thresh.value() + 0.1)
        blank_val = min(0.99, self.spin_blank_thresh.value() + 0.1)
        if text_val > blank_val:
            self.spin_text_thresh.setValue(text_val)
            self.spin_blank_thresh.setValue(blank_val)

    def _add_small_boxes(self):
        text_val = max(0.01, self.spin_text_thresh.value() - 0.1)
        blank_val = max(0.01, self.spin_blank_thresh.value() - 0.1)
        if text_val > blank_val:
            self.spin_text_thresh.setValue(text_val)
            self.spin_blank_thresh.setValue(blank_val)

    def _reset_settings(self):
        default_text, default_blank = self._load_thresh_defaults()
        self.spin_text_thresh.setValue(default_text)
        self.spin_blank_thresh.setValue(default_blank)

    def _save_settings(self):
        settings = QSettings("transcriptor", "ocr_viewer")
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("maximized", self.isMaximized())
        settings.setValue("splitter", self._splitter.saveState())
        screen = self.screen()
        if screen:
            settings.setValue("screenSize", screen.size())

    def _restore_settings(self):
        settings = QSettings("transcriptor", "ocr_viewer")
        geom = settings.value("geometry")
        maximized = settings.value("maximized", False, type=bool)
        splitter = settings.value("splitter")
        saved_screen = settings.value("screenSize")
        if geom and saved_screen:
            current_screen = self.screen()
            if current_screen and saved_screen == current_screen.size():
                self.restoreGeometry(geom)
                if maximized:
                    self.showMaximized()
                if splitter:
                    self._splitter.restoreState(splitter)
                return
        self.showMaximized()

    def closeEvent(self, event):
        self._save_settings()
        super().closeEvent(event)

    def _on_detection_done(self, lines: list[dict]):
        self._loading_model = False
        self.btn_run_detection.setEnabled(True)
        self.btn_run_detection.setText("Run Detection")
        self.test_lines = lines
        self._use_test = True
        self._update_lines_display()

    def _populate_lines(self):
        lines = self._get_current_lines()
        self.lines_table.setRowCount(len(lines))
        for i, line in enumerate(lines):
            idx = line["index"]
            conf = line.get("confidence", 0.0)
            bbox = line.get("bbox", [])
            bbox_str = f"[{bbox[0]:.0f}, {bbox[1]:.0f}, {bbox[2]:.0f}, {bbox[3]:.0f}]" if len(bbox) == 4 else ""

            self.lines_table.setItem(i, 0, QTableWidgetItem(str(idx)))
            self.lines_table.setItem(i, 1, QTableWidgetItem(f"{conf:.3f}"))
            self.lines_table.setItem(i, 2, QTableWidgetItem(bbox_str))

    def _on_line_select(self):
        sel = self.lines_table.selectedItems()
        if not sel:
            return
        row = sel[0].row()
        self.image_viewer.select_line(row)

    def event(self, e):
        if e.type() == 1000:
            self._on_detection_done(e.lines)
            return True
        if e.type() == 1001:
            self._loading_model = False
            self.btn_run_detection.setEnabled(True)
            self.btn_run_detection.setText("Run Detection")
            QMessageBox.warning(self, "Detection Error", f"Error: {e.error}")
            return True
        return super().event(e)


class _DetectionDoneEvent(QEvent):
    def __init__(self, lines):
        super().__init__(QEvent.Type(1000))
        self.lines = lines


class _DetectionErrorEvent(QEvent):
    def __init__(self, error):
        super().__init__(QEvent.Type(1001))
        self.error = error


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <output_folder_path>")
        sys.exit(1)

    output_path = sys.argv[1]
    if not Path(output_path).is_dir():
        print(f"Error: '{output_path}' is not a directory")
        sys.exit(1)

    app = QApplication(sys.argv)
    viewer = OCRViewer(output_path)
    viewer.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
