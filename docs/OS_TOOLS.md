# 电脑助手功能与安全说明

> 这份文档面向两类读者：
>
> - **普通使用者**：读第 1–10 节，用大白话解释"能做什么、边界在哪、每一步怎么确认"。
> - **开发者**：读第 11 节，包含工具参数、安全阈值、代码位置和扩展方法。
>
> 代码实现：[agent/tools/os_tools.py](../agent/tools/os_tools.py)　桌面控制：[agent/tools/computer_use.py](../agent/tools/computer_use.py)　安全策略：[agent/tools/safety.py](../agent/tools/safety.py)　确认与事件：[agent/react_agent.py](../agent/react_agent.py)　界面：[app.py](../app.py)
>
> OmniPilot 桌面控制的完整接入故事、用法与风险，见 [OMNIPILOT_INTEGRATION_REPORT.md](OMNIPILOT_INTEGRATION_REPORT.md)。

---

## 0. 先看这张安全地图

这个助手不是一个"拿到电脑就随便动"的程序。它在多道防线后面工作：

| 防线 | 作用 | 你能看到什么 |
| --- | --- | --- |
| 1. 工作区域 | 所有文件操作（包括读取）只能在你允许的目录内 | 侧边栏显示当前工作区域 |
| 2. 敏感目录 | `.ssh`、`.aws` 等目录一律拒绝 | 工具返回"属于敏感目录" |
| 3. 人工确认 | 系统信息/进程读取 + 所有修改类操作必须你点确认 | 确认卡片，用人话解释每一步 |
| 4. 回收区 | 删除默认只是移到 `.agent_trash` | 操作记录里显示回收路径 |
| 5. 自动备份 | 覆盖已有文件前先备份 | 操作记录里显示备份路径 |
| 6. 命令拦截 | 危险命令、越界路径直接拒绝 | 工具返回"被安全策略拦截" |
| 7. 桌面控制确认 | 截图/剪贴板/应用列表和鼠标键盘动作必须确认 | 确认卡片说明坐标、目标窗口和隐私风险 |
| 8. 任务卡 | 只允许卡里列出的动作类别和目录范围 | 越界返回“守卫层拒绝” |
| 9. 只读总开关 | 一键禁用所有写操作 | 侧边栏「安全开关」 |
| 10. 批次回滚 | 一次任务的所有改动可整批撤销 | 侧边栏「批次与回滚」 |
| 11. 预算与急停 | 步数/时长/费用超限或按下急停后不再发起新动作 | 侧边栏状态 + `STOP` 文件 |
| 12. 第二主体 PIN | 命令、永久删除、桌面输入等 R2/R3 动作需要口令 | 确认卡片上的口令输入框 |

事后你还能查两本账：聊天页面的「操作记录」，以及 `logs/audit_YYYYMMDD.jsonl` 审计日志。

---

## 1. 它能做什么

它是一个跑在你自己电脑上的对话助手。你用日常语言告诉它想做什么，它会调用本机能力去完成：

- 想知道电脑状态，它去查；
- 想找文件，它去翻；
- 想改文件、删文件、执行命令，它会先把"要做什么"摆给你看，你点确认它才动手；
- 想看屏幕、点按钮、输入文字，它会先截图观察，再一步一步操作鼠标键盘，每一步都等你确认。

它不会偷偷执行。读取系统信息/进程、以及所有会改变电脑状态的操作，都必须经过你确认；所有实际执行的操作，都会留下记录。

---

## 2. 工作区域：所有操作的边界

### 2.1 为什么要有工作区域

电脑上有系统文件、程序安装目录、个人隐私目录。如果助手能访问整块硬盘，一次判断失误就可能改到不该改的地方。

工作区域就是给助手划一个“活动范围”：

- 范围内的文件可以看、可以改；
- 范围外的路径，**连读取都会被拒绝**；
- 敏感目录（`.ssh`、`.aws` 等）即使在范围内也拒绝。

这不是“建议”，是硬性检查。每个接路径的工具在真正执行前都会先过 `guard_path()`。

### 2.2 默认范围

| 路径 | 说明 |
| --- | --- |
| `C:\Users\Administrator` | 当前用户目录，桌面、下载、文档都在里面 |
| `D:\PyDocuments\project3` | 项目根目录 |

子目录自动包含，不需要逐个添加。

### 2.3 怎么查看当前范围

三个地方都能看到：

- 左侧边栏「工作区域」区块；
- 输入框左侧「工作区域」按钮；
- 直接问它“这台电脑的工作区域是哪里？”。

### 2.4 怎么修改

#### 方式一：界面按钮（推荐，立即生效）

1. 点击输入框左侧的「工作区域」按钮；
2. 在弹出的文本框里，每行写一个绝对路径；
3. 点「保存并应用」；
4. 保存成功后会立即生效，不需要重启。

保存的内容会写回 `config/agent.yml`，下次启动自动使用。

#### 方式二：直接改配置文件

编辑 `config/agent.yml`：

```yaml
workspace_roots:
  - "~"
  - "."
  - "D:/资料"
```

改完需要重启应用。

### 2.5 允许与拒绝的例子

