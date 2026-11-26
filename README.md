# llm-auto：纯文字 LLM 市场利率采集

使用本机 Ollama 读取银行网页文字、HTML 表格和 PDF 文本，生成调研表和彩虹表。模型请求只含文字；截图仅保留产品表、必要说明区域和 PDF 整页，供人工查看，同一表或 PDF 页的文字任务共用图片，不再逐行逐格截图。网页原文、原始 PDF 和校验值仍保存。图片型 PDF、没有可读文字的来源或未通过核对的结果会提示处理，不生成虚构的视觉结果。

完整流程保留 SGD 促销、挂牌、外币促销、Premier Rates、人工修正、历史滚动、两份 Excel 的插表适配，以及两套格式整理。结果一次交付四份文件：全量调研、全量彩虹表、格式对齐调研、格式对齐彩虹表。原模板和底稿不覆盖。

## 在另一台电脑安装

GitHub 发布包的选择、校验及下载步骤见 [GitHub 下载与复现](docs/GitHub下载与复现.md)。

完整流程需要 macOS、Python 3.11+、桌面 Microsoft Excel、Ollama 和可访问银行官网的网络。使用批次选择或邮件设置窗口还需要 Python 的 `tkinter`。新电脑请重装 Python 依赖和浏览器，并下载本地模型。

1. 将项目放在可写目录，首次启动 Microsoft Excel 和 Ollama，并在 Ollama 中确认本机已有 `qwen3.5:4b`（或按模型配置改用其他本地模型）。
2. 在 Finder 中双击 `首次安装.command`。脚本建立或复用项目的 Python 环境，并安装依赖和 Chromium。
3. 双击 `检查本机运行环境.command`。首次控制 Excel 时，按 macOS 提示允许终端/Python 自动化 Microsoft Excel。
4. 从获授权的来源取得下文所列的银行链接表、历史底稿和格式样板，放在项目所需路径，或按本机配置指定路径。
5. 双击 `一键运行.command` 执行采集和四份 Excel 交付。

如 Gatekeeper 阻止本地脚本，可在 Finder 对 `.command` 文件点按右键并选择“打开”。也可在终端进入项目目录后执行：

```sh
bash scripts/install_macos.sh
.venv/bin/python -m scripts.run_all --check
.venv/bin/python -m scripts.run_all
```

Mac 本机配置仍保存在未跟踪的 `config/project.local.json` 与 `config/ollama.runtime.local.json`；Ollama 只连接回环地址，不回退到云端。

无默认打印机的 Mac 无法使用 Excel 的自动 PDF 打印接口。此时仅局部 PDF 预览使用 LibreOffice 转换临时的值工作簿，报价写入、公式调整、重算和交付检查仍由 Microsoft Excel 完成。在另一台无打印机的 Mac 上，请安装 LibreOffice 到“应用程序”；安装脚本会记录转换器路径，也可在 `config/excel.runtime.local.json` 的 `pdf_converter` 中指定可执行程序。保存绝对路径可确保 Finder 双击入口也能找到它。

如需下载默认模型，请在终端运行 `ollama pull qwen3.5:4b`。如需使用其他本机模型，请修改 `config/project.local.json` 的 `models.text_model`，再运行环境检查。

Ollama 采用本机回环地址，默认 `http://127.0.0.1:11434`，禁止云端回退。模型目录位于其他磁盘时，仅在 `config/ollama.runtime.local.json` 设置本机路径；该文件不会上传。硬件性能影响速度，未承诺所有电脑使用相同耗时。

可运行下方的 `scripts.probe_local_model` 检查本机模型的结构化文字输出。模型探测和离线自测之外，实际报价仍以本次采集的来源核验为准。

## 模板、底稿与适配规则

| 文件                              | 作用                                 |
| --------------------------------- | ------------------------------------ |
| `inputs/bank-links.xlsx`        | 银行来源链接                         |
| `inputs/baseline-report.xlsx`   | 完整调研历史底稿，包含业务结构及明细 |
| `inputs/baseline-rainbow.xlsx`  | 完整彩虹历史底稿                     |
| `templates/report-format.xlsx`  | 调研展示样板                         |
| `templates/rainbow-format.xlsx` | 彩虹展示样板                         |
| `config/workbook-policy.json`   | 银行、产品、币种、期限及展示口径     |

以上工作簿需另行准备。使用旧底稿时，仍需主动确认是否登记为历史。全量流程和 Excel 离线自测均需要配置相应工作簿。

更换底稿请双击 `选择处理文件.command`；更换样板编辑 `config/format-templates.local.json`。相对路径按项目根目录解析。下次采集应选择两份“全量”文件。格式对齐调研保留“本期专属明细”供 Premier 联动，但删除其他采集辅助明细，仍不适合继续作为采集底稿。

## 修改利率链接与 Premier 明细

打开 `inputs/bank-links.xlsx`，在“工作表1”按银行、币种、促销/挂牌找到对应行，直接修改 **D 列网址**，保存并关闭 Excel，然后启动新采集。网址显示文字优先于单元格内旧超链接；清空网址会提示缺失，不会悄悄使用旧链接。A/B/C 列是来源身份，移动时请移动完整银行区块，勿单独排序网址列。程序不再依赖固定行号。

固定条款、产品说明等入口统一放在同一工作簿的 **“补充来源”D 列**，C 列来源编号保持不变。网页中每期更新的公告和条款附件，仍从表内登记的官方入口发现并记录到本次采集证据。更换网址不会自动改变页面结构适配；若新版页面找不到完整表格、币种或期限，程序会提示核实。继续旧任务使用其已有证据，改链接后要启动新任务。

