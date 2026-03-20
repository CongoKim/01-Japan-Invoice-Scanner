# Japan Invoice Scanner

日本发票扫描识别系统。上传包含发票图片/PDF的ZIP文件，通过多AI模型（Gemini + GPT-4o + Claude）交叉验证提取发票字段，输出Excel。

## 功能

- 支持图片格式：JPEG, PNG, TIFF, BMP, WebP, HEIC
- 支持PDF：结构化PDF和扫描件PDF
- 多页PDF智能判断（自动识别是否包含多张发票）
- 三模型交叉验证（Gemini 2.5 Pro + GPT-4o 并行 → Claude Opus 仲裁）
- 暂停/继续/取消功能
- 断线自动恢复
- 实时进度显示（SSE）

## 提取字段

文件名称 / 相手先 / 登録番号 / 発行日 / 業務内容 / 币种 / 報酬額 / 消費税額 / 総額 / 發票號 / 消费税核定 / 源泉税金額

## 安装

```bash
cp .env.example .env
# 填写 API Keys
pip install -r requirements.txt
```

## 运行

```bash
uvicorn app.main:app --reload --port 8000
```

访问 http://localhost:8000

## 部署 (Railway)

项目已配置 Dockerfile 和 railway.toml，直接连接 GitHub 仓库即可部署。

需要在 Railway 中设置环境变量：
- `GEMINI_API_KEY`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