| 你想做的事 | 路径 | 结果 |
| --- | --- | --- |
| 读桌面的报告 | `C:\Users\Administrator\Desktop\报告.docx` | 允许 |
| 读项目里的 README | `D:\PyDocuments\project3\README.md` | 允许 |
| 读系统文件 | `C:\Windows\win.ini` | 拒绝：不在工作区域 |
| 读 D 盘其他项目 | `D:\其他项目\a.txt` | 拒绝：不在工作区域 |
| 读 SSH 私钥 | `C:\Users\Administrator\.ssh\id_rsa` | 拒绝：敏感目录 |
| 写一个 bat 脚本 | `C:\Users\Administrator\Desktop\run.bat` | 拒绝：可执行后缀 |
| 执行越界命令 | `type C:\Windows\win.ini` | 拒绝：命令含工作区外路径 |
| 在桌面建文件夹 | `C:\Users\Administrator\Desktop\备份` | 允许，但会弹确认卡片 |

越界时工具会返回：

```text
不在允许的工作区域内，已拒绝：C:\Windows\win.ini
当前工作区域：
- C:\Users\Administrator
- D:\PyDocuments\project3
如需访问其他目录，请修改 config/agent.yml 的 workspace_roots 后重启应用。
```

它不会反复重试，也不会绕路。

### 2.6 填写规则

