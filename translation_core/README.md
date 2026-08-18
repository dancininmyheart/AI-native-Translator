# Format Translator Core

面向 Microsoft Word 和 PowerPoint 的格式保真翻译服务核心。输入支持
`.doc`、`.docx`、`.ppt`、`.pptx`；旧格式会先转换为 OOXML，最终只输出
`.docx` 或 `.pptx`。

## 处理流程

```text
接收 Office 文件
  -> 旧格式安全转换
  -> OOXML 文本块提取与占位符保护
  -> 第三方翻译 Provider
  -> 语义 QA
  -> 原位置回填
  -> Word/PPT 原生结构与版式 QA
  -> 多轮诊断、修复、验收或回滚
  -> Office 文件、QA 报告、计时报告、manifest
```

整个流程不生成、比较或交付其他文档格式。

## 支持能力

- Word：段落、表格、页眉页脚、脚注尾注、分页属性、section 页面几何。
- PowerPoint：幻灯片、文本框、表格文字、shape 坐标、画布边界、媒体和关系。
- PowerPoint 专用修复适配器：shape 边界、字号、文本框溢出、对象坐标恢复。
- Windows PowerPoint 实际布局门禁：通过本机 PowerPoint
  `TextFrame2.TextRange` 测量文本边界。
- Linux LibreOffice 门禁：用隔离的 headless LibreOffice 打开并重新保存
  PPTX，验证生产环境可打开性；shape 级溢出继续使用原生容量估算。
- 实际布局结果会与可信原件对比，只阻断候选中新出现或显著恶化的溢出，
  避免把原件既有排版问题误判成翻译缺陷。
- 数字、日期、URL、邮箱等受保护内容可逆恢复。
- 每轮修复写入新文件；源文件和上一轮候选不可覆盖。
- 确定性修复只接受质量分严格提升且不引入新阻断问题的候选；真实模型复验证据见
  [`PI_AGENT_VALIDATION.md`](PI_AGENT_VALIDATION.md)。
- 完整语义/版式 QA 与最终包验证分开持久化；包验证不得覆盖公开 QA 报告或降低其问题级别。
- 支持第三方 OpenAI-compatible 翻译、语义 QA、text-fit、诊断和修复规划。

## Pi Agent API

Pi Sidecar 将 Skill 编排限制在受控的预检、翻译、QA、修复、验证和最终化工具内，并把长任务、
取消、幂等键和状态持久化包装为 HTTP API。接口请求示例见
[`pi_agent/README.md`](pi_agent/README.md)，Linux 生产部署见
[`PI_AGENT_DEPLOY.md`](PI_AGENT_DEPLOY.md)。

服务边界保持不变：接收 DOC/DOCX/PPT/PPTX，旧格式由 LibreOffice 归一化，只输出 DOCX/PPTX，
不包含 PDF 路径。生产部署通过 Redis 租约将多副本活跃文档任务总数硬限制为 20；约 30 分钟是容量目标，QA 未解决时返回
`NEEDS_REVIEW`，而不是强行发布。

## CLI

完整翻译：

```powershell
format-translator translate input.docx `
  --output-dir .\jobs `
  --target zh `
  --llm-env .\.env `
  --enable-processing-agent `
  --qa-repair-rounds 2
```

PowerPoint 使用同一个命令：

```powershell
format-translator translate input.pptx `
  --output-dir .\jobs `
  --target zh `
  --llm-env .env
```

单独运行 QA/修复 Agent：

```powershell
format-translator qa-repair source.docx translated.docx `
  --work-dir .\jobs\qa `
  --max-rounds 2
```

```powershell
format-translator qa-repair source.pptx translated.pptx `
  --work-dir .\jobs\qa `
  --max-rounds 2
```

检查文件：

```powershell
format-translator inspect input.pptx
```

## Python API

```python
from pathlib import Path

from format_translator.models import JobSpec
from format_translator.pipeline import translate_document

report = translate_document(
    JobSpec(
        job_id="job-001",
        input_path=Path("input.docx"),
        output_dir=Path("jobs"),
        source_language="auto",
        # auto: 中文源文档→英文；所有非中文源文档→中文
        target_language="auto",
        max_qa_repair_rounds=2,
    ),
    provider=my_translation_provider,
    processing_provider=my_processing_provider,
)
```

QA Agent 的详细状态、动作白名单和报告结构见
[`QA_REPAIR_AGENT.md`](QA_REPAIR_AGENT.md)。

PPTX 修复适配器也可以单独调用：