全量调研和格式对齐调研的 `Premier Rates` 都以公式引用各自工作簿内的 **“本期专属明细”**。可在该页 **J 列**修改本期年利率，例如输入 `3.50%`；对应展示单元格会随 Excel 自动计算更新。空白显示 `-`，`0%` 保留为零；记录丢失或重复时显示“待核实”。公式按记录编号匹配，排序明细不会串客群。A 列编号不要手改；新增产品、改变金额档或资格后请重新生成。手动编辑输出表只影响该文件，需要跨期保留的修正仍走框架的人工修正流程。

现行完整公开报价模式按实际期限分组，8M 与 9M 各自展示。报价保留产品、客群、渠道、金额边界和来源原币种，CNH 可在人民币区展示。程序还保留 DBS 挂牌加点公式、Personal 排序、历史日期与公式、涨跌箭头、Premier 同步以及条款和公告日期。新增银行结构、未知币种或全新模板仍需补充适配。

## 日常操作

- `一键运行.command`：访问官网并执行全部采集与输出。
- `继续上次全量任务.command` 打开批次选择窗口。搜索日期、启动时间或批次号，查看新加坡时间、当前阶段和错误。实时批次在启动当天可续跑，已完成的当前项目交付可校验后打开。指定断点的终端入口为 `.venv/bin/python -m scripts.run_all --resume "runs/批次目录" --resume-only`。
- `全部重新生成.command`：使用新项目已保存的来源重新生成，首次运行没有来源时会提示先采集。
- `人工修正入口.command`、`挂牌人工修正入口.command`、`外币促销人工修正.command`：核实来源后确认、修正、排除或补录。
- `确认本期作为历史底稿.command`：检查后主动登记历史；运行程序不会自动批准。
- `整理调研表格式.command`：独立整理已有的完整调研表。

原始来源、模型响应、核验结果及修正记录分别保留。取消 VLM 后采用文字与原文绑定、表格行列校验、金额和单位规则、重复/缺失检查及人工复核；单模型结果不能视为准确率保证。旧双路运行记录和旧模型缓存不作为纯 LLM 的通过记录。

全量任务失败时，终端末尾会列出已确认的失败银行、业务类别、子步骤和原因，并提供日志路径及本批次的续跑命令。环境或 Excel 交付问题会显示实际出错的步骤。

## 发送结果邮件

双击「邮件设置.command」，或运行 `.venv/bin/python -m scripts.email_settings`。可填写 SMTP 和收件信息，或通过「导入旧配置」载入旧项目的 `assets/email_params.log`。预览后勾选「启用正式邮件发送」并保存。本机设置保存在 `config/email.local.json`。

在「发送方式」可选一封四张表，或两封各两张表。分两封时，全量调研与全量彩虹表为第一封，格式对齐调研与彩虹表为第二封，每封附件合计须低于 10,000,000 字节。设置适用于终端、独立发送窗口和全量任务完成后的发送提示。

启用后，全量任务完成时会显示待发送内容。确认后发送两份全量表和两份格式对齐表。也可双击“发送结果邮件.command”，或运行 `.venv/bin/python -m scripts.send_report_email --gui`。在窗口按日期或批次号搜索，从下拉框选择已完成批次，核对附件和收件人后确认发送。工作簿被改动后需要重新生成并通过交付校验。发送结果在批次的 `email-attempts/` 下，发信失败时可以单独补发。

终端默认选最近完成的批次。使用 `--workflow "runs/批次目录"` 指定其他批次，`--no-cc` 让本次抄送留空。可用 `--split single` 或 `--split both` 临时覆盖已保存的发送方式。

```sh
.venv/bin/python -m scripts.send_report_email --dry-run
.venv/bin/python -m scripts.send_report_email
```

预览只检查配置和附件，正式发送前在终端输入 `YES`。测试可加 `--test-to test@example.com`，将示例地址替换为经授权的测试收件人。测试邮件使用保存的发送方式、清空抄送并加上「测试」主题标记。

分两封时，每封分别记录 Message-ID 与 SMTP 结果。再次选同一批次及收件配置时，已被 SMTP 接受的分组会跳过，结果未知或部分拒收的分组需先核对收件，再通过 `--split raw` 或 `--split formatted` 单独处理。SMTP 接受状态与收件人邮箱确认分别核对。

## 独立验证

```sh
.venv/bin/python -m unittest tests.test_report_email tests.test_unified_local
.venv/bin/python -m scripts.probe_local_model --out data/model-probe
.venv/bin/python -m scripts.self_test
```

上述单元测试覆盖发信与续跑，使用模拟 SMTP 和临时文件。完整单元测试、`self_test` 及 Excel 回归还需要另行取得获授权的测试输入和工作簿。离线自测结果位于 `data/self-test-*`，实际报价以本次采集的来源核验为准。

## 文件边界

仓库包含代码、配置示例、操作文档和测试代码。历史测试数据需另行准备。工作簿、邮件和本机配置、运行记录、历史、缓存、模型权重与虚拟环境保存在本地。克隆后重新安装依赖。

## 文档索引

- 日常操作与续跑请看 [一键运行说明](一键运行说明.md)、[统一运行入口](docs/本机统一运行.md)和[每周运行与历史滚动](docs/每周运行与历史滚动.md)。
- 安装和复现请看 [GitHub 下载与复现](docs/GitHub下载与复现.md)、[本机文字流程](docs/完全本地运行说明.md)、[模型配置](docs/本地模型操作说明.md)和[文字缓存](docs/节省token与后续扩展.md)。
- 业务核对请看 [SGD 人工修正](docs/SGD全行覆盖与人工修正.md)、[调研格式](docs/调研格式整理程序.md)和[彩虹格式](docs/彩虹表格式整理程序.md)。
