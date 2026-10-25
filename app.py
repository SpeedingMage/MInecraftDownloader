"""Tk desktop UI for the vanilla downloader; no third-party packages required."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from downloader import Cancelled, Network, install, install_layout, list_versions, make_plan


def readable_size(size: int) -> str:
    return f"{size / (1024 ** 2):,.1f} MiB"


def desktop_work_area(root):
    """Return desktop coordinates excluding the taskbar in this process's DPI space."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            rectangle = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rectangle), 0):
                return rectangle.left, rectangle.top, rectangle.right, rectangle.bottom
        except (AttributeError, OSError):
            pass
    return 0, 0, root.winfo_screenwidth(), max(1, root.winfo_screenheight() - 64)


def fitted_geometry(area):
    """Fit client size plus borders/title bar within a desktop work area."""
    left, top, right, bottom = area
    max_width = max(1, right - left - 32)
    max_height = max(1, bottom - top - 80)
    width, height = min(960, max_width), min(660, max_height)
    x = left + (right - left - width) // 2
    y = top + max(8, (bottom - top - height - 40) // 2)
    return width, height, x, y, max_width, max_height


def configure_style(root):
    """A consistent light theme, independent of the Windows native widget theme."""
    background, surface, ink, muted, green = "#eef3f0", "#ffffff", "#18362b", "#65786f", "#168252"
    root.configure(background=background)
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        tkfont.nametofont(name, root=root).configure(family="Microsoft YaHei UI", size=10)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=("Microsoft YaHei UI", 10), foreground=ink, background=background)
    style.configure("TFrame", background=background)
    style.configure("Card.TFrame", background=surface)
    style.configure("Card.TLabel", background=surface)
    style.configure("Muted.TLabel", foreground=muted, background=surface)
    style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 13, "bold"), background=surface)
    style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
    style.configure("Subtitle.TLabel", foreground=muted)
    style.configure("Badge.TLabel", foreground=green, background="#dcefe3", padding=(12, 6))
    style.configure("Mode.TLabel", foreground=green, background="#edf7f0", padding=(10, 8))
    style.configure("Metric.TLabel", font=("Microsoft YaHei UI", 12, "bold"), background=surface)
    style.configure("TButton", padding=(10, 5), background="#eaf1ec", borderwidth=0, foreground=ink)
    style.map("TButton", background=[("active", "#dce8df"), ("disabled", "#edf1ee")],
              foreground=[("disabled", "#9aa89f")])
    style.configure("Primary.TButton", background=green, foreground="white", font=("Microsoft YaHei UI", 10, "bold"), padding=(18, 7))
    style.map("Primary.TButton", background=[("disabled", "#b9d1c2"), ("active", "#106b42")], foreground=[("disabled", "#f4f8f5")])
    style.configure("Cancel.TButton", foreground="#a65046", background="#f9eeeb")
    style.map("Cancel.TButton", background=[("active", "#f1dcd6"), ("disabled", "#f3f5f3")], foreground=[("disabled", "#9aa89f")])
    style.configure("TEntry", fieldbackground="#f6f9f7", bordercolor="#d8e3dc", padding=8)
    style.map("TEntry", bordercolor=[("focus", green)], fieldbackground=[("disabled", "#f0f3f1")])
    style.configure("TCombobox", fieldbackground="#f6f9f7", background="#eaf1ec", bordercolor="#d8e3dc", padding=7, arrowsize=13)
    style.map("TCombobox", fieldbackground=[("readonly", "#f6f9f7"), ("disabled", "#f0f3f1")], selectbackground=[("readonly", "#f6f9f7")], selectforeground=[("readonly", ink)])
    style.configure("TSpinbox", fieldbackground="#f6f9f7", bordercolor="#d8e3dc", padding=6)
    style.configure("TCheckbutton", background=surface, padding=(0, 5))
    style.map("TCheckbutton", background=[("active", surface)])
    style.configure("Versions.Treeview", background=surface, fieldbackground=surface, borderwidth=0, rowheight=28)
    style.configure("Versions.Treeview.Heading", background="#edf3ef", foreground=muted, padding=(8, 9), font=("Microsoft YaHei UI", 9))
    style.map("Versions.Treeview", background=[("selected", "#dcefe3")], foreground=[("selected", "#106b42")])
    style.configure("TScrollbar", background="#d6e2d9", troughcolor=surface, borderwidth=0, arrowsize=12)
    style.configure("Transfer.Horizontal.TProgressbar", troughcolor="#e8efea", background=green, borderwidth=0, thickness=8)


