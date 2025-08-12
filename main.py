#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import time
import shutil
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# 可选：将删除操作移动到废纸篓（macOS 更安全）。
# 若未安装，将回退为永久删除（弹窗确认）。
try:
    from send2trash import send2trash  # type: ignore
    HAS_TRASH = True
except Exception:
    HAS_TRASH = False
    def send2trash(_path: str):  # 占位，未安装时不会被调用
        raise RuntimeError("send2trash not available")


SKIP_FOLDERS = {
    'node_modules', 'dist', '.git', '.idea', '.vscode', 'build', 'out', '.next', '.nuxt', '.vercel'
}

VUE_SIGNATURES = {
    'vue', '@vue/cli-service', '@vitejs/plugin-vue', 'vite', 'nuxt', 'nuxt3', 'vitepress', 'pinia', 'vue-router'
}


def chk_char(checked: bool) -> str:
    return '☑' if checked else '☐'


@dataclass
class ProjectInfo:
    path: str
    name: str
    last_active_ts: float = 0.0
    node_modules_path: Optional[str] = None
    dist_path: Optional[str] = None
    node_modules_size: int = 0
    dist_size: int = 0

    @property
    def reclaimable(self) -> int:
        return int(self.node_modules_size) + int(self.dist_size)

    @property
    def last_active_days(self) -> int:
        if not self.last_active_ts:
            return 99999
        return max(0, int((time.time() - self.last_active_ts) // 86400))


def human_size(num_bytes: int) -> str:
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    size = float(num_bytes)
    for u in units:
        if size < 1024.0:
            return f"{size:.1f} {u}"
        size /= 1024.0
    return f"{size:.1f} PB"


def is_vue_project(dir_path: str) -> bool:
    pkg = os.path.join(dir_path, 'package.json')
    if not os.path.isfile(pkg):
        return False
    try:
        with open(pkg, 'r', encoding='utf-8') as f:
            data = json.load(f)
        deps = data.get('dependencies', {}) or {}
        dev = data.get('devDependencies', {}) or {}
        all_keys = set(map(str.lower, list(deps.keys()) + list(dev.keys())))
        return any(sig in all_keys for sig in VUE_SIGNATURES)
    except Exception:
        return False


def safe_scandir(path: str):
    try:
        with os.scandir(path) as it:
            for entry in it:
                yield entry
    except Exception:
        return


def calc_dir_size(path: str) -> int:
    total = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        for entry in safe_scandir(cur):
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
            except Exception:
                continue
    return total


def latest_activity_ts(dir_path: str) -> float:
    latest = 0.0
    stack = [dir_path]
    while stack:
        cur = stack.pop()
        for entry in safe_scandir(cur):
            name = entry.name
            if entry.is_dir(follow_symlinks=False) and name in SKIP_FOLDERS:
                continue
            try:
                st = entry.stat(follow_symlinks=False)
                latest = max(latest, st.st_mtime, st.st_ctime)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
            except Exception:
                continue
    if latest == 0.0:
        # 兜底：使用项目目录本身时间
        try:
            st = os.stat(dir_path)
            latest = max(st.st_mtime, st.st_ctime)
        except Exception:
            pass
    return latest


class CleanerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Vue 项目垃圾清理工具 (macOS)")
        self.geometry("980x640")
        self.minsize(900, 560)

        self.root_dir_var = tk.StringVar(value=os.path.expanduser('~'))
        self.days_var = tk.IntVar(value=30)
        self.status_var = tk.StringVar(value="准备就绪")
        self.only_old_var = tk.BooleanVar(value=True)
        self.selected_total_var = tk.StringVar(value="已选可回收: 0 B (0 项)")
        # 新增：是否使用废纸篓（默认根据 send2trash 是否可用）
        self.use_trash_var = tk.BooleanVar(value=HAS_TRASH)

        self.projects: Dict[str, ProjectInfo] = {}
        self.checked: Dict[str, bool] = {}
        self._scan_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()

        # 新增：排序状态（默认按“未活跃天数”降序）
        self.sort_col: str = 'days'
        self.sort_reverse: bool = True

        self._build_ui()
        # 新增：启动时居中窗口（延迟到下一轮事件循环，确保几何已就绪）
        self.after(0, self._center_on_screen)

    # 新增：窗口居中方法
    def _center_on_screen(self):
        try:
            self.update_idletasks()
            # 优先使用当前窗口大小；若过小则从 geometry 解析
            w = self.winfo_width()
            h = self.winfo_height()
            if w <= 1 or h <= 1:
                try:
                    geo = self.geometry().split('+')[0]
                    w, h = map(int, geo.split('x'))
                except Exception:
                    w, h = 980, 640
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2)
            self.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=10, pady=8)

        ttk.Label(top, text="根目录:").pack(side=tk.LEFT)
        entry = ttk.Entry(top, textvariable=self.root_dir_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(top, text="浏览", command=self._choose_dir).pack(side=tk.LEFT)

        ttk.Label(top, text=" 近N天活跃阈值:").pack(side=tk.LEFT)
        spin = ttk.Spinbox(top, from_=1, to=3650, textvariable=self.days_var, width=6)
        spin.pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(top, text="仅显示超过阈值未活跃的项目", variable=self.only_old_var, command=self._refresh_view).pack(side=tk.LEFT, padx=8)

        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, padx=10)
        ttk.Button(btns, text="扫描", command=self.start_scan).pack(side=tk.LEFT)
        # 新增：取消扫描按钮
        ttk.Button(btns, text="取消扫描", command=self.cancel_scan).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="全选", command=lambda: self._select_all(True)).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="全不选", command=lambda: self._select_all(False)).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="仅选未活跃项目", command=self._select_old_only).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="预估可回收空间", command=self._estimate).pack(side=tk.LEFT, padx=12)
        ttk.Button(btns, text="清理所选", command=self.delete_selected).pack(side=tk.RIGHT)

        # 表格，首列为复选框
        columns = ("chk", "name", "days", "nm", "dist", "reclaim", "path")
        tree = ttk.Treeview(self, columns=columns, show='headings', selectmode='extended')
        self.tree = tree
        # 基础表头文本
        self._heading_labels = {
            "chk": "选中",
            "name": "项目",
            "days": "未活跃天数",
            "nm": "node_modules",
            "dist": "dist",
            "reclaim": "可回收",
            "path": "路径",
        }
        tree.heading("chk", text=self._heading_labels["chk"])  # 复选列不参与排序
        # 可排序列：点击表头切换排序
        tree.heading("name", text=self._heading_labels["name"], command=lambda: self._on_sort('name'))
        tree.heading("days", text=self._heading_labels["days"], command=lambda: self._on_sort('days'))
        tree.heading("nm", text=self._heading_labels["nm"], command=lambda: self._on_sort('nm'))
        tree.heading("dist", text=self._heading_labels["dist"], command=lambda: self._on_sort('dist'))
        tree.heading("reclaim", text=self._heading_labels["reclaim"], command=lambda: self._on_sort('reclaim'))
        tree.heading("path", text=self._heading_labels["path"], command=lambda: self._on_sort('path'))

        tree.column("chk", width=60, anchor=tk.CENTER)
        tree.column("name", width=200, anchor=tk.W)
        tree.column("days", width=90, anchor=tk.CENTER)
        tree.column("nm", width=140, anchor=tk.E)
        tree.column("dist", width=120, anchor=tk.E)
        tree.column("reclaim", width=120, anchor=tk.E)
        tree.column("path", width=400, anchor=tk.W)

        tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)
        # 改为在鼠标释放时处理复选框，更稳定（macOS Tk 下更可靠）
        tree.bind('<ButtonRelease-1>', self._on_tree_click)

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, padx=10, pady=(0, 10))
        self.status_lbl = ttk.Label(bottom, textvariable=self.status_var)
        self.status_lbl.pack(side=tk.LEFT)
        ttk.Label(bottom, textvariable=self.selected_total_var).pack(side=tk.RIGHT, padx=(8, 0))
        # 新增：删除方式选择（复选）
        trash_chk = ttk.Checkbutton(bottom, text="使用废纸篓(安全)", variable=self.use_trash_var)
        if not HAS_TRASH:
            trash_chk.state(['disabled'])
            ttk.Label(bottom, text="未安装 send2trash，将执行永久删除").pack(side=tk.RIGHT, padx=(8, 0))
        trash_chk.pack(side=tk.RIGHT)

        # 初始化表头箭头
        self._update_heading_arrows()

    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.root_dir_var.get() or os.path.expanduser('~'))
        if d:
            self.root_dir_var.set(d)

    # ========== 扫描 ==========
    def start_scan(self):
        if self._scan_thread and self._scan_thread.is_alive():
            messagebox.showinfo("提示", "正在扫描中，请稍候…")
            return
        root = self.root_dir_var.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showerror("错误", "请选择有效的根目录")
            return
        self.projects.clear()
        self.checked.clear()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.status_var.set("扫描中…")
        self._stop_flag.clear()
        self._scan_thread = threading.Thread(target=self._scan_worker, args=(root,), daemon=True)
        self._scan_thread.start()

    def cancel_scan(self):
        if self._scan_thread and self._scan_thread.is_alive():
            self._stop_flag.set()
            self.status_var.set("取消扫描…")
        else:
            messagebox.showinfo("提示", "当前没有进行中的扫描")

    def _scan_worker(self, root: str):
        found = 0
        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            # 跳过常见大目录
            dirnames[:] = [d for d in dirnames if d not in SKIP_FOLDERS]
            if 'package.json' in filenames:
                if is_vue_project(dirpath):
                    pj = ProjectInfo(path=dirpath, name=os.path.basename(dirpath))
                    # 组件路径
                    nm = os.path.join(dirpath, 'node_modules')
                    ds = os.path.join(dirpath, 'dist')
                    if os.path.isdir(nm):
                        pj.node_modules_path = nm
                        pj.node_modules_size = calc_dir_size(nm)
                    if self._stop_flag.is_set():
                        break
                    if os.path.isdir(ds):
                        pj.dist_path = ds
                        pj.dist_size = calc_dir_size(ds)
                    pj.last_active_ts = latest_activity_ts(dirpath)
                    self.projects[dirpath] = pj
                    found += 1
                    self._update_status(f"发现 {found} 个 Vue 项目…")
                    # 实时插入或更新列表
                    self.after(0, self._insert_or_update_row, pj)
            if self._stop_flag.is_set():
                break
        # 根据是否取消展示不同状态
        if self._stop_flag.is_set():
            self._update_status(f"已取消扫描，共发现 {len(self.projects)} 个 Vue 项目")
        else:
            self._update_status(f"扫描完成，共发现 {len(self.projects)} 个 Vue 项目")
        # 扫描完成后统一刷新一次（排序/过滤）
        self.after(0, self._refresh_view)

    def _update_status(self, text: str):
        self.after(0, lambda: self.status_var.set(text))

    def _insert_or_update_row(self, pj: ProjectInfo):
        # 根据当前过滤条件决定是否显示
        if self.only_old_var.get() and pj.last_active_days < self.days_var.get():
            # 若存在已显示项且现在不满足过滤，则移除
            if self.tree.exists(pj.path):
                self.tree.delete(pj.path)
            return
        checked = self.checked.get(pj.path, False)
        values = (
            chk_char(checked),
            pj.name,
            pj.last_active_days,
            human_size(pj.node_modules_size),
            human_size(pj.dist_size),
            human_size(pj.reclaimable),
            pj.path,
        )
        if self.tree.exists(pj.path):
            # 更新现有行
            for col, val in zip(("chk","name","days","nm","dist","reclaim","path"), values):
                self.tree.set(pj.path, col, val)
        else:
            self.tree.insert('', tk.END, iid=pj.path, values=values)
        self._update_selected_total()

    def _refresh_view(self):
        # 清空
        # 记录当前勾选状态（已存在于 self.checked）
        self.tree.delete(*self.tree.get_children())
        # 过滤
        days_threshold = self.days_var.get()
        only_old = self.only_old_var.get()
        items: List[Tuple[str, ProjectInfo]] = []
        for p, pj in self.projects.items():
            if only_old and pj.last_active_days < days_threshold:
                continue
            items.append((p, pj))
        # 排序：依据当前表头选择
        items.sort(key=lambda kv: self._sort_key_for(kv[1]), reverse=self.sort_reverse)
        for p, pj in items:
            checked = self.checked.get(p, False)
            self.tree.insert('', tk.END, iid=p, values=(
                chk_char(checked),
                pj.name,
                pj.last_active_days,
                human_size(pj.node_modules_size),
                human_size(pj.dist_size),
                human_size(pj.reclaimable),
                pj.path,
            ))
        self._update_selected_total()

    # 新增：根据列生成排序键
    def _sort_key_for(self, pj: ProjectInfo):
        if self.sort_col == 'name':
            return (pj.name.lower(),)
        if self.sort_col == 'days':
            return (pj.last_active_days,)
        if self.sort_col == 'nm':
            return (pj.node_modules_size,)
        if self.sort_col == 'dist':
            return (pj.dist_size,)
        if self.sort_col == 'reclaim':
            return (pj.reclaimable,)
        if self.sort_col == 'path':
            return (pj.path.lower(),)
        # 默认回退
        return (pj.last_active_days, pj.reclaimable)

    # 新增：处理点击表头排序
    def _on_sort(self, col: str):
        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col
            # 数值列默认降序，其它列默认升序
            self.sort_reverse = col in ('days', 'nm', 'dist', 'reclaim')
        self._update_heading_arrows()
        self._refresh_view()

    # 新增：更新表头箭头指示
    def _update_heading_arrows(self):
        for cid, base in self._heading_labels.items():
            if cid == 'chk':
                self.tree.heading(cid, text=base)
                continue
            arrow = ''
            if cid == self.sort_col:
                arrow = ' ▼' if self.sort_reverse else ' ▲'
            self.tree.heading(cid, text=f"{base}{arrow}")

    def _selected_paths(self) -> List[str]:
        # 基于复选框的勾选状态
        return [p for p, v in self.checked.items() if v and self.tree.exists(p)]

    def _select_all(self, flag: bool):
        # 对当前可见行设置勾选
        for iid in self.tree.get_children():
            self.checked[iid] = flag
            self.tree.set(iid, 'chk', chk_char(flag))
        self._update_selected_total()

    def _select_old_only(self):
        # 仅勾选当前可见（即已按阈值过滤）的项目
        for iid in self.tree.get_children():
            self.checked[iid] = True
            self.tree.set(iid, 'chk', chk_char(True))
        self._update_selected_total()

    # 新增：处理点击首列复选框的事件
    def _on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region != 'cell':
            return
        col = self.tree.identify_column(event.x)
        if col != '#1':  # 只处理第一列“选中”
            return
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        current = self.checked.get(row_id, False)
        self.checked[row_id] = not current
        self.tree.set(row_id, 'chk', chk_char(not current))
        self._update_selected_total()
        return 'break'

    # 新增：更新底部“已选可回收”统计
    def _update_selected_total(self):
        total = 0
        count = 0
        for p in self._selected_paths():
            pj = self.projects.get(p)
            if not pj:
                continue
            total += pj.reclaimable
            count += 1
        self.selected_total_var.set(f"已选可回收: {human_size(total)} ({count} 项)")

    # ========== 预估 ==========
    def _estimate(self):
        total = 0
        count = 0
        for p in self._selected_paths():
            pj = self.projects.get(p)
            if not pj:
                continue
            total += pj.reclaimable
            count += 1
        messagebox.showinfo("预估", f"选中 {count} 个项目，可回收约 {human_size(total)}。")

    # ========== 删除 ==========
    def delete_selected(self):
        paths = self._selected_paths()
        if not paths:
            messagebox.showinfo("提示", "请先选择需要清理的项目")
            return
        # 构造删除列表：仅删除 node_modules 和 dist
        targets: List[Tuple[str, str]] = []  # (路径, 描述)
        total_bytes = 0
        for p in paths:
            pj = self.projects.get(p)
            if not pj:
                continue
            if pj.node_modules_path and os.path.isdir(pj.node_modules_path):
                targets.append((pj.node_modules_path, f"{pj.name} · node_modules"))
                total_bytes += pj.node_modules_size
            if pj.dist_path and os.path.isdir(pj.dist_path):
                targets.append((pj.dist_path, f"{pj.name} · dist"))
                total_bytes += pj.dist_size
        if not targets:
            messagebox.showinfo("提示", "选中的项目没有可清理的 node_modules 或 dist")
            return

        # 根据用户选择与可用性决定删除方式
        use_trash = bool(self.use_trash_var.get() and HAS_TRASH)
        human_total = human_size(total_bytes)
        if use_trash:
            ok = messagebox.askyesno("确认", f"将把 {len(targets)} 个目录移动至废纸篓，预计可回收 {human_total}。继续吗？")
        else:
            ok = messagebox.askyesno("确认(永久删除)", f"将永久删除 {len(targets)} 个目录，预计可回收 {human_total}。确定继续吗？")
        if not ok:
            return

        def worker():
            success = 0
            for idx, (path, label) in enumerate(targets, 1):
                try:
                    self._update_status(f"删除中({idx}/{len(targets)}): {label}")
                    if use_trash:
                        send2trash(path)
                    else:
                        if os.path.isdir(path):
                            shutil.rmtree(path, ignore_errors=False)
                        elif os.path.isfile(path):
                            os.remove(path)
                    success += 1
                except Exception as e:
                    print(f"删除失败: {label} -> {e}")
                finally:
                    # 删除后刷新对应项目的大小并更新行
                    pj_dir = os.path.dirname(path)
                    pj = self.projects.get(pj_dir)
                    if pj:
                        if path == pj.node_modules_path:
                            pj.node_modules_size = 0
                        if path == pj.dist_path:
                            pj.dist_size = 0
                        self.after(0, self._insert_or_update_row, pj)
            self._update_status(f"完成: 成功清理 {success}/{len(targets)} 个目录")

        threading.Thread(target=worker, daemon=True).start()


# 程序入口：确保运行脚本时弹出窗口
if __name__ == "__main__":
    try:
        app = CleanerApp()
        app.mainloop()
    except Exception as e:
        # 若 Tkinter 初始化失败，打印错误帮助排查
        print(f"启动失败: {e}")
