# 文档审计清单

> `python scripts/audit.py check` 的机械检查之外的人工审计要点。文档体系变更或定期审计时过一遍。

- [ ] **单一事实源**：同一事实只在一处定义（SKILL.md = 运行规则；dispatch-patterns §退出码契约 = 退出码；TELEMETRY_FIELDS = 遥测 schema）。索引文档只导航不复制正文。
- [ ] **入口一致**：AGENTS.md / CLAUDE.md / GEMINI.md 三份一致（`agent_links.py check`）。
- [ ] **无沙箱误述**：全部文档不得把 `--tools`/`-nc`/`-na`/`--forbid-paths`/路径审计描述成安全沙箱或访问控制。
- [ ] **不变量锚点**：SKILL.md 含 3 次上限、预算授权、幂等禁自动重派、知识截止、converge 排除、工具面声明（verify_pisr_skill.py 全绿）。
- [ ] **交叉链接有效**：refs↔SKILL.md↔docs 互链无死链（audit.py dead-links）。
- [ ] **STRUCTURE 收录**：新建文档已登记定位与读取时机；无独立长期职责的内容不单独建文档。
- [ ] **过时清理**：被替换的表述删除而非注释保留；迁移历史交给 Git。
- [ ] **测试同步**：行为契约（退出码、遥测字段、白名单 fail-closed）变更后 tests/ 与 refs/dispatch-patterns.md 同步。
