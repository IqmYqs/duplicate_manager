import hashlib
import os
import re
import shutil
import sys
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QFileDialog, QTreeWidget, QTreeWidgetItem, QLabel, QMessageBox,
    QProgressBar, QAbstractItemView, QHeaderView)
from send2trash import send2trash

CHUNK = 1024 * 1024

def fmt_size(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.1f} {unit}'
        n /= 1024

def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()

def name_match_key(name):
    stem = Path(name).stem
    key = re.split(r'\s+-\s+|\s+\(|\s+\[|_', stem, maxsplit=1)[0]
    return ' '.join(key.casefold().split())

class Scanner(QThread):
    progress = Signal(int, int)
    completed = Signal(object, object, object)

    def __init__(self, root):
        super().__init__()
        self.root = root
        self.cancelled = False

    def run(self):
        by_size = defaultdict(list)
        files = []
        errors = []
        def onerror(e):
            errors.append(str(e))
        for folder, _, names in os.walk(self.root, onerror=onerror, followlinks=False):
            if self.cancelled:
                break
            for name in names:
                if self.cancelled:
                    break
                path = os.path.join(folder, name)
                try:
                    if os.path.islink(path):
                        continue
                    st = os.stat(path)
                    if os.path.isfile(path):
                        by_size[st.st_size].append(path)
                        files.append((path, st.st_size, st.st_mtime))
                except OSError as e:
                    errors.append(f'{path}: {e}')
        candidates = [p for paths in by_size.values() if len(paths) > 1 for p in paths]
        by_hash = defaultdict(list)
        for i, path in enumerate(candidates, 1):
            if self.cancelled:
                break
            try:
                st_before = os.stat(path)
                key = (st_before.st_size, digest(path))
                st_after = os.stat(path)
                if (st_before.st_size, st_before.st_mtime_ns) != (st_after.st_size, st_after.st_mtime_ns):
                    errors.append(f'File changed during scan: {path}')
                else:
                    by_hash[key].append((path, st_after.st_mtime))
            except OSError as e:
                errors.append(f'{path}: {e}')
            self.progress.emit(i, len(candidates))
        groups = [(key, sorted(items, key=lambda x: x[0].casefold()))
                  for key, items in by_hash.items() if len(items) > 1]
        groups.sort(key=lambda x: (-x[0][0], x[1][0][0].casefold()))
        exact_path_sets = [set(path for path, _ in items) for _, items in groups]
        by_name = defaultdict(list)
        for path, size, mtime in files:
            key = name_match_key(os.path.basename(path))
            if len(key) >= 4:
                by_name[key].append((path, size, mtime))
        name_groups = []
        for key, items in by_name.items():
            paths = {path for path, _, _ in items}
            if len(items) > 1 and not any(paths <= exact_paths for exact_paths in exact_path_sets):
                name_groups.append((key, sorted(items, key=lambda x: x[0].casefold())))
        name_groups.sort(key=lambda x: x[0])
        self.completed.emit(groups if not self.cancelled else [],
                            name_groups if not self.cancelled else [], errors)

