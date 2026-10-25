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
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QFileDialog, QTreeWidget, QTreeWidgetItem, QLabel, QMessageBox,
    QProgressBar, QAbstractItemView, QHeaderView, QInputDialog, QMenu)
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
                        files.append((path, st.st_size, st.st_mtime, st.st_mtime_ns))
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
        for path, size, mtime, mtime_ns in files:
            key = name_match_key(os.path.basename(path))
            if len(key) >= 4:
                by_name[key].append((path, size, mtime, mtime_ns))
        name_groups = []
        for key, items in by_name.items():
            paths = {path for path, _, _, _ in items}
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
        self.tree.itemDoubleClicked.connect(self.open_file)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_context_menu)
        rename_action = QAction('เปลี่ยนชื่อ', self.tree)
        rename_action.setShortcut(QKeySequence(Qt.Key.Key_F2))
        rename_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        rename_action.triggered.connect(self.rename_selected)
        self.tree.addAction(rename_action)
        layout.addWidget(self.tree)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.status = QLabel('เลือกโฟลเดอร์เพื่อเริ่มต้น • ดับเบิลคลิกไฟล์เพื่อเปิดไฟล์')
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        for label, callback in [('เลือกไฟล์ซ้ำอัตโนมัติ', self.auto_select),
                                ('ยกเลิกการเลือก', self.clear_checks),
                                ('เปิดตำแหน่ง', self.open_selected),
                                ('Copy', lambda: self.perform('copy')),
                                ('Move', lambda: self.perform('move')),
                                ('ลบถาวร', lambda: self.perform('delete'))]:
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
                child.setData(0, Qt.ItemDataRole.UserRole, (path, size, sha, None))
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Unchecked)
            total_redundant += len(items) - 1
            parent.setExpanded(True)
        for key, items in name_groups:
            parent = QTreeWidgetItem(self.tree, [f'ชื่อคล้ายกัน: {key} ({len(items)} ไฟล์)', '', '', '', ''])
            parent.setData(0, Qt.ItemDataRole.UserRole, 'name')
            parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            for path, size, mtime, mtime_ns in items:
                child = QTreeWidgetItem(parent, [Path(path).name, fmt_size(size),
                    datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S'), '', path])
                child.setData(0, Qt.ItemDataRole.UserRole, (path, size, None, mtime_ns))
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Unchecked)
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
        self.clear_checks()
        for _, items in self.children():
            for i, item in enumerate(items):
                item.setCheckState(0, Qt.CheckState.Unchecked if i == 0 else Qt.CheckState.Checked)

    def clear_checks(self):
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            if parent.data(0, Qt.ItemDataRole.UserRole) in ('content', 'name'):
                for j in range(parent.childCount()):
                    parent.child(j).setCheckState(0, Qt.CheckState.Unchecked)

    def selected_paths(self):
        selected = []
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            if parent.data(0, Qt.ItemDataRole.UserRole) in ('content', 'name'):
                selected.extend(parent.child(j).data(0, Qt.ItemDataRole.UserRole)
                                for j in range(parent.childCount())
                                if parent.child(j).checkState(0) == Qt.CheckState.Checked)
        return selected

    def open_location(self, item, column=0):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple):
            path = data[0]
            if os.path.exists(path):
                import subprocess
                subprocess.Popen(['explorer.exe', '/select,', os.path.normpath(path)])
            else:
                QMessageBox.warning(self, 'ไม่พบไฟล์', path)

    def open_file(self, item, column=0):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(data, tuple):
            return
        path = data[0]
        if not os.path.isfile(path):
            QMessageBox.warning(self, 'ไม่พบไฟล์', path)
            return
        try:
            os.startfile(path)
        except OSError as error:
            QMessageBox.warning(self, 'เปิดไฟล์ไม่สำเร็จ', f'{path}\n\n{error}')

    def show_context_menu(self, position):
        item = self.tree.itemAt(position)
        if not item or not isinstance(item.data(0, Qt.ItemDataRole.UserRole), tuple):
            return
        self.tree.clearSelection()
        self.tree.setCurrentItem(item)
        item.setSelected(True)
        menu = QMenu(self)
        rename_action = menu.addAction('เปลี่ยนชื่อ\tF2')
        rename_action.triggered.connect(self.rename_selected)
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def rename_selected(self):
        items = self.tree.selectedItems()
        if len(items) != 1 or not isinstance(items[0].data(0, Qt.ItemDataRole.UserRole), tuple):
            return
        data = items[0].data(0, Qt.ItemDataRole.UserRole)
        old_path = data[0]
        if not os.path.isfile(old_path):
            QMessageBox.warning(self, 'ไม่พบไฟล์', old_path)
            return
        new_name, accepted = QInputDialog.getText(
            self, 'เปลี่ยนชื่อไฟล์', 'ชื่อใหม่:', text=os.path.basename(old_path))
        if not accepted:
            return
        new_name = new_name.strip()
        if (not new_name or new_name in ('.', '..') or
                any(char in new_name for char in '<>:"/\\|?*') or
                new_name.endswith(('.', ' '))):
            QMessageBox.warning(self, 'ชื่อไฟล์ไม่ถูกต้อง', 'กรุณาระบุชื่อไฟล์ที่ใช้ได้')
            return
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        if os.path.normcase(new_path) == os.path.normcase(old_path):
            return
        if os.path.exists(new_path):
            QMessageBox.warning(self, 'มีไฟล์ชื่อนี้แล้ว', new_path)
            return
        try:
            os.rename(old_path, new_path)
        except OSError as error:
            QMessageBox.warning(self, 'เปลี่ยนชื่อไม่สำเร็จ', f'{old_path}\n\n{error}')
            return
        stat = os.stat(new_path)
        items[0].setText(0, new_name)
        items[0].setText(2, datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'))
        items[0].setText(4, new_path)
        items[0].setData(0, Qt.ItemDataRole.UserRole,
                         (new_path, data[1], data[2], stat.st_mtime_ns if data[2] is None else None))
        parent = items[0].parent()
        if parent.data(0, Qt.ItemDataRole.UserRole) == 'name':
            parent.setText(0, f'ชื่อคล้ายกัน (ผลสแกนเดิม; มีการเปลี่ยนชื่อ) ({parent.childCount()} ไฟล์)')
        self.status.setText('เปลี่ยนชื่อไฟล์แล้ว ข้อมูลแถวนี้อัปเดตทันที; กลุ่มชื่อคล้ายอาจต้องสแกนใหม่เพื่อจัดกลุ่มตามชื่อปัจจุบัน')

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
        similar_count = sum(sha is None for _, _, sha, _ in selected)
        similar_warning = (f'\n{similar_count} ไฟล์เป็นเพียงชื่อคล้ายกัน ไม่ได้ยืนยันว่าเนื้อหาซ้ำ\n'
                           if similar_count else '')
        verb = {'copy': 'คัดลอก', 'move': 'ย้าย', 'delete': 'ลบถาวร'}[action]
        permanent_warning = ('การลบนี้จะลบไฟล์ออกจากเครื่องทันที ไม่ผ่าน Recycle Bin และกู้คืนไม่ได้\n'
                             if action == 'delete' else '')
        if QMessageBox.question(self, 'ยืนยัน', f'{verb} {len(selected)} ไฟล์?\n' +
            (f'ปลายทาง: {target}\n' if target else '') +
            permanent_warning +
            similar_warning +
            'โปรแกรมจะตรวจสอบว่าไฟล์ไม่เปลี่ยนแปลงก่อนดำเนินการ') != QMessageBox.StandardButton.Yes:
            return
        success = 0
        errors = []
        deleted_paths = set()
        for path, size, sha, scan_mtime_ns in selected:
            try:
                if not os.path.isfile(path):
                    raise ValueError('ไฟล์เปลี่ยนแปลงหลังสแกน')
                st = os.stat(path)
                if st.st_size != size or (sha and digest(path) != sha) or (
                    sha is None and st.st_mtime_ns != scan_mtime_ns
                ):
                    raise ValueError('ไฟล์เปลี่ยนแปลงหลังสแกน')
                if action == 'delete':
                    os.remove(path)
                    if os.path.lexists(path):
                        raise OSError('ลบไฟล์แล้ว แต่ยังพบไฟล์ที่ตำแหน่งเดิม')
                    deleted_paths.add(path)
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
        result_message = f'สำเร็จ {success} ไฟล์ • ไม่สำเร็จ {len(errors)} ไฟล์'
        if action == 'delete':
            for parent_index in range(self.tree.topLevelItemCount() - 1, -1, -1):
                parent = self.tree.topLevelItem(parent_index)
                for child_index in range(parent.childCount() - 1, -1, -1):
                    child = parent.child(child_index)
                    data = child.data(0, Qt.ItemDataRole.UserRole)
                    if isinstance(data, tuple) and data[0] in deleted_paths:
                        parent.removeChild(child)
                if parent.childCount() < 2:
                    self.tree.takeTopLevelItem(parent_index)
                elif parent.childCount():
                    title = re.sub(r'\(\d+ ไฟล์\)$', f'({parent.childCount()} ไฟล์)', parent.text(0))
                    parent.setText(0, title)
            if success:
                result_message += '\n\nนำไฟล์ที่ลบสำเร็จออกจากรายการแล้ว กลุ่มที่ยังเหลือไฟล์ซ้ำจะแสดงต่อไป'
                self.status.setText(f'ลบแล้ว {success} ไฟล์; อัปเดตรายการบนหน้าจอแล้ว')
            else:
                self.status.setText('ไม่มีไฟล์ถูกลบ รายการบนหน้าจอยังคงเดิม')
        else:
            result_message += '\n\nกรุณาสแกนใหม่เพื่ออัปเดตผลลัพธ์'
            self.tree.clear()
            self.status.setText('ข้อมูลเปลี่ยนแปลงแล้ว กรุณาสแกนใหม่')
        if errors:
            result_message += '\n\n' + '\n'.join(errors[:10])
        QMessageBox.information(self, 'ผลการดำเนินการ', result_message)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = Window()
    window.show()
    sys.exit(app.exec())
