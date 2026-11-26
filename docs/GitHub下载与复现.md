# GitHub 下载与复现

在 [RateStats 仓库](https://github.com/COKEiiii/RateStats) 克隆源码，或使用 GitHub 的 Code → Download ZIP。解压后从含 README 和「首次安装.command」的项目根目录操作。

仓库包含纯文字 LLM 流程、银行适配器、四份 Excel 交付逻辑、人工修正入口和配置示例。银行链接表、历史底稿、格式样板、邮件配置和运行记录需在本机单独准备。

## 在另一台电脑运行

1. 准备 macOS、Python 3.11 或更高版本、桌面 Microsoft Excel 和 Ollama。使用 GUI 还需要 Python 的 `tkinter`。
2. 将源码解压到本地可写目录，双击 `首次安装.command`，等待依赖和浏览器安装完成。
3. 在 Ollama 下载 `qwen3.5:4b`，例如运行 `ollama pull qwen3.5:4b`。框架只向模型发送文字。
4. 从获授权的来源准备银行链接表、历史底稿和格式样板，按 [README 的工作簿说明](../README.md#模板底稿与适配规则) 放置或在本机配置中选择。完成后双击「检查本机运行环境.command」和「一键运行.command」，重新访问官网并生成四份 Excel。
5. 要发送邮件，在新电脑单独打开「邮件设置.command」，配置 SMTP、收件人和发送方式。可点「导入旧配置」载入旧项目的 `assets/email_params.log`，检查后再启用。
6. 按 README 可运行离线自测。自测使用历史测试数值，正式报价以本期实时批次为准。

修改利率链接：编辑 `inputs/bank-links.xlsx` 中“工作表1”或“补充来源”的 D 列，保存并关闭后启动新任务。

修改本期 Premier 利率：在输出调研工作簿的“本期专属明细”J 列编辑；全量版和格式对齐版各自的 Premier Rates 均保留公式引用。跨期保留修正仍使用框架的人工修正入口。

具体依赖、适配边界、历史底稿选择和验证方法见 [README](../README.md) 与 [一键运行说明](../一键运行说明.md)。
