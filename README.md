# 银发服务体验认证

服务用于记录不同银发人群的真实体验证据、适用范围和认证变更，避免笼统宣传掩盖差异。

当前版本在基础服务之上提供**认证范围拆分与承接能力**：当原认证覆盖的服务被拆给多个运营主体时，
系统不允许“整张证书直接转过去”，而是冻结原责任边界，由各主体分别承接、按证据适用范围继承、
双确认后签发相互独立的新认证版本。

运行 `python3 service.py --check` 可核对服务配置；执行 `python3 service.py --port 8000` 后访问
`/health` 可确认服务身份。

## 业务规则

* **冻结**：开启拆分时快照原证书的服务组件、人群分层、地点、责任方、有效期，原证书进入 `frozen`，
  承接声明不得超出冻结边界，同一组件不能由两个主体同时承接。
* **承接声明与证据候选**：每个新主体分别声明承接项、控制措施、缺口、人员与设施；
  系统按证据的组件 / 人群 / 地点适用范围给出可继承候选，并对不适用证据标注人群或地点缺口原因。
* **双确认门**：评审员逐组件确认体验覆盖（必须承接适用证据、缺口必须经整改关闭）；
  合规人员逐组件确认责任承接（须明确承接责任并给出投诉渠道）。声明内容修订后旧确认失效，须复审。
* **阻断隔离**：无法分割的严重事件、跨主体共用人员、共享设施只让**相关组件范围暂停**，
  不拖累无关组件；事件闭环或共用解除后相关范围可进入后续版本。
* **独立版本与追加事实**：每个承接主体拿到相互独立的新证书；部分通过、整改、部分撤销、撤销、
  取代都以追加事实保存，不抹除历史。
* **幂等与复审**：相同承接方案重放不重复签发；内容变化（控制、范围、人员设施、阻断、确认）
  产生新方案哈希，旧确认失效并进入复审，复审通过后签发新版本、旧版本标记 `superseded`。
* **宣传引用**：新宣传必须指向明确且当前有效的证书版本；冻结、撤销、被取代、范围部分撤销
  都会在引用查询中反映出来。
* **消费者保护**：已购买用户保留原承诺；责任变更按“消费者 × 新证书 × 组件”记录一次并可确认送达，
  新版本不重复打扰。
* **溯源**：从任一新证书可追到原证书冻结快照、继承的原证据、未继承原因（不适用 / 评审未采纳）、
  暂停与撤销范围、历史消费者处置；从原证书可列出全部独立后继。

## HTTP 接口

* `POST /rpc`：请求体 `{"command": <命令>, "params": {...}, "idempotency_key"?: <键>}`。
  成功返回 `{"ok": true, "result": ...}`；领域冲突返回
  `{"ok": false, "error": {"code", "message", "details"}}` 与对应 4xx 状态。
* 查询：`GET /certificates/<id>`、`/certificates/<id>/versions`、
  `/certificates/<id>/lineage`、`/splits/<id>`、`/consumers/<id>`、
  `/promotions/<id>`、`/facts`（追加事实审计流）、`/health`。

命令包括：`register_organization`、`register_certificate`、`record_evidence`、`record_purchase`、
`open_split`、`register_severe_incident`、`close_severe_incident`、`declare_succession`、
`amend_succession_declaration`、`record_rectification`、`confirm_experience_coverage`、
`confirm_responsibility_handover`、`issue_certification`、`revoke_certification`、
`register_promotion_reference`、`acknowledge_notice`。

领域内核（`certification.py`）与 HTTP 适配（`service.py`）分离，仅依赖 Python 标准库；
状态保存在进程内存中，适合本地联调与契约测试。

## 测试与构建

执行完整测试（基础服务 + 领域契约 + HTTP 契约，共 33 个用例）：

```bash
npm test
```

手工端到端演示（康养旅居服务拆分为场地公司与线上健康公司）：

```bash
python3 smoke.py
```

执行编译检查：

```bash
python3 -m compileall -q .
```

两条命令都可在单个 Linux 应用容器内直接运行，不需要额外服务。