class Application:
    def __init__(self, root: tk.Tk, auto_refresh: bool = True):
        self.root = root
        root.title("Minecraft 原版下载器 1.2.1")
        self.work_area = desktop_work_area(root)
        width, height, x, y, max_width, max_height = fitted_geometry(self.work_area)
        root.minsize(min(640, max_width), min(420, max_height))
        root.maxsize(max_width, max_height)
        root.geometry(f"{width}x{height}+{x}+{y}")
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.versions = []
        self.visible = []
        self.last_output = None
        self.operation_output = None
        self.search = tk.StringVar()
        self.snapshots = tk.BooleanVar(value=False)
        self.old = tk.BooleanVar(value=False)
        self.output = tk.StringVar(value=str(Path(__file__).resolve().parent / "downloads"))
        self.proxy_mode = tk.StringVar(value="系统代理")
        self.proxy_url = tk.StringVar(value="http://127.0.0.1:7897")
        self.workers = tk.IntVar(value=8)
        self.pack = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="正在准备版本列表…" if auto_refresh else "就绪")
        self.selection = tk.StringVar(value="请选择一个版本")
        self.destination_mode = tk.StringVar()
        self.destination_preview = tk.StringVar()
        self.version_count = tk.StringVar(value="等待加载")
        self.phase = tk.StringVar(value="就绪")
        self.percent = tk.StringVar(value="0%")
        self.file_metric = tk.StringVar(value="0 / 0")
        self.reuse_metric = tk.StringVar(value="0")
        self.bytes_metric = tk.StringVar(value="0.0 MiB")
        self.show_logs = False

        configure_style(root)
        container = ttk.Frame(root, padding=(14, 12))
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(1, weight=1)
        header = ttk.Frame(container)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        mark = tk.Canvas(header, width=38, height=42, background="#eef3f0", highlightthickness=0)
        mark.pack(side="left", padx=(0, 14))
        mark.create_polygon(27, 4, 51, 17, 27, 30, 3, 17, fill="#50b878", outline="")
        mark.create_polygon(3, 17, 27, 30, 27, 55, 3, 42, fill="#168252", outline="")
        mark.create_polygon(27, 30, 51, 17, 51, 42, 27, 55, fill="#105d3e", outline="")
        mark.scale("all", 0, 0, 0.7, 0.7)
        brand = ttk.Frame(header)
        brand.pack(side="left")
        ttk.Label(brand, text="Minecraft 原版下载器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(brand, text="选择版本 · 增量下载 · 导出实例", style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Label(header, text="Windows x64 / 1.2.1", style="Badge.TLabel").pack(side="right")

        viewport = ttk.Frame(container)
        viewport.grid(row=1, column=0, sticky="nsew")
        viewport.columnconfigure(0, weight=1)
        viewport.rowconfigure(0, weight=1)
        self.content_canvas = tk.Canvas(viewport, background="#eef3f0", highlightthickness=0, height=260)
        self.content_canvas.grid(row=0, column=0, sticky="nsew")
        content_scroll = ttk.Scrollbar(viewport, orient="vertical", command=self.content_canvas.yview)
        content_scroll.grid(row=0, column=1, sticky="ns")
        self.content_canvas.configure(yscrollcommand=content_scroll.set)
        columns = ttk.Frame(self.content_canvas)
        self.columns = columns
        self.content_window = self.content_canvas.create_window(0, 0, window=columns, anchor="nw")
        self.content_canvas.bind("<Configure>", self.resize_content)
        columns.bind("<Configure>", lambda _: self.content_canvas.configure(scrollregion=self.content_canvas.bbox("all")))
        root.bind("<MouseWheel>", self.scroll_content, add="+")
        self.narrow_layout = None
        columns.columnconfigure(0, weight=1, uniform="cards")
        columns.columnconfigure(1, weight=1, uniform="cards")
        columns.rowconfigure(0, weight=1)
        versions_card = ttk.Frame(columns, style="Card.TFrame", padding=12)
        self.versions_card = versions_card
        versions_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        versions_card.columnconfigure(0, weight=1)
        versions_card.rowconfigure(3, weight=1)
        version_header = ttk.Frame(versions_card, style="Card.TFrame")
        version_header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(version_header, text="选择游戏版本", style="Heading.TLabel").pack(side="left")
        self.refresh_button = ttk.Button(version_header, text="刷新列表", command=self.refresh)
        self.refresh_button.pack(side="right")
        ttk.Entry(versions_card, textvariable=self.search).grid(row=1, column=0, sticky="ew")
        filters = ttk.Frame(versions_card, style="Card.TFrame")
        filters.grid(row=2, column=0, sticky="ew", pady=(7, 9))
        ttk.Checkbutton(filters, text="快照 / 预发布", variable=self.snapshots, command=self.filter_versions).pack(side="left")
        ttk.Checkbutton(filters, text="远古版本", variable=self.old, command=self.filter_versions).pack(side="left", padx=(14, 0))
        ttk.Label(filters, textvariable=self.version_count, style="Muted.TLabel").pack(side="right")
        self.search.trace_add("write", lambda *_: self.filter_versions())
        listing = ttk.Frame(versions_card, style="Card.TFrame")
        listing.grid(row=3, column=0, sticky="nsew")
        self.tree = ttk.Treeview(listing, style="Versions.Treeview", columns=("version", "type", "date"), show="headings", selectmode="browse", height=5)
        for name, label, width in (("version", "版本", 150), ("type", "类型", 120), ("date", "发布日期", 110)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=80, anchor="w")
        self.tree.tag_configure("alternate", background="#f6f9f7")
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_selection)
        self.empty_label = ttk.Label(listing, text="加载版本列表后，在这里选择版本", style="Muted.TLabel")
        self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        ttk.Label(versions_card, textvariable=self.selection, style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(10, 0))

        settings = ttk.Frame(columns, style="Card.TFrame", padding=12)
        self.settings_card = settings
        settings.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        settings.columnconfigure(0, weight=1)
        ttk.Label(settings, text="下载设置", style="Heading.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 16))
        ttk.Label(settings, text="保存到", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 6))
        folder_row = ttk.Frame(settings, style="Card.TFrame")
        folder_row.grid(row=2, column=0, sticky="ew")
        folder_row.columnconfigure(0, weight=1)
        self.directory_entry = ttk.Entry(folder_row, textvariable=self.output, width=26)
        self.directory_entry.grid(row=0, column=0, sticky="ew")
        self.browse_button = ttk.Button(folder_row, text="浏览…", command=self.browse)
        self.browse_button.grid(row=0, column=1, padx=(7, 0))
        ttk.Label(settings, textvariable=self.destination_mode, style="Mode.TLabel").grid(row=3, column=0, sticky="ew", pady=(9, 5))
        self.preview_label = ttk.Label(settings, textvariable=self.destination_preview, style="Muted.TLabel", wraplength=420, justify="left")
        self.preview_label.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        self.preview_label.bind("<Configure>", lambda e: self.preview_label.configure(wraplength=max(200, e.width)))
        ttk.Label(settings, text="网络连接", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=(0, 6))
        proxy = ttk.Frame(settings, style="Card.TFrame")
        proxy.grid(row=6, column=0, sticky="ew")
        self.proxy_box = ttk.Combobox(proxy, textvariable=self.proxy_mode, values=("系统代理", "直连", "自定义 HTTP 代理"), state="readonly", width=18)
        self.proxy_box.pack(side="left")
        ttk.Label(proxy, text="并发", style="Muted.TLabel").pack(side="left", padx=(12, 6))
        self.worker_spin = ttk.Spinbox(proxy, from_=1, to=16, textvariable=self.workers, width=3, state="readonly")
        self.worker_spin.pack(side="left")
        self.proxy_entry = ttk.Entry(settings, textvariable=self.proxy_url)
        self.proxy_entry.grid(row=7, column=0, sticky="ew", pady=(7, 12))
        self.proxy_box.bind("<<ComboboxSelected>>", lambda *_: self.sync_proxy())
        self.sync_proxy()
        self.pack_button = ttk.Checkbutton(settings, text="完成后导出 MultiMC 实例 ZIP", variable=self.pack)
        self.pack_button.grid(row=8, column=0, sticky="w")
        ttk.Label(settings, text="导出包可导入启动器；导入时可能仍需联网。\nJava 运行时由启动器选择或安装。", style="Muted.TLabel", justify="left").grid(row=9, column=0, sticky="w", pady=(6, 0))

        transfer = ttk.Frame(container, style="Card.TFrame", padding=10)
        self.transfer = transfer
        transfer.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        transfer.columnconfigure(0, weight=1)
        task_header = ttk.Frame(transfer, style="Card.TFrame")
        task_header.grid(row=0, column=0, sticky="ew")
        ttk.Label(task_header, text="下载任务", style="Heading.TLabel").pack(side="left")
        ttk.Label(task_header, textvariable=self.phase, style="Badge.TLabel").pack(side="left", padx=12)
        ttk.Label(task_header, textvariable=self.percent, style="Heading.TLabel").pack(side="right")
        metrics = ttk.Frame(transfer, style="Card.TFrame")
        self.metrics = metrics
        metrics.grid(row=1, column=0, sticky="ew", pady=(5, 5))
        for index, (label, value) in enumerate((("已完成文件", self.file_metric), ("已校验并复用", self.reuse_metric), ("本次下载", self.bytes_metric))):
            metrics.columnconfigure(index, weight=1, uniform="metrics")
            metric = ttk.Frame(metrics, style="Card.TFrame")
            metric.grid(row=0, column=index, sticky="w")
            ttk.Label(metric, textvariable=value, style="Metric.TLabel").pack(anchor="w")
            ttk.Label(metric, text=label, style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
        self.progress = ttk.Progressbar(transfer, style="Transfer.Horizontal.TProgressbar", mode="determinate")
        self.progress.grid(row=2, column=0, sticky="ew")
        # A long file name must never grow this fixed operation panel vertically.
        self.status_label = ttk.Label(transfer, textvariable=self.status, style="Muted.TLabel", width=1, anchor="w")
        self.status_label.grid(row=3, column=0, sticky="ew", pady=(5, 4))
        self.log = tk.Text(transfer, height=2, wrap="word", font=("Microsoft YaHei UI", 9), state="disabled", background="#f3f7f4", foreground="#52685b", relief="flat", padx=8, pady=4)
        self.log.grid(row=1, column=0, sticky="ew", pady=(5, 5))
        self.log.grid_remove()
        actions = ttk.Frame(transfer, style="Card.TFrame")
        self.actions = actions
        actions.grid(row=5, column=0, sticky="ew", pady=(5, 0))
        self.log_button = ttk.Button(actions, text="展开日志", command=self.toggle_logs)
        self.log_button.pack(side="left")
        self.open_button = ttk.Button(actions, text="打开游戏目录", command=self.open_output)
        self.open_button.pack(side="left", padx=8)
        self.start_button = ttk.Button(actions, text="开始下载", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="right")
        self.cancel_button = ttk.Button(actions, text="取消", style="Cancel.TButton", command=self.stop, state="disabled")
        self.cancel_button.pack(side="right", padx=8)
        self.output.trace_add("write", lambda *_: self.update_destination())
        self.update_destination()
        self.write_log("首次加载及下载需要联网。文件经校验后可复用；本工具不包含 Java 运行时。")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Destroy>", self.cleanup_timers, add="+")
        self.poll_timer = root.after(100, self.poll)
        self.refresh_timer = None
        if auto_refresh:
            self.refresh_timer = root.after(150, self.refresh)

    def cleanup_timers(self, event):
        if event.widget is self.root:
            for timer in (self.poll_timer, self.refresh_timer):
                if timer is not None:
                    try:
                        self.root.after_cancel(timer)
                    except tk.TclError:
                        pass

    def resize_content(self, event):
        """Only the content viewport scrolls; task controls stay fixed below it."""
        self.content_canvas.itemconfigure(self.content_window, width=event.width)
        narrow = event.width < 760
        if narrow != self.narrow_layout:
            self.narrow_layout = narrow
            if narrow:
                self.columns.columnconfigure(1, weight=0, uniform="")
                self.columns.columnconfigure(0, weight=1, uniform="")
                self.versions_card.grid_configure(row=0, column=0, padx=0, sticky="ew")
                self.settings_card.grid_configure(row=1, column=0, padx=0, pady=(10, 0), sticky="ew")
            else:
                self.columns.columnconfigure(0, weight=1, uniform="cards")
                self.columns.columnconfigure(1, weight=1, uniform="cards")
                self.versions_card.grid_configure(row=0, column=0, padx=(0, 6), sticky="nsew")
                self.settings_card.grid_configure(row=0, column=1, padx=(6, 0), pady=0, sticky="nsew")
        self.content_canvas.configure(scrollregion=self.content_canvas.bbox("all"))

    def scroll_content(self, event):
        if isinstance(event.widget, (ttk.Treeview, tk.Text, ttk.Combobox, ttk.Spinbox)):
            return
        widget = event.widget
        while widget is not None:
            if widget is self.content_canvas:
                self.content_canvas.yview_scroll(-int(event.delta / 120), "units")
                return "break"
            widget = getattr(widget, "master", None)

    def toggle_logs(self):
        self.show_logs = not self.show_logs
        if self.show_logs:
            self.metrics.grid_remove()
            self.log.grid()
        else:
            self.log.grid_remove()
            self.metrics.grid()
        self.log_button.configure(text="收起日志" if self.show_logs else "展开日志")

    def update_destination(self):
        selected = self.tree.selection()
        version = self.visible[int(selected[0])]["id"] if selected else "所选版本"
        text = self.output.get().strip()
        if not text:
            self.destination_mode.set("请选择保存位置")
            self.destination_preview.set("选择 .minecraft 可直接增量下载到已有游戏目录。")
            return
        try:
            layout = install_layout(Path(text), version)
            self.destination_mode.set("增量模式 · 直接写入 .minecraft" if layout.incremental else "独立实例 · 自动创建游戏目录")
            export = "ZIP：.minecraft / downloader-exports" if layout.incremental else "ZIP：保存在所选文件夹中"
            self.destination_preview.set(f"游戏目录：{layout.game_root}\n{export}")
        except (OSError, ValueError) as error:
            self.destination_mode.set("请检查目录路径")
            self.destination_preview.set(str(error))
        if not self.busy:
            self.last_output = None

    def sync_proxy(self):
        enabled = not self.busy and self.proxy_mode.get() == "自定义 HTTP 代理"
        self.proxy_entry.configure(state="normal" if enabled else "disabled")

    def network(self):
        value = {"系统代理": "system", "直连": "direct"}.get(self.proxy_mode.get(), self.proxy_url.get().strip())
        return Network(value, log=lambda message: self.events.put(("log", message)))

    def write_log(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", f"[{datetime.now():%H:%M:%S}] {message}\n")
        if int(self.log.index("end-1c").split(".")[0]) > 300:
            self.log.delete("1.0", "100.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_busy(self, busy):
        self.busy = busy
        for widget in (self.refresh_button, self.start_button, self.browse_button,
                       self.directory_entry, self.pack_button):
            widget.configure(state="disabled" if busy else "normal")
        self.proxy_box.configure(state="disabled" if busy else "readonly")
        self.worker_spin.configure(state="disabled" if busy else "readonly")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.sync_proxy()

    def launch(self, function):
        self.cancel = threading.Event()
        self.set_busy(True)

        def run():
            try:
                function()
            except Cancelled as error:
                self.events.put(("cancelled", str(error)))
            except Exception as error:
                self.events.put(("error", str(error)))
            finally:
                self.events.put(("idle", None))

        threading.Thread(target=run, daemon=True).start()

    def refresh(self):
        if self.busy:
            return
        try:
            network = self.network()
        except ValueError as error:
            messagebox.showerror("代理配置错误", str(error))
            return
        self.status.set("正在获取官方版本列表…")
        self.phase.set("获取版本")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.launch(lambda: self.events.put(("versions", list_versions(network, self.cancel))))

    def filter_versions(self):
        selected = self.tree.selection()
        previous = self.visible[int(selected[0])]["id"] if selected and int(selected[0]) < len(self.visible) else None
        search = self.search.get().strip().lower()
        self.visible = [v for v in self.versions if search in v["id"].lower()
                        and (v["type"] == "release" or v["type"] == "snapshot" and self.snapshots.get()
                             or v["type"] in ("old_alpha", "old_beta") and self.old.get())]
        self.tree.delete(*self.tree.get_children())
        types = {"release": "正式版", "snapshot": "快照 / 预发布", "old_alpha": "远古 Alpha", "old_beta": "远古 Beta"}
        selected_id = None
        for index, version in enumerate(self.visible):
            self.tree.insert("", "end", iid=str(index), values=(version["id"], types.get(version["type"], version["type"]), version.get("releaseTime", "")[:10]), tags=("alternate",) if index % 2 else ())
            if version["id"] == previous:
                selected_id = str(index)
        if self.visible:
            self.empty_label.place_forget()
            self.tree.selection_set(selected_id or "0")
        else:
            self.selection.set("没有匹配的版本")
            self.empty_label.configure(text="没有匹配的版本，试试其他关键词")
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        self.version_count.set(f"{len(self.visible):,} 个版本")
        self.update_destination()

    def on_selection(self, *_):
        selected = self.tree.selection()
        if selected:
            self.selection.set(f"已选择：Minecraft {self.visible[int(selected[0])]['id']}  ·  Windows x64")
        self.update_destination()

    def browse(self):
        folder = filedialog.askdirectory(initialdir=self.output.get(), title="选择下载保存目录")
        if folder:
            self.output.set(folder)

    def start(self):
        if self.busy:
            return
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("选择版本", "请先从列表中选择版本。")
            return
        try:
            network = self.network()
            if not self.output.get().strip():
                raise ValueError("请选择保存目录")
            # Keep the selected basename until layout detection, including symlinks.
            output = Path(self.output.get()).expanduser().absolute()
            workers = self.workers.get()
            if not 1 <= workers <= 16:
                raise ValueError("并发数必须在 1 到 16 之间")
        except (ValueError, tk.TclError) as error:
            messagebox.showerror("配置错误", str(error))
            return
        entry = dict(self.visible[int(selected[0])])
        pack = self.pack.get()
        self.operation_output = output
        self.last_output = None
        self.file_metric.set("0 / 0")
        self.reuse_metric.set("0")
        self.bytes_metric.set("0.0 MiB")
        self.percent.set("0%")
        self.phase.set("解析清单")
        self.status.set(f"正在解析 Minecraft {entry['id']} 的文件清单…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.write_log(f"准备下载 Minecraft {entry['id']}。")

        def work():
            emit = lambda kind, value: self.events.put((kind, value))
            plan = make_plan(entry, network, self.cancel)
            java = plan.manifest.get("javaVersion", {}).get("majorVersion", "由启动器选择")
            emit("log", f"共 {len(plan.files):,} 个下载文件，约 {readable_size(plan.total_bytes)}；游戏需要 Java {java}。")
            result = install(plan, output, network, self.cancel, emit, workers, pack)
            emit("done", result)

        self.launch(work)

    def stop(self):
        self.cancel.set()
        self.phase.set("正在取消")
        self.cancel_button.configure(state="disabled")
        self.status.set("正在取消，等待当前连接结束（通常最多约 30 秒）…")

    def open_output(self):
        selected = self.tree.selection()
        version = self.visible[int(selected[0])]["id"] if selected else "所选版本"
        output = self.operation_output if self.busy and self.operation_output else Path(self.output.get())
        try:
            layout = install_layout(output, version)
            path = self.last_output or (layout.game_root if layout.game_root.exists() else output)
        except (OSError, ValueError) as error:
            messagebox.showerror("目录无效", str(error))
            return
        if path.exists():
            os.startfile(path)
        else:
            messagebox.showinfo("目录尚未创建", "开始下载后会自动创建保存目录。")

    def poll(self):
        if self.poll_timer is not None:
            self.root.after_cancel(self.poll_timer)
            self.poll_timer = None
        # Coalesce fast small-file completions to keep the UI responsive.
        progress = None
        for _ in range(500):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                progress = value
                continue
            if progress is not None:
                self.show_progress(progress)
                progress = None
            if kind == "versions":
                self.versions = value
                self.filter_versions()
                self.status.set(f"已获取 {len(value):,} 个版本，默认显示正式版。")
                self.write_log("官方版本列表加载成功。")
                self.phase.set("就绪")
            elif kind == "log":
                self.write_log(value)
            elif kind == "total":
                self.progress.stop()
                self.progress.configure(mode="determinate", maximum=max(value[0], 1), value=0)
                self.phase.set("校验与下载")
                self.file_metric.set(f"0 / {value[0]:,}")
                self.percent.set("0%")
            elif kind == "layout":
                self.last_output = value[0]
            elif kind == "packing":
                self.progress.configure(mode="determinate", maximum=max(value[1], 1), value=value[0])
                self.status.set(f"正在打包并生成校验清单：{value[0]:,} / {value[1]:,}")
                self.phase.set("导出实例包")
                self.percent.set(f"{value[0] / max(value[1], 1):.0%}")
            elif kind == "done":
                folder, archive = value
                self.last_output = folder
                self.progress.configure(value=self.progress["maximum"])
                self.status.set("下载与打包完成。" if archive else "下载完成。")
                self.phase.set("已完成")
                self.percent.set("100%")
                self.write_log(f"游戏目录：{folder}")
                if archive:
                    self.write_log(f"实例包：{archive}")
                text = f"游戏目录：\n{folder}"
                if archive:
                    text += f"\n\n实例包：\n{archive}\n\n在启动器中选择导入实例 / 整合包，或解压后添加 .minecraft 游戏目录。导入时可能仍需联网。"
                messagebox.showinfo("下载完成", text)
            elif kind in ("error", "cancelled"):
                self.status.set("操作失败，可修正后重试。" if kind == "error" else "已取消。")
                self.phase.set("需要处理" if kind == "error" else "已取消")
                self.write_log(value)
                if kind == "error" and not self.show_logs:
                    self.toggle_logs()
                if kind == "error":
                    messagebox.showerror("操作失败", value)
            elif kind == "idle":
                if str(self.progress["mode"]) == "indeterminate":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                self.set_busy(False)
        if progress is not None:
            self.show_progress(progress)
        self.poll_timer = self.root.after(100, self.poll)

    def show_progress(self, value):
        completed, total, relative, reused, transferred = value
        self.progress.configure(value=completed)
        self.file_metric.set(f"{completed:,} / {total:,}")
        self.reuse_metric.set(f"{reused:,}")
        self.bytes_metric.set(readable_size(transferred))
        self.percent.set(f"{completed / max(total, 1):.0%}")
        self.status.set(f"{completed:,} / {total:,} 个文件  ·  {Path(relative).name[:64]}")

    def close(self):
        if self.busy:
            self.stop()
            self.write_log("取消完成后即可关闭窗口；已校验文件会保留。")
            return
        self.root.destroy()


if __name__ == "__main__":
    Application(tk.Tk()).root.mainloop()
