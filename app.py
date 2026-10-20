"""Tk desktop UI for the vanilla downloader; no third-party packages required."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from downloader import Cancelled, Network, install, list_versions, make_plan


def readable_size(size: int) -> str:
    return f"{size / (1024 ** 2):,.1f} MiB"


class Application:
    def __init__(self, root: tk.Tk, auto_refresh: bool = True):
        self.root = root
        root.title("Minecraft 原版下载器 1.1")
        root.geometry("980x760")
        root.minsize(820, 650)
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

        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Sub.TLabel", foreground="#526174")
        style.configure("TButton", padding=(10, 5))
        container = ttk.Frame(root, padding=22)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text="Minecraft 原版下载器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(container, text="选择版本 · 下载并校验 · 导出可导入的实例包", style="Sub.TLabel").pack(anchor="w", pady=(4, 15))

        filters = ttk.Frame(container)
        filters.pack(fill="x", pady=(0, 8))
        ttk.Label(filters, text="搜索版本").pack(side="left")
        ttk.Entry(filters, textvariable=self.search, width=24).pack(side="left", padx=8)
        ttk.Checkbutton(filters, text="显示快照", variable=self.snapshots, command=self.filter_versions).pack(side="left", padx=8)
        ttk.Checkbutton(filters, text="显示远古版本", variable=self.old, command=self.filter_versions).pack(side="left", padx=8)
        self.refresh_button = ttk.Button(filters, text="刷新列表", command=self.refresh)
        self.refresh_button.pack(side="right")
        self.search.trace_add("write", lambda *_: self.filter_versions())

        listing = ttk.Frame(container)
        listing.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(listing, columns=("version", "type", "date"), show="headings", selectmode="browse", height=10)
        for name, label, width in (("version", "版本", 250), ("type", "类型", 160), ("date", "发布日期", 180)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, anchor="w")
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_selection)
        ttk.Label(container, textvariable=self.selection, style="Sub.TLabel").pack(anchor="w", pady=(7, 12))

        settings = ttk.LabelFrame(container, text="下载与导出", padding=12)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="保存目录").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.directory_entry = ttk.Entry(settings, textvariable=self.output)
        self.directory_entry.grid(row=0, column=1, sticky="ew")
        self.browse_button = ttk.Button(settings, text="选择目录", command=self.browse)
        self.browse_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(settings, text="网络代理").grid(row=1, column=0, sticky="w", pady=8)
        proxy = ttk.Frame(settings)
        proxy.grid(row=1, column=1, columnspan=2, sticky="ew")
        self.proxy_box = ttk.Combobox(proxy, textvariable=self.proxy_mode, values=("系统代理", "直连", "自定义 HTTP 代理"), state="readonly", width=18)
        self.proxy_box.pack(side="left")
        self.proxy_entry = ttk.Entry(proxy, textvariable=self.proxy_url, width=34)
        self.proxy_entry.pack(side="left", padx=8, fill="x", expand=True)
        self.proxy_box.bind("<<ComboboxSelected>>", lambda *_: self.sync_proxy())
        self.sync_proxy()
        options = ttk.Frame(settings)
        options.grid(row=2, column=0, columnspan=3, sticky="w")
        self.pack_button = ttk.Checkbutton(options, text="下载完成后生成 MultiMC 实例 ZIP", variable=self.pack)
        self.pack_button.pack(side="left")
        ttk.Label(options, text="并发数").pack(side="left", padx=(18, 5))
        self.worker_spin = ttk.Spinbox(options, from_=1, to=16, textvariable=self.workers, width=4, state="readonly")
        self.worker_spin.pack(side="left")
        ttk.Label(settings, text="目标：Windows x64 原版。包内包含完整游戏目录；启动器导入时仍可能联网补齐依赖。", style="Sub.TLabel", wraplength=830).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        actions = ttk.Frame(container)
        actions.pack(fill="x", pady=12)
        self.start_button = ttk.Button(actions, text="下载所选版本", command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="取消", command=self.stop, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        self.open_button = ttk.Button(actions, text="打开保存目录", command=self.open_output)
        self.open_button.pack(side="right")
        self.progress = ttk.Progressbar(container, mode="determinate")
        self.progress.pack(fill="x")
        ttk.Label(container, textvariable=self.status, wraplength=880).pack(anchor="w", pady=(5, 8))
        self.log = tk.Text(container, height=5, wrap="word", font=("Microsoft YaHei UI", 9), state="disabled", background="#f4f6f8", relief="flat", padx=8, pady=6)
        self.log.pack(fill="x")
        self.write_log("首次加载及下载需要联网。文件经校验后可复用；本工具不包含 Java 运行时。")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        if auto_refresh:
            root.after(150, self.refresh)

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
            self.tree.insert("", "end", iid=str(index), values=(version["id"], types.get(version["type"], version["type"]), version.get("releaseTime", "")[:10]))
            if version["id"] == previous:
                selected_id = str(index)
        if self.visible:
            self.tree.selection_set(selected_id or "0")
        else:
            self.selection.set("没有匹配的版本")

    def on_selection(self, *_):
        selected = self.tree.selection()
        if selected:
            self.selection.set(f"已选择：Minecraft {self.visible[int(selected[0])]['id']}  ·  Windows x64")

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
            output = Path(self.output.get()).expanduser().resolve()
            workers = self.workers.get()
            if not 1 <= workers <= 16:
                raise ValueError("并发数必须在 1 到 16 之间")
        except (ValueError, tk.TclError) as error:
            messagebox.showerror("配置错误", str(error))
            return
        entry = dict(self.visible[int(selected[0])])
        pack = self.pack.get()
        self.operation_output = output
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
        self.cancel_button.configure(state="disabled")
        self.status.set("正在取消，等待当前连接结束（通常最多约 30 秒）…")

    def open_output(self):
        path = self.last_output or self.operation_output or Path(self.output.get())
        if path.exists():
            os.startfile(path)
        else:
            messagebox.showinfo("目录尚未创建", "开始下载后会自动创建保存目录。")

    def poll(self):
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
            elif kind == "log":
                self.write_log(value)
            elif kind == "total":
                self.progress.stop()
                self.progress.configure(mode="determinate", maximum=max(value[0], 1), value=0)
            elif kind == "packing":
                self.progress.configure(mode="determinate", maximum=max(value[1], 1), value=value[0])
                self.status.set(f"正在打包并生成校验清单：{value[0]:,} / {value[1]:,}")
            elif kind == "done":
                folder, archive = value
                self.last_output = archive.parent if archive else folder.parent.parent
                self.progress.configure(value=self.progress["maximum"])
                self.status.set("下载与打包完成。" if archive else "下载完成。")
                self.write_log(f"游戏目录：{folder}")
                if archive:
                    self.write_log(f"实例包：{archive}")
                text = f"游戏目录：\n{folder}"
                if archive:
                    text += f"\n\n实例包：\n{archive}\n\n在启动器中选择导入实例 / 整合包，或解压后添加 .minecraft 游戏目录。导入时可能仍需联网。"
                messagebox.showinfo("下载完成", text)
            elif kind in ("error", "cancelled"):
                self.status.set("操作失败，可修正后重试。" if kind == "error" else "已取消。")
                self.write_log(value)
                if kind == "error":
                    messagebox.showerror("操作失败", value)
            elif kind == "idle":
                if str(self.progress["mode"]) == "indeterminate":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                self.set_busy(False)
        if progress is not None:
            self.show_progress(progress)
        self.root.after(100, self.poll)

    def show_progress(self, value):
        completed, total, relative, reused, transferred = value
        self.progress.configure(value=completed)
        self.status.set(f"{completed:,} / {total:,} 个文件 · 已复用 {reused:,} · 本次下载 {readable_size(transferred)}\n{relative}")

    def close(self):
        if self.busy:
            self.stop()
            self.write_log("取消完成后即可关闭窗口；已校验文件会保留。")
            return
        self.root.destroy()


if __name__ == "__main__":
    Application(tk.Tk()).root.mainloop()