class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Duplicate File Manager')
        self.resize(1180, 730)
        self.root = ''
        self.worker = None
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        top = QHBoxLayout()
        self.folder_label = QLabel('ยังไม่ได้เลือกโฟลเดอร์')
        choose = QPushButton('เลือกโฟลเดอร์')
        choose.clicked.connect(self.choose_folder)
        self.scan_btn = QPushButton('เริ่มสแกน')
        self.scan_btn.clicked.connect(self.scan)
        self.stop_btn = QPushButton('หยุด')
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        top.addWidget(choose)
        top.addWidget(self.folder_label, 1)
        top.addWidget(self.scan_btn)
        top.addWidget(self.stop_btn)
        layout.addLayout(top)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['ไฟล์ / กลุ่ม', 'ขนาด', 'แก้ไขล่าสุด', 'SHA-256', 'ตำแหน่งเต็ม'])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.tree.itemDoubleClicked.connect(self.open_location)
        layout.addWidget(self.tree)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.status = QLabel('เลือกโฟลเดอร์เพื่อเริ่มต้น • ดับเบิลคลิกไฟล์เพื่อเปิดตำแหน่ง')
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        for label, callback in [('เลือกไฟล์ซ้ำอัตโนมัติ', self.auto_select),
                                ('ยกเลิกการเลือก', self.clear_checks),
                                ('เปิดตำแหน่ง', self.open_selected),
                                ('Copy', lambda: self.perform('copy')),
                                ('Move', lambda: self.perform('move')),
                                ('ลบลง Recycle Bin', lambda: self.perform('delete'))]:
            b = QPushButton(label)
            b.clicked.connect(callback)
            actions.addWidget(b)
        layout.addLayout(actions)
        self.setStyleSheet('QWidget { font-size: 13px; } QPushButton { padding: 8px 12px; }')

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, 'เลือกโฟลเดอร์')
        if path:
            self.root = path
            self.folder_label.setText(path)

    def scan(self):
        if not self.root:
            QMessageBox.information(self, 'เลือกโฟลเดอร์', 'กรุณาเลือกโฟลเดอร์ก่อน')
            return
        if self.worker and self.worker.isRunning():
            return
        self.tree.clear()
        self.progress.setValue(0)
        self.scan_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.worker = Scanner(self.root)
        self.worker.progress.connect(self.update_progress)
        self.worker.completed.connect(self.show_results)
        self.worker.finished.connect(lambda: (self.scan_btn.setEnabled(True), self.stop_btn.setEnabled(False)))
        self.status.setText('กำลังค้นหาไฟล์และคำนวณ SHA-256...')
        self.worker.start()

    def stop(self):
        if self.worker:
            self.worker.cancelled = True
            self.status.setText('กำลังหยุด...')

    def update_progress(self, done, total):
        self.progress.setValue(round(100 * done / total) if total else 100)
        self.status.setText(f'ตรวจสอบ Hash: {done:,}/{total:,} ไฟล์')

    def show_results(self, groups, name_groups, errors):
        self.tree.clear()
        total_redundant = 0
        for idx, ((size, sha), items) in enumerate(groups, 1):
            parent = QTreeWidgetItem(self.tree, [f'กลุ่ม {idx} ({len(items)} ไฟล์)', fmt_size(size), '', sha, ''])
            parent.setData(0, Qt.ItemDataRole.UserRole, 'content')
            parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            for path, mtime in items:
                child = QTreeWidgetItem(parent, [Path(path).name, fmt_size(size),
                    datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S'), sha, path])
                child.setData(0, Qt.ItemDataRole.UserRole, (path, size, sha))
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Unchecked)
            total_redundant += len(items) - 1
            parent.setExpanded(True)
        for key, items in name_groups:
            parent = QTreeWidgetItem(self.tree, [f'ชื่อคล้ายกัน: {key} ({len(items)} ไฟล์)', '', '', '', ''])
            parent.setData(0, Qt.ItemDataRole.UserRole, 'name')
            parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            for path, size, mtime in items:
                child = QTreeWidgetItem(parent, [Path(path).name, fmt_size(size),
                    datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S'), '', path])
                child.setData(0, Qt.ItemDataRole.UserRole, (path, size, None))
                child.setFlags(child.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            parent.setExpanded(True)
        self.progress.setValue(100)
        self.status.setText(f'พบไฟล์ซ้ำจริง {len(groups):,} กลุ่ม • ชื่อคล้าย {len(name_groups):,} กลุ่ม • ไฟล์ส่วนเกิน {total_redundant:,} ไฟล์ • ข้อผิดพลาด {len(errors):,} รายการ')
        if errors:
            QMessageBox.warning(self, 'ไฟล์บางส่วนอ่านไม่ได้',
                f'ข้ามไฟล์ที่มีปัญหา {len(errors)} รายการ\n\n' + '\n'.join(errors[:12]))

    def children(self):
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            if parent.data(0, Qt.ItemDataRole.UserRole) == 'content':
                yield parent, [parent.child(j) for j in range(parent.childCount())]

    def auto_select(self):
        for _, items in self.children():
            for i, item in enumerate(items):
                item.setCheckState(0, Qt.CheckState.Unchecked if i == 0 else Qt.CheckState.Checked)

    def clear_checks(self):
        for _, items in self.children():
            for item in items:
                item.setCheckState(0, Qt.CheckState.Unchecked)

    def selected_paths(self):
        return [item.data(0, Qt.ItemDataRole.UserRole)
                for _, items in self.children() for item in items
                if item.checkState(0) == Qt.CheckState.Checked]

    def open_location(self, item, column=0):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data:
            path = data[0]
            if os.path.exists(path):
                import subprocess
                subprocess.Popen(['explorer.exe', '/select,', os.path.normpath(path)])
            else:
                QMessageBox.warning(self, 'ไม่พบไฟล์', path)

    def open_selected(self):
        for item in self.tree.selectedItems():
            if item.data(0, Qt.ItemDataRole.UserRole):
                self.open_location(item)
                return
        QMessageBox.information(self, 'เลือกไฟล์', 'คลิกเลือกแถวไฟล์ก่อน (ไม่ใช่ Checkbox)')

    def perform(self, action):
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, 'กำลังสแกน', 'รอให้การสแกนเสร็จก่อน')
            return
        selected = self.selected_paths()
        if not selected:
            QMessageBox.information(self, 'ยังไม่ได้เลือก', 'ติ๊ก Checkbox ของไฟล์ที่ต้องการก่อน')
            return
        target = None
        if action in ('copy', 'move'):
            target = QFileDialog.getExistingDirectory(self, 'เลือกโฟลเดอร์ปลายทาง')
            if not target:
                return
            target = os.path.abspath(target)
            if action == 'move' and os.path.normcase(target) == os.path.normcase(os.path.abspath(self.root)):
                QMessageBox.warning(self, 'ปลายทางไม่เหมาะสม', 'กรุณาเลือกโฟลเดอร์อื่นที่ไม่ใช่โฟลเดอร์ต้นทาง')
                return
        if action == 'delete':
            # Prevent accidentally deleting every known copy within a duplicate group.
            for _, items in self.children():
                if items and all(i.checkState(0) == Qt.CheckState.Checked for i in items):
                    QMessageBox.warning(self, 'ป้องกันการลบทั้งหมด',
                        'มีอย่างน้อยหนึ่งกลุ่มที่เลือกทุกไฟล์ กรุณาเหลือไฟล์อย่างน้อยหนึ่งรายการต่อกลุ่ม')
                    return
        verb = {'copy': 'คัดลอก', 'move': 'ย้าย', 'delete': 'ส่งไป Recycle Bin'}[action]
        if QMessageBox.question(self, 'ยืนยัน', f'{verb} {len(selected)} ไฟล์?\n' +
            (f'ปลายทาง: {target}\n' if target else '') +
            'โปรแกรมจะตรวจสอบ Hash อีกครั้งก่อนดำเนินการ') != QMessageBox.StandardButton.Yes:
            return
        success = 0
        errors = []
        for path, size, sha in selected:
            try:
                if not os.path.isfile(path) or os.path.getsize(path) != size or digest(path) != sha:
                    raise ValueError('ไฟล์เปลี่ยนแปลงหลังสแกน')
                if action == 'delete':
                    send2trash(path)
                else:
                    # Preserve relative directory layout and avoid overwriting existing files.
                    relative = os.path.relpath(path, self.root)
                    if relative.startswith('..'):
                        raise ValueError('ไฟล์อยู่นอกโฟลเดอร์ต้นทาง')
                    destination = os.path.join(target, relative)
                    if os.path.normcase(os.path.abspath(destination)) == os.path.normcase(os.path.abspath(path)):
                        raise ValueError('ต้นทางและปลายทางตรงกัน')
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    if os.path.exists(destination):
                        base, ext = os.path.splitext(destination)
                        destination = f'{base}_{uuid.uuid4().hex[:8]}{ext}'
                    if action == 'copy':
                        shutil.copy2(path, destination)
                    else:
                        shutil.move(path, destination)
                success += 1
            except Exception as e:
                errors.append(f'{path}: {e}')
        QMessageBox.information(self, 'ผลการดำเนินการ',
            f'สำเร็จ {success} ไฟล์ • ไม่สำเร็จ {len(errors)} ไฟล์' +
            ('\n\n' + '\n'.join(errors[:10]) if errors else '') +
            '\n\nกรุณาสแกนใหม่เพื่ออัปเดตผลลัพธ์')
        self.tree.clear()
        self.status.setText('ข้อมูลเปลี่ยนแปลงแล้ว กรุณาสแกนใหม่')

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = Window()
    window.show()
    sys.exit(app.exec())
