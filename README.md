# Minecraft 原版下载器 1.1

独立桌面工具，面向 **Windows x64**，仅依赖 Python 3.10+ 标准库和 Tk。
不修改 HMCL，也不需要先编译 HMCL。

## 使用

1. 双击 `start.cmd`，或执行 `python app.py`。
2. 等待官方版本列表加载，选择版本；可搜索、显示快照或远古版本。
3. 选择保存目录、代理模式和并发数。
4. 保持勾选“下载完成后生成 MultiMC 实例 ZIP”，点击下载。
5. 完成后点击“打开保存目录”。

默认使用系统代理；自定义代理接受 HTTP/HTTPS 代理 URL，不支持 SOCKS URL。
“直连”仅禁用应用层代理，不能绕过系统的 TUN、VPN 或路由。
下载失败后可重试：已有且 SHA-1 正确的文件会复用，未完成的单文件会重新下载。
取消会停止安排新任务，当前连接可能需要等待约 30 秒超时。
同一版本的输出目录有进程锁，避免两个窗口同时写入。

如果 Mojang 清单连接出现 SSL EOF，1.1 会在 `piston-meta.mojang.com` 和
`launchermeta.mojang.com` 两个官方域名之间切换，并显示重试日志；清单哈希校验和
TLS 证书验证始终保留。系统代理模式重试时重新读取代理设置。若仍然失败，可在界面选择
“自定义 HTTP 代理”，填写本机代理的实际地址后重试。更新后需要关闭旧窗口、重新启动。

## 输出和导入

```text
downloads/
  Minecraft-<版本>/.minecraft/     # 传统游戏目录布局
  Minecraft-<版本>-Windows-x64.zip
```

ZIP 包含 `instance.cfg`、`mmc-pack.json`、完整 `.minecraft` 目录，以及文件校验清单
`download-manifest.json`。只有本次清单列出的游戏文件会入包，不会扫描和打包账户、令牌、
已有存档或其他文件。

- **直接导入**：在 HMCL、Prism Launcher 或 MultiMC 中选择“导入实例/整合包”，选中 ZIP。
  使用标准 MultiMC 实例描述，只声明 `net.minecraft` 原版组件。
  **各启动器的全局缓存和导入规则不同，可能重新下载元数据、客户端、库或资源；不保证离线导入。**
  该格式已对照当前 HMCL 导入源码，尚未完成启动器实际导入和游戏启动测试。
- **完整目录迁移**：解压 ZIP，将 `.minecraft` 添加到支持传统目录布局的启动器的游戏目录列表。
  不要直接覆盖已有游戏目录；某些新版启动器会迁移旧目录结构。
- 本包只面向 Windows x64。不会下载或捆绑 Java 运行时、账号信息或启动器。
  所需 Java 主版本显示在日志和 ZIP 的说明文件中，由启动器选择或安装。
- 游戏登录与所有权验证由启动器处理。本工具只负责文件下载。

## 下载内容

从 Mojang 官方版本清单获取版本 JSON，下载客户端、按当前 Windows 环境规则筛选的库和
原生库、资源索引及资源对象、日志配置。兼容旧资源的 virtual/map_to_resources 目录。
有 SHA-1 或大小的文件按元数据校验；校验完成后原子替换目标文件。
旧版没有哈希的依赖每次重新下载。旧的可变资源索引使用与 HMCL 相同的哈希校验条件。
缺少资源索引或平台库元数据的旧版本会明确报错，不会声称完整下载成功。
原生库保留为原始 JAR，由启动器在启动时解压。

## 验证

```powershell
python -m unittest discover -s tests -v
```

测试使用本地 HTTP 服务验证下载、校验、失败重试、缓存复用、取消、路径保护和 ZIP 内容，
不依赖外部服务或下载大型游戏。UI 可用以下命令进行构建冒烟验证：

```powershell
python -c "import tkinter as t; from app import Application; r=t.Tk(); a=Application(r, auto_refresh=False); r.update(); r.destroy(); print('UI OK')"
```

本次验证：16 项本地测试通过（包括 SSL EOF 后切换官方域名、备用响应哈希校验和取消重试）；
官方版本清单读取成功；Minecraft 1.21.1 的库、资源对象、
日志配置三个真实样本下载并通过 SHA-1 校验。没有执行完整游戏的大容量下载，
也没有在启动器中实际导入并启动游戏。