| 规则 | 说明 |
| --- | --- |
| 必须绝对路径 | 例如 `C:\Users\Administrator\Desktop`；也支持 `~` 和 `.` |
| 每行一个 | 多个目录就写多行 |
| 至少保留一个 | 全部删空会提示错误 |
| 不能是盘符根 | `C:\`、`D:\` 会被拒绝 |
| 不能是系统保护目录 | `C:\Windows`、`Program Files` 等会被拒绝 |
| 不能是敏感目录 | `.ssh`、`.aws` 等会被拒绝 |
| 路径可以暂时不存在 | 保存时会提示“暂时无法使用” |
| 子目录自动包含 | 加了 `D:\资料`，它的所有子目录都可用 |

### 2.7 常见错误

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 保存提示“不是绝对路径” | 写成了 `Desktop` 或 `.\data` | 改成完整路径，如 `C:\Users\Administrator\Desktop` |
| 保存提示“不能使用系统保护目录” | 写了 `C:\Windows` | 换成用户目录或项目目录 |
| 保存提示“至少需要保留一个” | 文本框被清空 | 至少留一行 |
| 保存成功但工具仍拒绝 | 路径拼错或已不存在 | 在文件管理器里复制一次完整路径 |
| 改了 yml 没生效 | 直接改文件后没重启 | 用界面保存，或重启应用 |
| 想临时访问其他盘 | 该盘不在工作区 | 在「工作区域」按钮里加一行，保存即可 |

### 2.8 敏感目录

即使目录在工作区域内，以下目录也会被拒绝：

```text
.ssh  .gnupg  .aws  .azure  .kube
```

这些目录通常存放密钥和云服务凭据。可以在 `config/agent.yml` 的 `sensitive_dirs` 里增删（这一项目前只能改配置文件，改完重启）。

---

## 3. 两种操作

| 类型 | 包括什么 | 它的行为 | 你需要做什么 |
| --- | --- | --- | --- |
| **只读（直接执行）** | 列目录、读文件、按名字搜、按内容搜；桌面的看屏幕尺寸、屏幕找图、等待 | 直接执行，不打扰你 | 无 |
| **敏感读取（需确认）** | 看系统信息、看进程列表；桌面的截图、读剪贴板、列应用 | 先停下，弹出确认卡片 | 点「我确认，执行」或「不要执行」 |
| **修改类（有副作用）** | 写文件、建目录、移动、复制、删除、执行命令、打开程序；桌面的鼠标点击/拖拽/滚动、键盘输入、写剪贴板、启停应用 | 先停下，弹出确认卡片 | 点「我确认，执行」或「不要执行」 |

查看类也会被记录，只是不需要你点头。桌面控制是一个工具（`computer_use`）里的 18 个动作，确认粒度按动作而不是按工具。

---

## 4. 六种"查看"能力

### 4.1 查看电脑配置和空间

> 这一项会弹出确认卡片，因为它会读取主机名、用户名等设备标识。

**你可以这样说：**

- "这台电脑什么配置？"
- "C 盘还剩多少空间？"
- "工作区域是哪里？"

**它会告诉你：** 操作系统版本、电脑名称、当前用户、CPU 核心数、内存占用、磁盘占用、桌面/下载/文档路径，以及当前工作区域。

**限制：** 不读取密码、浏览记录、聊天记录。

---

### 4.2 查看某个文件夹里有什么

**你可以这样说：**

- "看看桌面有什么文件"
- "列出项目文件夹的内容"
- "隐藏文件也要看"

**它会返回：** 名称、是文件还是文件夹、大小、最后修改时间。

```text
目录：C:\Users\Administrator\Desktop（共 3 项）
[目录] 项目资料 | - | 2026-10-20 14:22
[文件] 报告.docx | 1.2MB | 2026-10-24 09:15
```

**限制：**

- 只能列工作区域内的目录；
- 最多显示 500 项；
- 默认不显示以 `.` 开头的项，不检测 Windows 隐藏属性。

---

### 4.3 读取一个文本文件

**你可以这样说：**

- "帮我读一下 D 盘项目里那个说明.txt"
- "这个文件太长了，先读前 5000 个字"
- "从第 20000 个字开始往后读"

**它会返回：** 文件路径、总长度、本次读的是哪一段，然后是正文。

**限制：**

- 只能读工作区域内的文件；
- 单个文件最多读前 2 MB；
- 单次最多返回 20000 字符，可以分段继续读；
- 二进制文件会被拒绝；
- 编码依次尝试 UTF-8、GBK、Latin-1。

---

### 4.4 按文件名找文件

**你可以这样说：**

- "在项目里找所有 .py 文件"
- "找一下名字里带'报告'的文件"

**限制：** 搜索根目录必须在工作区域内；只匹配文件名，不匹配文件夹名；默认 50 条，最多 200 条。

---

### 4.5 按内容搜文件

**你可以这样说：**

- "在项目里搜一下 TODO"
- "哪些 .log 文件里出现了 timeout"

**它会返回：** `文件:行号: 内容`。

**限制：** 搜索根目录必须在工作区域内；跳过大于 5 MB 的文件；最多扫描 5000 个文件；是子串匹配，不是正则。

---

### 4.6 看哪些程序最占内存

> 这一项会弹出确认卡片，因为进程列表可能暴露你正在使用的软件。

**你可以这样说：**

- "哪个程序最占内存？"
- "列一下内存占用前 10 的进程"

**它会返回：** 进程 ID、程序名、内存占用、所属用户。

**限制：** 只按内存排序，不显示 CPU 百分比；默认 20 个，最多 100 个。

---

## 5. 七种"修改"能力与桌面控制

5.1–5.7 是文件与命令类修改；5.8–5.12 是桌面控制。除了 5.8 的查看分辨率和部分只读动作，其余都会弹出确认卡片，并写入审计日志。

### 5.1 写入或追加文本

**你可以这样说：**

- "在桌面新建 note.txt，写'明天开会'"
- "往日志.txt 末尾追加一行 hello"

**它会做什么：**

- 文件不存在就创建；
- `write` 模式覆盖，`append` 模式追加；
- **覆盖已有文件前，自动把原文件备份到 `.agent_backup`**；
- 父文件夹不存在会自动创建。

**它不会做什么：**

- 拒绝写入 `.exe`、`.dll`、`.bat`、`.cmd`、`.ps1`、`.vbs`、`.js`、`.msi` 等可执行后缀；
- 拒绝写入敏感目录。

**返回示例：**

```text
已写入 5 个字符到 C:\Users\Administrator\Desktop\note.txt
（原文件已备份到 C:\Users\Administrator\.agent_backup\20261026_104501_note.txt）
```

---

### 5.2 创建文件夹

**你可以这样说：** "在 D 盘建一个'备份'文件夹"

**限制：** 只能在 work区域内创建；已存在也算成功。

---

### 5.3 移动或重命名

**你可以这样说：**

- "把桌面的 a.txt 改名成 b.txt"
- "把这个文件挪到项目目录"

**规则：**

- 源和目标都必须在工作区域内；
- 目标是已存在的文件夹 → 移进去；
- 目标是新路径 → 当重命名处理。

**风险：** 原位置的文件会消失。重要文件建议先复制。

---

### 5.4 复制文件或文件夹

**你可以这样说：** "把这个文件复制到项目目录"

**规则：** 源和目标都必须在工作区域内；原文件保留。

---

### 5.5 删除文件或文件夹

**你可以这样说：**

- "删掉桌面的 test.tmp"
- "把这个文件夹连同里面的东西一起删掉"

**默认行为：移到回收区，不是永久删除。**

```text
已移到回收区：C:\Users\Administrator\.agent_trash\20261026_104501_test.tmp
（如需彻底删除，请显式使用 permanent=True）
```

**规则：**

- 删文件 → 移到回收区；
- 删空文件夹 → 直接删除；
- 删非空文件夹 → 必须明确说"连同里面的内容一起删"，才会用 `recursive=True`；
- 只有明确要求"永久删除"，才会 `permanent=True`。

**确认卡片会显示影响范围**，例如"3 个文件，共 12.4MB"。

---

### 5.6 执行系统命令

**你可以这样说：**

- "执行 ipconfig 看看网络"
- "运行 dir 看看项目目录"

**它会返回：** 命令、退出码、标准输出、标准错误。

**安全限制：**

- 命令中包含工作区域外的绝对路径 → 拒绝；
- 命中危险命令黑名单 → 拒绝，分为几类：
  - 磁盘与系统：`format`、`diskpart`、`bcdedit`、`reg delete`、`shutdown`
  - 删除：`del`、`erase`、`Remove-Item`、`rm`、`rd /s`、`rmdir /s`
  - 进程/用户/网络：`taskkill /f`、`net user /add`、`netsh`、`wmic`
  - 权限/属性：`attrib`、`icacls`、`takeown`
  - 下载/执行：`certutil`、`bitsadmin`、`mshta`、`rundll32`、`regsvr32`、`Start-Process`、`Invoke-WebRequest`、`Invoke-Expression`
  - 绕过手法：PowerShell 编码命令（`-EncodedCommand`）、系统环境变量（`%SystemRoot%`）、向上切换目录（`cd ..`）、`copy con`、重定向到可执行后缀、`Set-Content`/`Out-File` 到可执行后缀、cmd 转义符 `^`
  - 其他：`cipher /w`、`vssadmin delete`、`schtasks /create`
- 工作目录必须在工作区域内；不指定时，默认在第一个工作区根目录执行，不会继承应用启动目录；
- 默认最多等 60 秒，最长 600 秒。

**为什么连 `del a.txt` 也拦？** 因为命令行删除不进回收区。要删除文件，请让它用 `delete_path`，默认会移到 `.agent_trash`，可以恢复。

**风险提示：** 它没有沙箱。确认卡片是最后一道防线，不要批准你不理解的命令。

---

### 5.7 打开文件、文件夹或网址

**你可以这样说：**

- "打开项目文件夹"
- "打开这个网址 https://example.com"

**限制：** 文件和文件夹必须在工作区域内；网址不受工作区限制。

**风险：** 如果打开的是可执行文件，可能会运行它。

---

### 5.8 截取屏幕与查看分辨率

**你可以这样说：**

- "看看当前屏幕"
- "屏幕上现在是什么界面？"
- "当前屏幕分辨率是多少？"

**动作：**

- `screenshot`：截取全屏或指定区域，图片会附加给模型；**需要确认**。
- `screen_size`：只读取分辨率，直接执行。
- `locate_image`：给一张模板 PNG，让它在屏幕上找；直接执行。省略 `confidence` 时用精确匹配，不需要 `opencv-python`；填写 0–1 的阈值则需要安装 `opencv-python`。
- `wait`：等待 0–10 秒，让界面完成响应；直接执行。

**限制与风险：**

- 截图可能包含密码、聊天记录、邮件等隐私内容，并会发送给模型分析；
- 没有控件树/OCR，坐标要靠截图观察或模板匹配；
- 多显示器/DPI 缩放可能让坐标偏移；点击不准时先 `screen_size` 或 `locate_image`；
- 截图以 base64 写入会话记忆，连续截图会让会话体积变大。

---

### 5.9 鼠标操作

**你可以这样说：**

- "点一下屏幕上 (800, 450) 的位置"
- "把鼠标移到右上角"
- "把窗口从 (400,300) 拖到 (900,600)"
- "向下滚 5 格"

**动作：** `click`、`double_click`、`right_click`、`move_mouse`、`drag`、`scroll`，都需要确认。

**规则：**

- 坐标原点在屏幕左上角，单位是像素；
- 点击前先截图确认目标位置；
- 一次只做一个动作，做完再截图验证；
- 坐标错误可能误点其他按钮。

---

### 5.10 键盘输入与快捷键

**你可以这样说：**

- "在输入框里输入 hello"
- "按一下回车"
- "按 Ctrl+S 保存"

**动作：** `type_text`、`press_key`、`hotkey`，都需要确认。

**规则：**

- `type_text` 输入到**当前焦点窗口**；焦点不对时文字可能落到别处；
- 中文/Unicode 通过剪贴板粘贴实现，会覆盖剪贴板；
- 不要主动输入密码、验证码、密钥；用户明确要求时先提醒风险；
- `press_key` 支持 enter/tab/escape/f5/up/down 等；`hotkey` 用 `['ctrl','s']` 或 `'ctrl+s'`。

---

### 5.11 剪贴板

**你可以这样说：**

- "看看剪贴板里是什么"
- "把这段文字复制到剪贴板"

**动作：** `clipboard_get` / `clipboard_set`，都需要确认。

**风险：** 剪贴板可能包含密码、验证码、密钥；读取和写入都会覆盖或暴露内容。审计日志里会对剪贴板正文做脱敏。

---

### 5.12 启动、关闭与查看应用

**你可以这样说：**

- "打开记事本"
- "把 Chrome 关掉"
- "现在有哪些程序在运行？"

**动作：** `app_open` / `app_close` / `app_list`，都需要确认。

**安全边界：**

- `app_open` 拒绝启动终端、脚本宿主、注册表、磁盘和策略工具（cmd、PowerShell、WSL、cscript、mshta、regedit、diskpart、bcdedit 等），避免绕过 `run_command` 的黑名单；
- 应用名/参数中不允许 `& | < > ^ % ( ) " ;` 等 cmd 元字符，防止 `cmd /c start` 被解释成多条命令；
- `app_close` 拒绝关闭系统关键进程（explorer、svchost、lsass、winlogon 等）；
- 关闭用户程序前先提醒保存，未保存的数据可能丢失；
- 通过路径启动程序时，路径必须在工作区域内。

