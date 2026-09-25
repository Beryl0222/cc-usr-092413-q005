"use strict";

const { spawnSync } = require("node:child_process");

// 运行全部契约模块：基础服务、拆分承接领域规则、HTTP 适配。
const result = spawnSync(
  "python3",
  ["-m", "unittest", "discover", "-v", "-p", "*_contract.py"],
  { stdio: "inherit" }
);
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
