# LTT 工坊下载器（Left 4 Dead 2）

给 **Left 4 Dead 2（appid 550）** 下载 Steam 创意工坊物品 / 合集的小工具。

**不用开游戏、不用登录 Steam、不用 steamcmd。**

> **说明**：本项目的代码与文档由 **AI（DeepSeek Harness）** 协助生成，作者负责提出需求、真机测试与验收。
> 这只是个小工具，以 **GPL-3.0** 公开 —— 可以自由使用和修改，但**分发或衍生的版本必须同样开源**。

---

## 为什么要有这个东西

L4D2 的工坊内容必须**开着游戏**才会下载；而其他游戏（饥荒、Unturned 等）走 Steam 客户端就能下。

另外 steamcmd 在部分 Windows 机器上会因为自带的旧版引导程序不支持 HTTPS 而无法自更新
（`Can't use HTTPS because steamcommon was compiled without ENABLE_OPENSSLCONNECTION`），
导致常见的下载工具全部卡死。

本工具直接走 **Steam 公开 API** 的直链，绕开上述两个问题。

## 原理

1. 调 `ISteamRemoteStorage/GetPublishedFileDetails/v1/` 拿到物品详情，里面有 `file_url`（公开 UGC 的直链）
2. 直接用 HTTP 把 `file_url` 下下来（**8 条并行 + Range 分段**）
3. 校验落盘字节数必须等于 API 报的 `file_size`，通过才原子改名
4. 合集：`ISteamRemoteStorage/GetCollectionDetails/v1/` 取子物品，逐个下载

## 功能

- 单个物品 / **合集**（合集支持勾选、搜索、已选合计）
- **批量粘贴**：一次粘多条（换行 / 逗号 / 分号 / 中文逗号都能拆）
- **断点续传** + 分段重试（每段 3 次）
- **8 并行下载**（默认；可调 1~16）
- 可选代理（默认关，记在 `settings.ini`）
- apt 风格的进度条（百分比 / 已下 / 总量 / 速度 / 剩余时间）
- 大小校验 + 原子写：失败**绝不留半截文件**
- 失败说清原因：带 Steam API 的 `result` 码与 HTTP 状态
- 输出命名：`<清洗后的标题>_<物品ID>.vpk`（同名 `.jpg` 缩略图）
- 运行日志 `run.log`（超过 2 MB 自动轮转）

## 用法

### 方式一：exe（给没有 Python 的人）

从本仓库的 **[Releases](../../releases)** 页下载 `LTT_WorkshopDL.exe`（仓库里不放二进制文件），
双击 → 粘贴工坊链接或物品 ID → 回车。

> exe 未做代码签名。若系统提示「Windows 已保护你的电脑」，点**更多信息 → 仍要运行**即可
> （该提示由文件的"来自 Internet"标记触发，走聊天软件直接传文件通常不会出现）。

### 方式二：源码

```bat
双击 ltt_workshop_dl.bat
:: 或
python ltt_workshop_dl.py
```

命令行 / 自动化：

```bat
python ltt_workshop_dl.py --cli "<工坊链接或ID>" [--threads=8] [--proxy=http://127.0.0.1:7897] [--all] [--list]
```

## 输出

下载文件放在**工具所在目录**：

```
<清洗后的标题>_<工坊ID>.vpk
<同名>.jpg            (工坊缩略图)
run.log               运行日志
settings.ini          代理开关 / 地址 / 线程数
```

## 已知限制

- **只支持 L4D2（appid 550）**
- 有些物品 `file_url` 为空（或已被作者删除/转私密，API 返回 `result=9`）⇒ 本工具下不了；
  工具会直接打印 **steamcmd 的替代命令**（L4D2 支持匿名下载，不需要登录）：
  ```
  steamcmd +login anonymous +workshop_download_item 550 <物品ID> +quit
  ```
  下载结果在 `steamapps/workshop/content/550/<物品ID>/` 下，文件名是 `*.bin`，
  **改名成 `.vpk`** 再放进 `left4dead2/addons/`。
- 依赖 Steam 公开 API 的行为：若 Valve 改变 `file_url` 的提供方式（例如改为需要登录），本工具会失效。
- 合集是**逐个**下载，不会同时下多个物品。

## 自己打包 exe

```bat
python -m venv venv
venv\Scripts\pip install pyinstaller
venv\Scripts\pyinstaller --onefile --console --name LTT_WorkshopDL ltt_workshop_dl.py
```

## 许可

**GNU General Public License v3.0 (GPL-3.0)** —— 全文见 [LICENSE](LICENSE)。

这意味着：

- ✅ 可以自由使用、修改、分发给朋友
- ✅ 允许收费（GPL 本身不禁止卖钱）
- ❗ **但**：分发的每一份（收费或免费）都必须附带源码，且**衍生作品必须同样以 GPL 开源**
- ❗ 所以"改个名字、闭源打包拿去卖"这条路是堵死的
- ❌ 不提供任何担保

下载内容的使用请遵守 Steam 订户协议与原作者许可。本工具不包含任何第三方代码，仅使用 Python 标准库。