---

## 6. 确认卡片：每一步都用人话解释

当它要做修改类操作时，聊天区会出现这样一块内容：

```text
┌──────────────────────────────────────────────────┐
│ 下面有 2 个操作需要你确认。它们会操作这台电脑      │
│（修改文件、设置或控制界面），确认前请看清每一步。  │
│                                                  │
│ 1. 移到回收区                                     │
│    把「C:\Users\...\test.tmp」移到工作区的        │
│    .agent_trash 回收目录。                        │
│    C:\Users\Administrator\Desktop\test.tmp        │
│    影响范围：1 个文件，共 820B                    │
│    需要注意：文件会移到工作区的 .agent_trash      │
│    回收目录，可以手动恢复。                       │
│                                                  │
│ 2. 执行系统命令                                   │
│    运行命令「ipconfig」。                         │
│    ipconfig                                      │
│    需要注意：命令会以你当前的用户权限执行，       │
│    可能修改文件、网络或系统设置。                 │
│                                                  │
│ [ 我确认，执行 ]        [ 不要执行 ]              │
└──────────────────────────────────────────────────┘
```

**每一项包含四层信息：**

1. **标题**：这个操作叫什么（移到回收区、执行系统命令）；
2. **大白话说明**：它具体要做什么；
3. **目标**：操作的完整路径或命令；
4. **风险说明**：最坏情况是什么，能不能恢复。

