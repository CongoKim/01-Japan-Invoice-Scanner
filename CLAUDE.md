# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 开发命令

```bash
cp .env.example .env       # 填写 API Keys
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

访问 http://localhost:8000（前端 SPA + API 同端口）

## 架构

**请求流程：**

```
上传 ZIP → POST /upload → 创建 task_id → 后台 process_task()
                                         ↓
                              extract_zip → 逐文件处理
                                         ↓
                         PDF → render_pdf_pages (PyMuPDF)
                         Image → 直接读取字节
                                         ↓
                      多页 PDF → gemini.detect_multi_invoice() 判断是否含多张发票
                                         ↓
                         Gemini + OpenAI 并行提取 (asyncio.gather)
                                         ↓
                         compare_and_arbitrate() → 字段一致则合并
                                              → 不一致则 Claude Opus 仲裁
                                         ↓
                              write_excel() → output.xlsx
```

**关键模块：**

| 路径 | 职责 |
|------|------|
| `app/main.py` | FastAPI 入口，挂载路由与静态文件 |
| `app/config.py` | `pydantic-settings` 读取 `.env`，含模型名称与并发数 |
| `app/models/task.py` | 内存 `task_store`，任务状态（extracting/analyzing/processing/paused/writing_excel/done/error/cancelled） |
| `app/models/invoice.py` | `InvoiceFields` Pydantic 模型，定义所有提取字段 |
| `app/services/orchestrator.py` | 核心流水线，`process_task()` / `resume_task()` |
| `app/services/comparator.py` | 对比 Gemini / OpenAI 结果，分歧时调用 Claude 仲裁 |
| `app/services/ai_clients/` | `base.py` 定义抽象接口；`gemini.py` / `openai_client.py` / `claude.py` 各自实现 |
| `app/routers/progress.py` | SSE 实时进度推送 (`GET /progress/{task_id}`) |
| `app/routers/task_control.py` | 暂停 / 继续 / 取消接口 |
| `app/routers/settings.py` | API Keys 管理（`POST/GET /api/settings/keys`），更新后调用 `orchestrator.reset_clients()` |
| `app/static/index.html` | 单文件前端 SPA，API Keys 面板将 key 存入 `localStorage` 并推送到服务端 |

**AI 模型配置（`app/config.py`）：**

- Gemini 2.5 Pro — 主提取 + 多发票检测
- GPT-4o — 并行主提取
- Claude Opus — 仲裁裁判（仅当两模型结果不一致时调用）

**并发控制：** `asyncio.Semaphore(settings.max_concurrency)`，默认 10，可通过环境变量 `MAX_CONCURRENCY` 覆盖。

**重试策略：** `_call_with_retry()` 指数退避，最多 3 次。

## 部署

已配置 `Dockerfile` + `railway.toml`，Railway 中需设置：`GEMINI_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`。

## 语言

所有回复使用**简体中文**。
