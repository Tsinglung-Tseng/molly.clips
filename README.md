# molly.clips

监控 Obsidian `Clippings/` 目录，自动用 `claude -p /obs-note` 整理笔记并写入 Vault 根目录，处理完毕后删除原文件。

## 工作原理

```
Clippings/raw-note.md
        |
        | watchdog 检测到新文件（防抖 5s）
        v
claude -p "/obs-note 请整理以下内容：<内容>"
  (cwd = Vault 根目录，--dangerously-skip-permissions)
        |
        v
Vault/Category_Topic_English.md  <- 整理后的笔记
Clippings/raw-note.md            <- 已删除
```

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `MOLLY_VAULT_PATH` | Yes | Obsidian Vault 根目录的绝对路径 |
| `MOLLY_CLAUDE_BIN` 或 `CLAUDE_BIN` | Yes | `claude` CLI 可执行文件的绝对路径 |
| `MOLLY_DEBOUNCE_SEC` | No | 防抖等待秒数，默认 `5.0` |

任一必填项未设置，启动时直接报错退出。

## 运行

```bash
uv sync
python watcher.py
```

## 注意

- 处理超时上限 900s（单篇笔记）
- claude 退出码非 0 时原文件保留，不会误删
- 遇到 Rate Limit 自动指数退避重试（最多 4 次），放弃后文件保留原地
- obs-note 的输出规范（文件命名、frontmatter、双向链接）由对应的 Claude Code skill 定义