删除操作还会额外显示**影响范围**（多少个文件、总共多大）。

**当前限制：**

- 一次只能全部确认或全部拒绝；
- 不能修改参数后再执行。发现路径不对，点「不要执行」，然后重新说一遍需求。

---

## 7. 操作记录：做完之后能看到什么

每次回答下面会有一个可展开的「本次操作记录」，列出这一轮实际执行过的所有工具调用，包括只读操作。

示例：

```text
本次操作记录（3 步）

1. 查看文件夹内容（已完成）
   打开了「C:\Users\Administrator\Desktop」，看看里面有哪些文件和文件夹。
   结果：目录：C:\Users\Administrator\Desktop（共 3 项）

2. 读取文件内容（已完成）
   打开了「C:\Users\Administrator\Desktop\笔记.txt」并读取其中的文字，没有改动文件。
   结果：文件：C:\Users\...\笔记.txt（总长度 128 字符）

3. 移到回收区（已完成）
   把「C:\Users\Administrator\Desktop\test.tmp」移到工作区的 .agent_trash 回收目录。
   结果：已移到回收区：C:\Users\Administrator\.agent_trash\...
```

**为什么要有这个？**

- 你能核对它到底做了什么，而不是只听它说；
- 它说"已删除"时，你能看到是移到回收区还是永久删除；
- 多步操作的顺序一目了然，方便排查问题。

**注意：**

- 最新一轮的操作记录默认展开，历史记录折叠显示；
- 状态显示「未完成」时，可能是工具报错，也可能是「结果校验」没通过（详见 8.9）；
- 从记忆后端恢复的旧会话没有操作记录，只显示文字；
- 完整记录以 `logs/audit_YYYYMMDD.jsonl` 为准。

---

## 8. 九道自动保险

### 8.1 回收区

- 位置：工作区域根目录下的 `.agent_trash`；
- 删除文件/非空目录时，自动移进去，文件名带时间戳；
- 恢复方式：用文件管理器把它拖回原位置；
- 只有 `permanent=True` 才真正删除。

### 8.2 覆盖前备份

- 覆盖已有文件前，自动复制一份到 `.agent_backup`；
- 文件名格式：`时间戳_原文件名`；
- 超过 10 MB 的文件不自动备份，会在结果里说明；
- 追加模式不备份（不会破坏原内容）。

### 8.3 可执行文件拦截

禁止写入以下后缀：

```text
.exe .dll .sys .bat .cmd .ps1 .vbs .vbe .js .jse
.wsf .wsh .scr .msi .com .cpl .jar
```

这是为了防止"写一个脚本再执行"的绕过链路。

### 8.4 危险命令拦截

见 5.6。命中即拒绝，不进入确认流程。

### 8.5 工具次数与任务预算

- 每个任务默认最多 **30 次工具调用 / 10 分钟 / 2 元**（任务卡可调）；
- 等待人工确认的时间不计入时长；
- 另有一个 **100 步硬上限** 作为兜底；
- 超限后 `guard_model` 会直接返回“任务已停止”，不再调用模型；
- 目的是防止模型在错误路径上反复循环、烧钱。

### 8.6 审计日志

- 位置：`logs/audit_YYYYMMDD.jsonl`；
- 每行一条 JSON，记录时间、事件、任务/批次、工具名、参数、状态、可逆性、审批人、结果摘要；
- 覆盖所有实际执行的工具调用，包括只读；也记录任务开始/结束、审批、急停、回滚；
- 写入失败不影响主流程。

示例：

```json
{"time": "2026-10-27T11:08:05", "event": "tool_call", "trace_id": "a1b2", "task_id": "a1b2", "batch_id": "B20261027-110805-a1b2", "step_no": 3, "session_id": "user_001", "tool": "read_text_file", "args": {"path": "C:\\...\\a.txt"}, "status": "success", "reversibility": "R0", "approver": "agent", "result": "文件：C:\\..."}
```

### 8.7 桌面应用与进程黑名单

- `app_open` 拒绝终端、脚本宿主、注册表、磁盘和策略工具，避免“开终端→敲命令”绕过 `run_command` 黑名单；
- 应用名与启动参数拒绝 `&`、竖线、重定向、`^`、`%`、括号、引号、分号等 cmd 元字符；
- `app_close` 拒绝关闭系统关键进程（explorer、svchost、lsass 等），缩写也拦；
- 实现位置：`agent/tools/computer_use.py` 的 `_BLOCKED_APPS` / `_BLOCKED_APP_KEYWORDS` / `_BLOCKED_APP_CHARS` / `_PROTECTED_PROCESSES`。

### 8.8 日志脱敏

- 截图的 base64 不写操作记录与审计日志，只留 `[图片已附加给模型，base64 已省略]`；
- `type_text` / `clipboard_set` 的参数、`clipboard_get` 的结果在审计日志里显示为 `***（已脱敏，N 字符）`；
- 实现位置：`agent/tools/safety.py` 的 `summarize_content_for_log` / `sanitize_tool_args` / `sanitize_tool_result`；
- 局限：其他工具的参数（如 `write_text_file` 的内容、`run_command` 的完整命令）仍未通用脱敏。

### 8.9 结果校验（写完/删完再对一次账）

工具说“已写入 / 已移动 / 已删除”之后，系统会真的去文件系统核对一次：

