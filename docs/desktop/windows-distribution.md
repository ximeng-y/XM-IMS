# XM-IMS Windows 桌面发行版

本文说明 Windows 安装包的构建、布局与发布约定。源码模式（`start.bat` / `launcher.py`）的用法不受影响。

## 构建

```powershell
& 'E:\others\Projects\XM-IMS\.venv\Scripts\python.exe' -B 'E:\others\Projects\XM-IMS\desktop\build.py'
```

- 构建解释器必须是 Python 3.12 x64（与嵌入式运行时一致）。
- 固定输入在 `desktop/runtime-lock.json`：Electron 45.3.0、Python 3.12.10 嵌入包、NSIS 3.13。
- 所有下载、解压、暂存与输出都在 `.XMTEMP\electron-windows\`，不安装全局 Node / NSIS / Python。
- 网络不可用时，把三个官方包放进 `.XMTEMP\electron-windows\cache\`（文件名见 `runtime-lock.json` 的 URL）后重跑。
- 输出：`.XMTEMP\electron-windows\dist\XM-IMS-<版本>-win-x64-Setup.exe`，版本取自 `app/version.py` 的 `APP_VERSION`。
- 已有 NSIS 时可用 `--nsis <makensis.exe 所在目录>` 复用，脚本不会扫描磁盘寻找替代品。

## 安装布局

```
<安装目录>\
  xm-ims.exe、xm-ims_wer.dll、Electron 官方运行文件、locales\
  resources\
    app\          Electron 主进程、preload 与桌面模块
    backend\      Python 应用代码与 backend_runner.py
    python\       嵌入式 Python 与 Lib\site-packages
    icons\        icon.png、icon.ico
    licenses\     Electron / Chromium / Python 随包许可
  installed-program-files.txt   安装器管理的程序文件清单
  uninstall.exe
  data\          业务数据（安装器不携带、不覆盖、不删除）
  tools\         局部安装的 Git / 登录组件（运行后由引导准备）
  .XMTEMP\       工具与更新临时目录
```

- 默认安装目录 `%LOCALAPPDATA%\Programs\XM-IMS`，可在安装向导中选择其他可写目录。
- 安装与卸载只处理程序文件清单登记的项目；`data`、`tools` 与用户文件保留。
- 安装器拒绝 32 位 Windows；不写系统 PATH，不设开机启动。

## 运行

- 安装后从开始菜单或桌面快捷方式启动 `xm-ims.exe`，不出现控制台，不自动打开浏览器。
- 窗口关闭后隐藏到托盘，服务继续运行；托盘「退出」才停止服务。重复启动只聚焦已有窗口。
- 两个内部服务只监听 `127.0.0.1`，端口被占用时按既有规则向后顺延。

## 依赖工具（Git / 登录组件）

- 桌面模式默认把 Git 装到软件目录的 `tools\git`（PortableGit 自解压，不写系统 PATH / 注册表），登录组件缺失时解压到 `tools\gcm`。
- 软件目录路径含中文时，安装前提示选择不含中文的文件夹，或安装到系统位置；选择其他文件夹使用原生文件夹窗口，成功后自动记住位置。
- 系统安装、用户自选位置与默认局部位置都记在 `data\desktop\tools.json`，备份、同步与登录统一使用该位置。
- GitHub 授权仍由系统浏览器完成，凭据保存在 Windows 凭据管理器中。

## 桌面版更新

- 检查对象固定为仓库 `ximeng-y/XM-IMS` 的最新正式 Release，匿名读取，不携带备份账号凭据。
- 版本标签使用 `vX.Y.Z` / `X.Y.Z`，按数值比较；正式发布草稿与预发布不作为更新来源。
- 安装资产名固定为 `XM-IMS-<X.Y.Z>-win-x64-Setup.exe`。
- 下载后使用 Release asset 的 `sha256` digest 校验，不一致不进入安装阶段。
- 用户确认安装后：读取本机同步状态，备份或同步进行中先提示稍后再试；随后正常停止服务、启动安装程序并在原安装目录升级。
- 更新安装完成后重新启动 XM-IMS；不会打开浏览器，也不会改动 `data`。

界面状态与提示：

| 情况 | 展示 |
|---|---|
| 暂无正式 Release | 暂无已发布版本 |
| 正式 Release 缺少安装包 | 该版本暂未提供 Windows 安装包 |
| 网络不可用 | 暂时无法获取版本信息 |
| 已是最新 | 已是最新 |

关闭「启动时自动检查新版本」后，启动不再自动查询；手动「检查更新」始终可用。

## 发布约定

1. 修改 `app/version.py` 的 `APP_VERSION`，重新构建安装包。
2. 在 GitHub 仓库创建 tag（如 `v0.1.1`）的正式 Release。
3. 上传 `XM-IMS-0.1.1-win-x64-Setup.exe`，assets 中的 digest 由 GitHub 生成，无需人工另附校验文件。
4. 已安装的客户机会在启动或手动检查时发现新版本。

安装版更新与源码模式的 `/api/v1/update/*` 是两条独立通道：源码模式继续按 Git 标签更新检出目录，安装版按 Release 安装包升级，二者不会互相替代。

首版不提供网页版启动入口、内网访问开关或开机启动项。
