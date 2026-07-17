# 脚本目录

根目录保留面向首次使用者的一键部署入口：

- `start.ps1`、`start.sh`：Docker 部署、配置、启停和状态查询。
- `start-docker.bat`：Windows Docker 双击入口。
- `start-dev.ps1`、`start-dev.bat`：Windows 非 Docker 原生部署入口。

本目录集中管理辅助运行和维护脚本：

| 脚本 | 用途 |
|---|---|
| `knowbase.ps1`、`knowbase.sh` | Windows/Linux 全局 CLI 的命令路由实现 |
| `install-cli.ps1`、`install-cli.sh` | 安装、检查或卸载当前用户的全局 `knowbase` 命令 |
| `knowbase.cmd`、`knowbase` | 安装到 PATH 目录中的 Windows/Linux 轻量入口模板 |
| `init-config.bat` | 打开 Docker 交互配置向导，生成或更新根目录 `.env` |
| `network-detection.ps1` / `.sh` | 为 Windows/Linux 配置向导检测计算机名和有效局域网 IPv4 |
| `access-modes.ps1` / `.sh` | 规范化组合访问方式、迁移旧配置并维护 Tunnel 开关 |
| `stop-dev.bat` | 停止本项目的 Windows 原生 Gateway、Chroma 和 MinIO 进程 |
| `start-desktop-shell.bat` | 安装缺失的桌面依赖并启动 pywebview 桌面壳 |
| `start-gui.bat` | 启动旧版 tkinter 管理器 |
| `test.ps1`、`test.sh` | 运行标准测试和依赖声明一致性检查 |
| `check_deps_sync.py` | 单独检查运行与开发依赖声明是否同步 |

Windows 示例：

```powershell
.\scripts\init-config.bat
.\scripts\stop-dev.bat
.\scripts\start-desktop-shell.bat
```

全局命令建议通过根目录部署入口管理，而不是直接调用安装器：

```powershell
.\start.ps1 cli-install
knowbase health
knowbase gateway restart
knowbase cli uninstall
```

```bash
sh ./start.sh cli-install
knowbase health
knowbase gateway restart
knowbase cli uninstall
```

Windows 默认将入口安装到已在用户 PATH 中的 `%LOCALAPPDATA%\Microsoft\WindowsApps`，绑定配置保存在 `%LOCALAPPDATA%\KnowledgeBaseManagement`；这能让复用旧环境的 Windows Terminal 新标签页直接发现命令。Linux 默认安装到 `~/.local/bin`，同时为 `~/.profile` 和当前 Bash/Zsh 配置增加带标记的 PATH 区块，安装后需重新打开终端。

访问配置以复选项展示“仅本机、局域网、公网、Cloudflare Tunnel”，输入逗号分隔的编号即可组合启用，例如 `2,3,4`。确认后只会进入已选方式的具体配置；公网直连域名和 Tunnel Hostname 分开保存，可并行使用。旧版单一 `DEPLOY_ACCESS_MODE` 会自动迁移，`-Tunnel` / `--tunnel` 只增删组合中的 Tunnel，不会覆盖其他入口。

局域网具体配置会自动检测计算机名及带默认路由的有效 IPv4，过滤回环地址和 `169.254.x.x` 链路本地地址。检测值以逗号列表作为默认项，直接回车即可同时启用；存在有线、Wi-Fi、VPN 等多个候选时，可删除不希望使用的地址，也可填写自定义内网域名。

所有仓库内辅助脚本都以脚本自身所在目录为基准定位上一级项目根目录，不依赖调用时的当前工作目录。移动整个项目目录后，仓库内脚本仍可直接使用；全局 `knowbase` 入口保存了注册时的绝对路径，因此移动后需重新执行 `cli-install` 更新绑定。如果单独复制某个辅助脚本，则必须保留“`scripts` 位于项目根目录下”这一相对结构。