| 工具 | 核对什么 |
| --- | --- |
| 写入文件 | 文件存在；回读内容与参数一致（追加则比对结尾） |
| 创建目录 | 目录真的存在且是目录 |
| 移动/重命名 | 源路径已消失、目标路径存在 |
| 复制 | 目标路径存在；文件还要大小与 SHA-256 一致 |
| 删除 | 原路径确实不存在 |
| 命令 | `退出码：0` 才算命令自身成功；非 0 标“未通过” |
| 桌面动作 / 打开程序 | 无法用文件系统判定，标“未确认”，需人看一眼 |

- 核对通过：结果里会多一行 `[结果校验] 已核对：...`；
- 无法核对：多一行 `[结果校验] 未确认：...`，状态仍是成功，但**不是“已完成”**；
- 核对不通过：多一行 `[结果校验未通过] ...`，同时状态改为「未完成」，模型不会把失败当成功；
- 核对本身出错时只记日志、不阻断操作；
- 实现位置：`agent/tools/verification.py`。

> 局限：命令退出码为 0 只代表命令自身认为成功，关键产物（文件数、行数、合计）仍需另行核对；GUI 结果仍需人工确认。

### 8.10 守卫层怎么用（小企业版 P0）

| 想做什么 | 去哪里 | 说明 |
| --- | --- | --- |
| 给任务定目标/判据/范围/预算 | 侧边栏「任务卡」 | 缺目标/成功判据无法启用；越界会被拒绝 |
| 临时禁止一切写入 | 侧边栏「安全开关」的「只读模式」 | 也可用 `AGENT_READ_ONLY=1` 临时演练 |
| 撤销一次任务的全部改动 | 侧边栏「批次与回滚」 | 勾选确认后点「撤销整批」；永久删除会报不可逆 |
| 立刻让任务停下来 | 侧边栏「急停」 | 写 `.agent_state/STOP`；独立于模型 |
| 设置审批口令 | `uv run python scripts/setup_approval_pin.py` | 只存哈希；不写进 `.env`/提示词 |
| 看某任务发生了什么 | 侧边栏「批次与回滚」→「查看回放」 | 审计三问：做了什么、谁批的、哪个批次 |

更细的入门讲解见 `docs/small-business-p0/01–08`。

---

## 9. 它现在还不能做什么

| 做不到 | 说明 |
| --- | --- |
| 控件树/OCR/窗口选择器 | 目前靠截图 + 坐标/模板匹配 |
| 多显示器/DPI 自动校准 | 坐标可能偏移，需要手动核对 |
| 启动终端/脚本宿主/注册表/磁盘工具 | 为防止绕过 `run_command`，`app_open` 直接拒绝 |
| 关闭系统关键进程 | `app_close` 直接拒绝 |
| 删除到 Windows 回收站 | 用的是工作区内的 `.agent_trash` |
| 命令沙箱隔离 | 只有黑名单和路径检查，不是沙箱 |
| 对命令退出码做校验 | 已校验：非 0 标“未通过”；但退出码 0 只代表命令自身认为成功，关键产物仍需核对 |
| 对桌面操作结果做校验 | 无法用文件系统判定，统一标“未确认”，需要人看一眼 |
| 逐项确认、改参数 | 只能全部确认或全部拒绝；R2/R3 需输入审批口令 |
| 真正的第二主体隔离 | PIN 是同机口令，挡不住同一 Windows 用户下的恶意程序 |
| 永久删除回滚 | 永久删除不可逆；批次回滚会明确报告 |
| 不可篡改审计 | 批次/审计是普通文件追加写，不是 WORM |
| 费用精确计费 | 只有供应商返回 `usage_metadata` 时才累计；否则只记录调用次数 |
| 查询端口、服务、注册表 | 未封装对应工具 |
| 跨工作区移动大文件 | 受工作区限制，不能把文件移出边界 |

---

## 10. 常见问题

**问：它会不会偷偷执行命令？**

不会。修改类操作会停在确认卡片，你不点确认就不会执行。执行过的操作都会出现在「操作记录」和审计日志里。

**问：为什么它说"不在允许的工作区域内"？**

目标路径不在 `config/agent.yml` 的 `workspace_roots` 里。可以让它换一个工作区内的路径，或者你修改配置后重启。

**问：为什么 `.ssh` 里的文件读不了？**

`.ssh`、`.aws` 等属于敏感目录，默认拒绝。确实需要时，从 `sensitive_dirs` 里移除对应项再重启。

**问：删除后去哪里找？**

工作区域根目录下的 `.agent_trash`，文件名前面带时间戳。把它拖回原位置即可恢复。

**问：一次任务改了很多文件，能一起撤销吗？**

可以。侧边栏「批次与回滚」找到对应批次，勾选「我确认要撤销这一批」后点「撤销整批」。永久删除会明确报“不可逆”，不会假装恢复。

**问：为什么命令/桌面操作还要我输入口令？**

这类动作难逆或不可逆（R2/R3）。口令（PIN）是第二主体，只在本机校验；先运行 `uv run python scripts/setup_approval_pin.py` 设置。未设置时批准按钮会禁用。

**问：覆盖错了怎么办？**

去 `.agent_backup` 找对应时间戳的备份文件。注意只有 10 MB 以内的文件会自动备份。

**问：为什么 `.bat` 文件写不进去？**

可执行后缀被安全策略拦截。可以先用 `.txt` 保存，再自己重命名。

