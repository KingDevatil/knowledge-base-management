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

所有仓库内辅助脚本都以脚本自身所在目录为基准定位上一级项目根目录，不依赖调用时的当前工作目录。移动整个项目目录后，仓库内脚本仍可直接使用；全局 `knowbase` 入口保存了注册时的绝对路径，因此移动后需重新执行 `cli-install` 更新绑定。如果单独复制某个辅助脚本，则必须保留“`scripts` 位于项目根目录下”这一相对结构。
