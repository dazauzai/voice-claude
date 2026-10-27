# 出新图

每张图 = 一份请求 JSON（提示词 + 参考图）交给 `generate_codex.py`，它用本机登录的 Codex（ChatGPT 账号）自带的生图画出 PNG，
不需要 API key。一张 30–90 秒。

```bash
python scripts/mascot/generate_codex.py --check            # Codex 装了、登录了？
python scripts/mascot/generate_codex.py --request req.json --output out.png --timeout 420
```

请求 JSON 的样子见 `make_requests_u.py`（标准图）；要新表情或姿势，复制它、改 `TAKES`/`SHEETS` 里的那一段，参考图永远用
`voiceclaude/assets/mascot_chibi.png`（身份、画风、服装）和 `voiceclaude/assets/fish.png`（伙伴），提示词主体用
`docs/mascot/design.md` 第 5 节那段，别改身份部分，只改姿势、表情、构图。

`make_icon.py` 从标准图裁圆头像并生成 `.ico`。`history/` 是定稿前每一轮的脚本，记录形象是怎么一步步定下来的。