**问：为什么命令被拒绝了？**

可能命中危险命令黑名单，或者命令里包含工作区域外的路径。工具会说明具体原因。

**问：操作记录和审计日志有什么区别？**

操作记录在聊天页面，给人看，跟着会话走；审计日志是文件，给排查和追溯用，按天分文件。

---

## 11. 开发者参考

### 11.1 工具清单

文件：[agent/tools/os_tools.py](../agent/tools/os_tools.py)

| 序号 | 工具名 | 入参 | 默认值 | 确认 | 工作区校验 |
| --- | --- | --- | --- | --- | --- |
| 1 | `get_system_info` | 无 | — | **是** | 不涉及路径 |
| 2 | `list_directory` | `path`、`show_hidden` | `.`、`False` | 否 | 是 |
| 3 | `read_text_file` | `path`、`start`、`max_chars` | `0`、`20000` | 否 | 是 |
| 4 | `search_files` | `root`、`pattern`、`max_results` | `.`、`*`、`50` | 否 | 是 |
| 5 | `search_text` | `root`、`keyword`、`file_glob`、`max_results` | `.`、空、`*.*`、`50` | 否 | 是 |
| 6 | `list_processes` | `top_n` | `20` | **是** | 不涉及路径 |
| 7 | `write_text_file` | `path`、`content`、`mode` | `write` | **是** | 是（写） |
| 8 | `create_directory` | `path` | — | **是** | 是（写） |
| 9 | `move_path` | `source`、`destination` | — | **是** | 是（写，双端） |
| 10 | `copy_path` | `source`、`destination` | — | **是** | 是（写目标） |
| 11 | `delete_path` | `path`、`recursive`、`permanent` | `False`、`False` | **是** | 是（写） |
| 12 | `run_command` | `command`、`cwd`、`timeout` | `None`、`60` | **是** | 是（cwd + 命令内路径） |
| 13 | `open_path` | `path` | — | **是** | 是（URL 除外） |

桌面控制：[agent/tools/computer_use.py](../agent/tools/computer_use.py)，一个工具 18 个动作。

| 动作 | 主要参数 | 确认 |
| --- | --- | --- |
| `screenshot` | `region`、`attach` | **是** |
| `screen_size` | 无 | 否 |
| `locate_image` | `image_path`、`confidence` | 否 |
| `wait` | `seconds` | 否 |
| `click` | `x`、`y`、`button`、`clicks` | **是** |
| `double_click` / `right_click` | `x`、`y` | **是** |
| `move_mouse` | `x`、`y` | **是** |
| `drag` | `x1`、`y1`、`x2`、`y2`、`button` | **是** |
| `scroll` | `amount`、`x`、`y` | **是** |
| `type_text` | `text`、`interval` | **是** |
| `press_key` | `key`、`presses` | **是** |
| `hotkey` | `keys` | **是** |
| `clipboard_get` / `clipboard_set` | `text`（写入时） | **是** |
| `app_open` | `name`、`arguments` | **是** |
| `app_close` | `name`、`force` | **是** |
| `app_list` | `name_filter`、`limit` | **是** |

### 11.2 工作区域配置

`config/agent.yml`：

```yaml
workspace_roots:
  - "~"          # Path.home()
  - "."          # get_project_root()

sensitive_dirs:
  - ".ssh"
  - ".gnupg"
  - ".aws"
  - ".azure"
  - ".kube"
```

解析逻辑在 `agent/tools/safety.py` 的 `_load_workspace_roots()`，模块导入时执行一次，所以修改后需要重启。

相对路径统一基于**项目根目录**解析，避免从不同目录启动应用时指向不同位置。路径比较使用 `os.path.realpath` + `os.path.normcase`，兼容 Windows 大小写与 8.3 短路径差异。

也可以直接在界面修改：输入框左侧「工作区域」→ 每行一个绝对路径 → 「保存并应用」。
界面保存会重写 `config/agent.yml`（原有注释可能丢失），并立即生效，无需重启。

### 11.3 安全阈值

| 常量 | 位置 | 值 |
| --- | --- | --- |
| `_MAX_READ_BYTES` | `os_tools.py` | 2 MB |
| `_MAX_SEARCH_FILES` | `os_tools.py` | 5000 |
| `_MAX_SEARCH_FILE_BYTES` | `os_tools.py` | 5 MB |
| `_MAX_RESULT_CHARS` | `os_tools.py` | 8000 |
| `_BACKUP_MAX_BYTES` | `safety.py` | 10 MB |
| 危险命令黑名单 | `safety.py` | 28 条规则 |
| `TOOL_CALL_LIMIT` | `react_agent.py` | 100 次/轮兜底（每任务默认预算 30 步，见 `agent/guard/state.py`） |
| `_MAX_TEXT_CHARS` | `computer_use.py` | 10000 字符/次 |
| `_MAX_CLIPBOARD_CHARS` | `computer_use.py` | 20000 字符/次 |
| `_MAX_WAIT_SECONDS` | `computer_use.py` | 10 秒 |
| `_BLOCKED_APPS` | `computer_use.py` | 终端/脚本宿主/注册表/磁盘/策略工具 |
| `_PROTECTED_PROCESSES` | `computer_use.py` | 系统关键进程 |

代码内另有上限：目录 500 项、`max_chars` 50000、搜索 200 条、进程 100 个、命令 600 秒。

### 11.4 确认机制

`agent/react_agent.py`：