```python
from format_translator import execute_pptx_repair_adapter

result = execute_pptx_repair_adapter(
    "source.pptx",
    "translated.pptx",
    "translated-repaired.pptx",
    actions=[
        {
            "action": "resolve_text_overflow",
            "slide": 3,
            "shape_id": "9",
            "min_font_size_pt": 8,
        },
        {
            "action": "restore_shape_coordinates",
            "slide": 4,
            "shape_id": "12",
        },
    ],
    audit_path="pptx-repair-audit.json",
    rendered_layout_required=True,
)
```

适配器使用从 1 开始的幻灯片编号和 OOXML shape ID。它只接受白名单动作，
始终写入新文件，并在交付前以可信原件重新执行 PPTX QA。Windows 生产部署
建议启用 `rendered_layout_required=True`；此时 PowerPoint COM 不可用、超时或
实际文本仍溢出都会拒绝本轮输出并保留失败审计。

## Linux 生产环境

Linux 默认自动选择 `libreoffice` 后端，不加载 PowerShell 或 COM。它执行：

- `.doc` / `.ppt` 到 OOXML 的旧格式转换；
- LibreOffice headless 可打开性门禁，失败时即使
  `rendered_layout_required=False` 也会 fail-closed；
- DOCX/PPTX 原生结构、文本、坐标、提示词泄漏和估算容量 QA；
- 确定性 QA Agent 修复与回滚。

LibreOffice 后端只声明 `package_openability` capability，不能伪装成
PowerPoint `shape_text_bounds`。Linux 若设置
`rendered_layout_required=True`，任务会以
`PPTX_RENDERED_LAYOUT_CAPABILITY_INSUFFICIENT` 拒绝。必须获得 PowerPoint
精确边界时，应把 Windows COM runner 部署为独立内网服务并注入
`layout_runner`。

```bash
export FORMAT_TRANSLATOR_PPTX_LAYOUT_BACKEND=libreoffice
export FORMAT_TRANSLATOR_SOFFICE_PATH=/usr/bin/libreoffice
export FORMAT_TRANSLATOR_LIBREOFFICE_MAX_WORKERS=3
export FORMAT_TRANSLATOR_LIBREOFFICE_LOCK_DIRECTORY=/var/lock/format-translator/libreoffice
```

容器定义和字体、共享锁卷说明见
[`deploy/linux/README.md`](deploy/linux/README.md)。

发布包可在 `translation_core` 目录生成：

```powershell
.\deploy\package-pi-agent-release.ps1 -ReleaseTag 20260804
```

生成的归档解开后包含同级 `translation_core/` 和 `document-translation/`，并排除 `.env`、
`node_modules`、`dist`、验证工作区、缓存及测试 Office/PDF 文件，同时生成 SHA-256 文件。

## PowerPoint COM 并发

默认的实际布局 runner 使用同一台 Windows 主机共享的 3 槽有界调度。业务层
可以同时接收 20 个任务，但整台主机最多同时执行 3 个 PowerPoint COM 测量，
其余任务排队，不会同时启动 20 个 PowerPoint。

可通过服务进程环境变量调整：

```powershell
$env:FORMAT_TRANSLATOR_PPTX_COM_MAX_WORKERS = "3"
$env:FORMAT_TRANSLATOR_PPTX_COM_QUEUE_TIMEOUT_SECONDS = "120"
$env:FORMAT_TRANSLATOR_PPTX_COM_LOCK_DIRECTORY = ".\service-data\pptx-com-locks"
```

每次 QA 报告都会记录 `pool_max_workers`、`pool_scope`、`queue_wait_ms`、
`queue_timeout_seconds`、`worker_id` 和 `worker_slot`。队列等待超时会让
provider 进入 unavailable；在 `rendered_layout_required=True` 时按 error
拒绝任务。

默认锁目录位于系统临时目录，因此同一主机上的多个 API 进程共享总槽位。
跨主机部署仍应把 COM runner 收敛到独立 Windows 服务，或使用外部分布式
信号量；本地文件锁不提供跨主机协调。

## 验证状态

真实 DOCX/PPTX 工具链、真实 `.doc` / `.ppt` 归一化和真实 Pi 模型调用已有本地证据；既验证了
残留问题安全返回 `NEEDS_REVIEW`，也完成了“质量严格改善后晋级”和“warning 透明发布”的真实
模型复验。本机没有
Docker/Redis，因此 Linux Compose、Redis 恢复和真实基础设施下的 20 文档并发压测尚未执行，
不能视为生产验收通过。证据、用时和预生产检查项见
[`PI_AGENT_VALIDATION.md`](PI_AGENT_VALIDATION.md)。

生产 Sidecar 已实现启动恢复：先核验所有持久化 `QUEUED` 任务的请求绑定、输入哈希和原子工作区，
再使用稳定任务 ID 幂等重入 BullMQ；保留的同 ID BullMQ 终态任务会先被替换，暂态存储错误不会
把任务永久写成失败，从而避免“状态已写入但进程在入队前退出”留下永久悬空任务。
