# NTULearn MCP

面向南洋理工大学（NTU Singapore）学生的非官方 MCP 服务，将 NTULearn（Blackboard Learn）的课程、公告、日历、成绩和课程文件接入支持 MCP 的 AI 客户端。

本仓库基于 [kingdonkatsu/ntulearn-mcp](https://github.com/kingdonkatsu/ntulearn-mcp) 扩展，默认仅从 **Chrome** 读取登录会话，并新增**课程资料增量同步**与**作业统一汇总**，共提供 10 个工具。保留上游 MIT 许可证与版权声明。

## 能做什么

你可以向已连接的 AI 客户端提出：

- “整理我这周所有课程的新公告。”
- “列出未来两周的作业、截止时间和来源；有冲突的日期单独标注。”
- “把本学期课程资料按课程和原有文件夹同步到本地，先给我预览。”
- “读取这份作业说明，找出提交要求和评分占比。”
- “查询这门课已经公布的成绩。”

服务只读取学校端数据，不提交作业、不修改成绩，也不自动安排提醒。下载和同步工具会写入你指定的本地目录。

## 环境要求

- Python **3.12 或更高版本**。
- 已安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 Git。
- 具有 NTULearn 访问权限，并已在 Chrome 中完成 SSO / MFA 登录。
- 支持本地 `stdio` MCP 服务的客户端。

资料增量同步使用 `fcntl` 文件锁，适用于 **Linux / macOS**；当前实现不支持在原生 Windows 上执行实际同步。其他工具仍受浏览器 Cookie 读取能力和系统密钥环配置影响。

## 快速开始

### 1. 下载并安装本仓库

```bash
git clone https://github.com/Realsbt/ntulearn-mcp.git
cd ntulearn-mcp
uv sync --frozen
```

请使用本仓库源码启动，以包含新增功能；直接执行 `uvx ntulearn-mcp` 获取的是包索引上的同名发行版，不保证与本仓库一致。

### 2. 登录 NTULearn

在 **Chrome** 中打开 [NTULearn](https://ntulearn.ntu.edu.sg)，完成登录。服务会尝试自动读取本机的 `BbRouter` Cookie，通常无需手动复制。

### 3. 配置 MCP 客户端

在客户端的 MCP 配置中加入以下内容，将 `/absolute/path/to/ntulearn-mcp` 替换为克隆目录的**绝对路径**。如果客户端找不到 `uv`，也应将 `command` 改为它的绝对路径。

```json
{
  "mcpServers": {
    "ntulearn": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/ntulearn-mcp",
        "run",
        "ntulearn-mcp"
      ]
    }
  }
}
```

例如，Cursor 可使用 `~/.cursor/mcp.json`。其他客户端按各自的 MCP 设置入口填写相同的启动命令与参数。

### 4. 重启客户端并试用

先尝试：“列出我的 NTULearn 课程。”再查询公告或作业。首次访问可能需要允许系统密钥环读取。

## 工具列表

| 工具 | 用途 |
|---|---|
| `ntulearn_list_courses` | 列出已选课程。 |
| `ntulearn_get_course_contents` | 查看课程内容；使用 `parent_id` 进入子文件夹。 |
| `ntulearn_search_course_content` | 在指定课程中递归搜索内容。 |
| `ntulearn_get_upcoming` | 汇总课程日历，默认查询未来两周；可用 `type: "GradebookColumn"` 筛选相关考核项。 |
| `ntulearn_get_announcements` | 汇总课程公告，按时间从新到旧排列；可指定 `since`。 |
| `ntulearn_get_gradebook` | 查询成绩簿项目，以及学校已开放的个人成绩。 |
| `ntulearn_download_file` | 下载内容项的附件，可指定本地 `destination_dir`。 |
| `ntulearn_read_file_content` | 直接读取附件内容；PDF 默认提取文字，可用 `mode: "vision"` 和 `pages` 查看指定页面图像。 |
| `ntulearn_sync_materials` | 预览或执行课程资料增量同步，保留本地修改。 |
| `ntulearn_get_assignments` | 汇总作业、截止时间、原始提交状态和来源，并提供待核实的文档日期线索。 |

支持跨课程汇总的工具通常可以省略 `course_ids`。单课程内容查询仍需提供 `course_id`；以客户端展示的工具参数为准。

## 课程资料增量同步

调用 `ntulearn_sync_materials` 时，必须提供 `semester` 和 `destination_dir`，可以用 `course_ids` 限定课程。`semester` 是本地目录标签。

先预览：

```json
{
  "semester": "2026S1",
  "destination_dir": "~/NTU",
  "course_ids": ["_course_1"],
  "dry_run": true
}
```

确认预览后，以相同参数将 `dry_run` 改为 `false` 即可下载。默认值是 `true`，不会自动开始下载。

文件按“学期 / 课程 / 原有文件夹”组织，文件名和课程目录附带稳定短标识，降低重名冲突。同步记录保存在 `.ntulearn-manifest.json`，记录路径、哈希和时间，不存储登录凭据或带签名的下载链接。

- 已有记录且未变化的文件会跳过；失败文件在下次运行时重试。
- 本地修改会标记为 `local_conflict` 并保留；工具不会删除文件。
- 课程或文件夹改名后，已跟踪文件保留原有本地路径。
- `verify_remote: true` 会重新下载并比较哈希，即使远程修改时间未变；没有远程修改时间时也会重新下载比较。
- 每个成功文件及其同步记录分别通过原子写入保存，支持按文件继续同步；不支持单个文件的字节断点续传。
- 文件锁防止同一目标目录的同步任务并发写入，但外部编辑器不受该锁约束。

单文件上限为 **100 MiB**；每门课 `max_items` 默认 **500**、最大 **2000**，目录遍历深度上限 **15**。完成后应检查 `errors`、`truncated` 和各文件状态；一次调用结束不代表所有资料均已同步。

## 作业统一汇总

`ntulearn_get_assignments` 合并成绩簿、日历考核项和作业页面的信息：

```json
{
  "include_documents": true,
  "max_documents": 10,
  "max_items": 500
}
```

也可添加 `course_ids` 限定课程。跨来源合并依据关联 ID 或忽略大小写后完全相同的标题，不进行模糊标题合并。

结果保留来源链接、附件信息和可获得的原始提交状态。缺失提交状态表示**未知**。不同来源的结构化截止时间有冲突时，会保留这些时间并标记 `deadlineConflict: true`，不会自行选择一个日期。

公告和文档中的日期摘录单独放在 `deadlineEvidence` 中，始终标记 `needsReview: true`。这些摘录仅供人工核查，不会覆盖结构化截止时间，也不会推测日期格式、缺失年份、具体时间或作业归属。PDF 摘录带页码。

文档扫描覆盖内容文本页、标题或附件名与作业／考核／项目／测验／说明／课程大纲相关的内容，以及成绩簿关联页面，并非扫描所有课件的全部文字。

文档扫描默认开启；`max_documents` 默认 **10**，单次请求最多 **30**，单文档上限 **10 MiB**，PDF 最多处理 **100 页**。支持 PDF 文本、DOCX、PPTX、TXT 和 HTML；不提供 OCR，扫描版 PDF 需人工检查。请查看 `documentScan`、`errors` 和 `truncated`，返回零项不能证明没有待交作业。

## 登录与会话

Cookie 按以下顺序读取：

1. 从 **Chrome** 自动读取有效的 `BbRouter`。
2. 自动读取失败时，使用环境变量 `NTULEARN_COOKIE`。
3. 前两项不可用时，尝试系统密钥环中的最近成功值。

浏览器读取成功后会尽力缓存到系统密钥环。遇到 HTTP 401 时，服务会清除失效缓存、重新读取会话并重试一次；若仍失败，请回到 Chrome 重新完成登录后再试。

### 平台注意事项

| 平台 | 注意事项 |
|---|---|
| Linux | Chrome Cookie 解密可能需要解锁系统密钥环。MCP 客户端需保留 `DBUS_SESSION_BUS_ADDRESS`、`XDG_RUNTIME_DIR` 等桌面会话环境变量；无桌面或经过环境过滤的进程可能无法读取。 |
| macOS | 首次访问可能弹出钥匙串授权窗口；确认进程来源后允许访问。 |
| Windows | Chrome 的 App-Bound Encryption 可能阻止自动读取；可使用下面的手动配置。不建议为读取 Cookie 而提高整个 AI 客户端的权限。资料实际同步依赖 `fcntl`，不支持原生 Windows。 |

本仓库默认不自动读取 Edge、Firefox 或 Brave，部分上游配置文件中的多浏览器注释不适用于本版本。

### 手动配置 Cookie

自动读取不可用时，在已登录的 Chrome 中打开开发者工具，进入 **Application → Cookies → ntulearn.ntu.edu.sg**，复制 `BbRouter` 的 Value，在 MCP 配置对应服务的 `env` 中设置：

```json
{
  "NTULEARN_COOKIE": "expires:1234567890,id:REPLACE_WITH_YOUR_LOCAL_VALUE"
}
```

这只是格式示例，请在本机填写真实值，不要提交到 GitHub、发送到聊天或分享给他人。会话过期后需要更新。该变量是备用来源；Chrome 自动读取成功时，以浏览器中的值为准。

## 可选配置

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `NTULEARN_COOKIE` | 自动读取 | 手动会话备用值。 |
| `NTULEARN_BASE_URL` | `https://ntulearn.ntu.edu.sg` | Blackboard 实例地址；浏览器 Cookie 自动读取仍固定针对 NTULearn 域名。 |
| `NTULEARN_DOWNLOAD_DIR` | `./downloads` | 普通附件下载未指定 `destination_dir` 时的目录；增量同步仍需显式指定目标目录。 |

这些变量可设置在 MCP 服务的 `env` 中。

## 常见问题

**找不到 Cookie，或返回 401 / `BbRouterExpiredError`**

先在 Chrome 中重新登录 NTULearn，完成 SSO / MFA，再重试。若仍失败，检查密钥环、桌面会话环境或手动配置。

**客户端显示服务，但调用一直等待**

检查是否有隐藏的钥匙串或密钥环授权窗口，也可在仓库目录运行 `uv run ntulearn-mcp` 查看启动错误。服务使用 `stdio` 协议，直接运行后等待输入属于正常现象。

**大型 PDF 读取不完整，或提示超出容量**

直接读取有文件、批量内容和图像输出限制。请用 `ntulearn_download_file` 下载后交给支持 PDF 的客户端读取；仅需少量图表时，可指定 `mode: "vision"` 和较小的 `pages` 范围，例如 `"5"`。

**同步或作业汇总有遗漏**

检查返回的错误、截断标记、文档扫描情况和文件状态。可以缩小课程范围、调整参数后重试；涉及截止时间时，应打开结果来源核实。

## 开发与测试

```bash
uv sync --frozen
uv run python -m unittest discover -s tests
uv run ntulearn-mcp
```

```text
src/ntulearn_mcp/
├── server.py     # MCP 入口、工具处理、会话解析和文档读取
├── client.py     # 异步 Blackboard REST 客户端
├── cookie.py     # Chrome Cookie 自动读取
├── cache.py      # 系统密钥环缓存
├── parsers.py    # HTML 与下载链接解析
└── workflows.py  # 资料增量同步和作业统一汇总
```

测试使用 `unittest`，HTTP 请求通过 `httpx.MockTransport` 模拟。单元测试通过不代表真实账号、所有课程或所有平台均已验证。

## 隐私与使用说明

这是个人用途的非官方工具，与 NTU Singapore、Anthology 或 Blackboard 无隶属、赞助或背书关系。相关商标归各自所有者。

- 仅使用自己的账号，并遵守学校与课程的数据使用要求。
- Cookie 在本机读取，用于向配置的 Blackboard 服务认证；不要公开真实 Cookie、`.env`、成绩或私人课程资料。
- 服务返回的课程、公告、成绩和文档内容会交由 MCP 客户端处理，可能随对话发送给模型提供方；请了解所用客户端的数据政策。
- 下载的课程资料不因本项目开源而获得再分发许可。

项目以 [MIT 许可证](LICENSE) 发布，按现状提供，不作担保。