```python
CONFIRM_TOOLS = {
    "write_text_file": "写入文件",
    "create_directory": "创建目录",
    "move_path": "移动或重命名",
    "copy_path": "复制文件",
    "delete_path": "删除文件或目录",
    "run_command": "执行系统命令",
    "open_path": "打开文件或网址",
    "computer_use": "控制桌面（截图/鼠标/键盘/剪贴板/应用）",
}

_HITL_CONFIG = {
    name: {"allowed_decisions": ["approve", "reject"]}
    for name in CONFIRM_TOOLS
}
# computer_use 只读动作放行，其余动作确认
_HITL_CONFIG["computer_use"]["when"] = lambda request: action_requires_confirmation(
    (request.tool_call.get("args") or {}).get("action")
)

hitl_middleware = HumanInTheLoopMiddleware(
    interrupt_on=_HITL_CONFIG,
    description_prefix="该操作会修改电脑状态，请确认参数后再决定是否执行",
)
```

链路：

1. `after_model` 命中 `interrupt_on` → `interrupt()` 挂起；
2. `updates` 流产出 `__interrupt__`；
3. `ReactAgent._stream` 转成 `AgentEvent(type="interrupt")`；
4. `app.py` 渲染小白化卡片；
5. 用户点击后调用 `resume_stream(decisions, pending_actions=...)`；
6. `Command(resume=...)` 恢复；批准则执行，拒绝则返回 error `ToolMessage`。

`pending_actions` 用于把恢复后的 `ToolMessage` 与之前被挂起的操作配对，保证操作记录完整。

### 11.5 操作记录事件

`AgentEvent` 有三种类型：

| type | 字段 | 说明 |
| --- | --- | --- |
| `text` | `content` | 模型输出文本 |
| `interrupt` | `action_requests` | 待确认操作 |
| `tool` | `tool_call` | 已执行工具的名称、参数、结果、状态 |

`_stream` 同时订阅 `values` 和 `updates`：

- `values` 用于取最终回答；
- `updates` 用于捕获 interrupt 和 ToolMessage。

`ToolMessage` 与 `AIMessage.tool_calls` 按 `tool_call_id` 配对。

### 11.6 审计日志格式

文件：`logs/audit_YYYYMMDD.jsonl`，每行：

```json
{
  "time": "2026-10-27T11:08:05",
  "event": "tool_call",
  "trace_id": "a1b2",
  "task_id": "a1b2",
  "batch_id": "B20261027-110805-a1b2",
  "step_no": 3,
  "session_id": "user_001",
  "tool": "delete_path",
  "args": {"path": "C:\\...\\a.txt", "permanent": false},
  "status": "success",
  "reversibility": "R1",
  "approver": "human",
  "approval_token": "……",
  "result": "已移到回收区：..."
}
```

写入位置：`agent/tools/middleware.py` 的 `monitor_tool`。

### 11.7 新增工具

1. 在 `os_tools.py` 用 `@tool(description=...)` 定义函数，返回 `str`。
2. 接路径的参数先调用 `guard_path()`，写操作传 `for_write=True`。
3. 只读工具直接注册到 `react_agent.py` 的 `tools`。
4. 有副作用的工具同时加入 `CONFIRM_TOOLS`。
5. 在 `safety.py` 的 `describe_operation` / `operation_risk` / `operation_target` 补上人话模板。
6. 更新 `prompts/main_prompt.txt` 的工具清单。
7. 同步本文 11.1、README 能力表、ARCHITECTURE 第 7 节、architecture.svg。
8. 桌面类动作加到 `agent/tools/computer_use.py`：在 `_ACTION_PARAMS` / `_REQUIRED_PARAMS` / `_tool_instance` / `_prepare_arguments` 登记，并判断是否加入 `READ_ONLY_ACTIONS`；同时在 `safety.py` 的 `_describe_computer_use` / `_computer_use_risk` / `_computer_use_target` 补人话文案。

### 11.8 测试

```powershell
# 工具本身（不调用模型）
uv run -m agent.tools.os_tools
uv run -m agent.tools.computer_use

# 工作区与安全策略
uv run python -c "from agent.tools.safety import workspace_text; print(workspace_text())"

# 小企业版 P0 守卫层用例
uv run python -m unittest tests.test_small_business_guard -v
uv run python scripts/verify_small_business.py
```

### 11.9 实现边界

- 工作区限制基于路径字符串比较，不防符号链接/junction 逃逸（`resolve()` 已尽量解析）。
- 命令检查是黑名单 + 正则，不是沙箱；精心构造的命令仍可能绕过。
- 审计日志只记录实际执行的工具，不记录被 HITL 拒绝的调用。
- 操作记录依赖 `updates` 流；自定义中间件如果吞掉 ToolMessage，记录会缺失。
- 历史会话从 checkpointer 恢复时没有操作记录（checkpointer 只存 messages）。
- 界面保存使用 `yaml.safe_dump` 重写 `config/agent.yml`，原有注释会丢失。
- 桌面控制依赖 pyautogui/Pillow，必须在有桌面会话的环境中运行；无头/SSH 环境只能跑文件、命令等非 GUI 工具，截图/键鼠/应用操作需要桌面会话。
- 截图/剪贴板内容会进入模型上下文；审计日志已对图片 base64、`type_text`/`clipboard_set` 参数与 `clipboard_get` 结果做定向脱敏，但模型上下文本身不做持久化脱敏。
