"""认证范围拆分与承接领域服务。

设计为只依赖 Python 标准库的事件溯源内核：所有状态变更都以“事实”追加保存，
查询视图由事实流折叠得到。覆盖的核心规则：

* 冻结：原证书的服务组件、人群分层、地点、责任方、有效期在拆分开启时整体快照；
* 承接：各新主体分别声明承接项、控制措施、缺口、人员与设施；
* 候选：系统按证据的组件/人群/地点适用范围提出可继承证据候选，并给出不适用原因；
* 双确认：评审员确认体验覆盖、合规人员确认责任与投诉承接后才可签发；
* 隔离阻断：无法分割的严重事件、跨主体共用人员、共享设施只暂停相关组件范围；
* 独立版本：每个承接主体得到相互独立的证书与版本，部分通过、整改、撤销均追加事实；
* 幂等与复审：相同承接方案重放不重复签发，内容变化使旧确认失效并进入复审；
* 宣传与消费者：新宣传引用必须钉住明确且有效的版本；已购用户保留原承诺，
  责任变更以通知事实记录，并可从任一新证书回溯原证据、未继承原因与消费者处置。
"""

import hashlib
import json
import threading
from datetime import datetime, timezone


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class DomainError(Exception):
    """领域规则冲突，code 供调用方与 HTTP 层稳定引用。"""

    def __init__(self, code, message, details=None, http_status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.http_status = http_status


class CertificationService:
    """以追加事实为唯一事实来源的认证拆分服务。"""

    def __init__(self, clock=_utc_now):
        self._clock = clock
        self._facts = []
        self._idem = {}
        self._lock = threading.RLock()
        self._rebuild()

    # ------------------------------------------------------------------ 事实层

    def _append(self, fact_type, **payload):
        fact = {
            "seq": len(self._facts) + 1,
            "at": self._clock(),
            "type": fact_type,
            "payload": payload,
        }
        self._facts.append(fact)
        return fact

    def facts(self):
        """返回全部追加事实（审计流）。"""
        with self._lock:
            return [dict(fact, payload=dict(fact["payload"])) for fact in self._facts]

    def _rebuild(self):
        """从事实流重建投影。数据量小，每次命令后全量折叠以保证一致。"""
        state = {
            "orgs": {},
            "certs": {},
            "evidence": {},
            "splits": {},
            "consumers": {},
            "promotions": {},
        }
        for fact in self._facts:
            self._apply(state, fact)
        self._state = state

    def _apply(self, state, fact):
        p = fact["payload"]
        kind = fact["type"]
        apply_fn = getattr(self, f"_apply_{kind}", None)
        if apply_fn is not None:
            apply_fn(state, fact)

    # ---------------------------------------------------------- 事实折叠处理器

    def _apply_OrganizationRegistered(self, state, fact):
        p = fact["payload"]
        state["orgs"][p["org_id"]] = {"org_id": p["org_id"], "name": p["name"]}

    def _apply_CertificateRegistered(self, state, fact):
        p = fact["payload"]
        state["certs"][p["cert_id"]] = {
            "cert_id": p["cert_id"],
            "holder_org_id": p["holder_org_id"],
            "valid_from": p["valid_from"],
            "valid_to": p["valid_to"],
            "components": [dict(c) for c in p["components"]],
            "status": "active",
            "split_id": None,
            "parent_cert_id": None,
            "split_successor_orgs": [],
            "versions": [],
            "revoked_components": [],
        }

    def _apply_EvidenceRecorded(self, state, fact):
        p = fact["payload"]
        state["evidence"][p["evidence_id"]] = dict(p)

    def _apply_PurchaseRecorded(self, state, fact):
        p = fact["payload"]
        consumer = state["consumers"].setdefault(
            p["consumer_id"], {"consumer_id": p["consumer_id"], "purchases": [], "notices": []}
        )
        consumer["purchases"].append(
            {
                "cert_id": p["cert_id"],
                "items": [dict(i) for i in p["items"]],
                "recorded_seq": fact["seq"],
            }
        )

    def _apply_SplitOpened(self, state, fact):
        p = fact["payload"]
        cert = state["certs"][p["cert_id"]]
        cert["status"] = "frozen"
        cert["split_id"] = p["split_id"]
        state["splits"][p["split_id"]] = {
            "split_id": p["split_id"],
            "cert_id": p["cert_id"],
            "opened_seq": fact["seq"],
            "frozen_at": fact["at"],
            "snapshot": {
                "holder_org_id": cert["holder_org_id"],
                "valid_from": cert["valid_from"],
                "valid_to": cert["valid_to"],
                "components": [dict(c) for c in cert["components"]],
            },
            "declarations": {},
            "incidents": [],
            "issuances": {},
        }

    def _apply_SuccessionDeclared(self, state, fact):
        self._ingest_declaration(state, fact, revision=1)

    def _apply_SuccessionAmended(self, state, fact):
        self._ingest_declaration(state, fact, revision=fact["payload"]["revision"])

    def _ingest_declaration(self, state, fact, revision):
        p = fact["payload"]
        split = state["splits"][p["split_id"]]
        # 修订重置确认状态（内容变化需复审），但整改是追加事实，历史必须保留。
        previous = split["declarations"].get(p["successor_org_id"])
        rectifications = list(previous["rectifications"]) if previous else []
        split["declarations"][p["successor_org_id"]] = {
            "successor_org_id": p["successor_org_id"],
            "revision": revision,
            "declared_seq": fact["seq"],
            "items": [dict(i) for i in p["items"]],
            "experience": None,
            "responsibility": None,
            "rectifications": rectifications,
        }

    def _apply_IncidentRegistered(self, state, fact):
        p = fact["payload"]
        state["splits"][p["split_id"]]["incidents"].append(
            {
                "incident_id": p["incident_id"],
                "component_ids": list(p["component_ids"]),
                "inseparable": p["inseparable"],
                "description": p["description"],
                "status": "open",
                "registered_seq": fact["seq"],
                "closed": None,
            }
        )

    def _apply_IncidentClosed(self, state, fact):
        p = fact["payload"]
        for incident in state["splits"][p["split_id"]]["incidents"]:
            if incident["incident_id"] == p["incident_id"]:
                incident["status"] = "closed"
                incident["closed"] = {"resolution": p["resolution"], "seq": fact["seq"], "at": fact["at"]}

    def _apply_RectificationRecorded(self, state, fact):
        p = fact["payload"]
        decl = state["splits"][p["split_id"]]["declarations"][p["successor_org_id"]]
        decl["rectifications"].append(
            {
                "rectification_id": p["rectification_id"],
                "component_id": p["component_id"],
                "gap": p.get("gap"),
                "blocker_id": p.get("blocker_id"),
                "action": p["action"],
                "status": p["status"],
                "seq": fact["seq"],
                "at": fact["at"],
            }
        )

    def _apply_ExperienceCoverageConfirmed(self, state, fact):
        p = fact["payload"]
        decl = state["splits"][p["split_id"]]["declarations"][p["successor_org_id"]]
        decl["experience"] = {
            "reviewer": p["reviewer"],
            "revision": p["declaration_revision"],
            "decisions": [dict(d) for d in p["decisions"]],
            "seq": fact["seq"],
        }

    def _apply_ResponsibilityHandoverConfirmed(self, state, fact):
        p = fact["payload"]
        decl = state["splits"][p["split_id"]]["declarations"][p["successor_org_id"]]
        decl["responsibility"] = {
            "officer": p["compliance_officer"],
            "revision": p["declaration_revision"],
            "decisions": [dict(d) for d in p["decisions"]],
            "seq": fact["seq"],
        }

    def _apply_CertificationIssued(self, state, fact):
        self._ingest_version(state, fact, first=True)

    def _apply_CertificationVersionAdded(self, state, fact):
        self._ingest_version(state, fact, first=False)

    def _ingest_version(self, state, fact, first):
        p = fact["payload"]
        split = state["splits"][p["split_id"]]
        issuance = split["issuances"].setdefault(
            p["successor_org_id"], {"cert_id": p["cert_id"], "last_plan_hash": None}
        )
        issuance["last_plan_hash"] = p["plan_hash"]
        if p["successor_org_id"] not in state["certs"][split["cert_id"]]["split_successor_orgs"]:
            state["certs"][split["cert_id"]]["split_successor_orgs"].append(p["successor_org_id"])
        if first:
            state["certs"][p["cert_id"]] = {
                "cert_id": p["cert_id"],
                "holder_org_id": p["successor_org_id"],
                "valid_from": fact["at"],
                "valid_to": p["valid_to"],
                "components": [],
                "status": "active",
                "split_id": p["split_id"],
                "parent_cert_id": split["cert_id"],
                "split_successor_orgs": [],
                "versions": [],
            }
        version = {
            "version": p["version"],
            "status": "active",
            "issued_seq": fact["seq"],
            "issued_at": fact["at"],
            "included_components": [dict(c) for c in p["included_components"]],
            "suspended_components": [dict(c) for c in p["suspended_components"]],
            "pending_components": [dict(c) for c in p["pending_components"]],
            "revoked_components": [dict(c) for c in p.get("revoked_components", [])],
            "plan_hash": p["plan_hash"],
            "partially_revoked_components": [],
        }
        cert = state["certs"][p["cert_id"]]
        cert["versions"].append(version)

    def _apply_CertificationSuperseded(self, state, fact):
        p = fact["payload"]
        for version in state["certs"][p["cert_id"]]["versions"]:
            if version["version"] == p["version"]:
                version["status"] = "superseded"
                version["superseded_seq"] = fact["seq"]

    def _apply_CertificationRevoked(self, state, fact):
        p = fact["payload"]
        cert = state["certs"][p["cert_id"]]
        cert["status"] = "revoked"
        cert["revoked"] = {"reason": p["reason"], "seq": fact["seq"], "at": fact["at"]}
        for version in cert["versions"]:
            if version["status"] == "active":
                version["status"] = "revoked"

    def _apply_CertificationScopePartiallyRevoked(self, state, fact):
        p = fact["payload"]
        cert = state["certs"][p["cert_id"]]
        # 部分撤销是证书级追加事实：对所有后续版本持续有效。
        cert.setdefault("revoked_components", [])
        for component_id in p["component_ids"]:
            if component_id not in cert["revoked_components"]:
                cert["revoked_components"].append(component_id)
        for version in cert["versions"]:
            if version["version"] == p["version"] and version["status"] == "active":
                version["partially_revoked_components"].extend(p["component_ids"])

    def _apply_PromotionReferenced(self, state, fact):
        p = fact["payload"]
        state["promotions"][p["promotion_id"]] = {
            "promotion_id": p["promotion_id"],
            "cert_id": p["cert_id"],
            "version": p["version"],
            "component_ids": list(p.get("component_ids", [])),
            "registered_seq": fact["seq"],
            "registered_at": fact["at"],
        }

    def _apply_ResponsibilityChangeNoticed(self, state, fact):
        p = fact["payload"]
        consumer = state["consumers"].setdefault(
            p["consumer_id"], {"consumer_id": p["consumer_id"], "purchases": [], "notices": []}
        )
        consumer["notices"].append(
            {
                "notice_id": p["notice_id"],
                "split_id": p["split_id"],
                "source_cert_id": p["source_cert_id"],
                "new_cert_id": p["new_cert_id"],
                "version": p["version"],
                "component_ids": list(p["component_ids"]),
                "old_responsible_party_org_id": p["old_responsible_party_org_id"],
                "new_responsible_party_org_id": p["new_responsible_party_org_id"],
                "commitments_preserved": True,
                "delivered": False,
                "seq": fact["seq"],
                "at": fact["at"],
            }
        )

    def _apply_NoticeDelivered(self, state, fact):
        p = fact["payload"]
        for consumer in state["consumers"].values():
            for notice in consumer["notices"]:
                if notice["notice_id"] == p["notice_id"]:
                    notice["delivered"] = True
                    notice["delivered_seq"] = fact["seq"]

    # ------------------------------------------------------------- 命令分发

    def command(self, name, params=None, idempotency_key=None):
        """执行一条命令；同 idempotency_key 的重放返回首次结果。"""
        params = dict(params or {})
        with self._lock:
            if idempotency_key is not None:
                cached = self._idem.get(idempotency_key)
                if cached is not None:
                    if cached["command"] != name:
                        raise DomainError(
                            "IDEMPOTENCY_KEY_CONFLICT",
                            "幂等键已绑定其他命令",
                            {"key": idempotency_key, "bound_command": cached["command"]},
                            http_status=409,
                        )
                    result = dict(cached["result"])
                    result["idempotent_replay"] = True
                    return result
            handler = getattr(self, f"_cmd_{name}", None)
            if handler is None:
                raise DomainError("UNKNOWN_COMMAND", f"未知命令: {name}", http_status=404)
            result = handler(params)
            if idempotency_key is not None:
                self._idem[idempotency_key] = {"command": name, "result": dict(result)}
            return result

    # --------------------------------------------------------- 基础登记命令

    def _cmd_register_organization(self, p):
        org_id, name = self._need(p, "org_id"), self._need(p, "name")
        if org_id in self._state["orgs"]:
            raise DomainError("ORG_EXISTS", "机构已登记", {"org_id": org_id}, 409)
        self._append("OrganizationRegistered", org_id=org_id, name=name)
        self._rebuild()
        return {"org_id": org_id}

    def _cmd_register_certificate(self, p):
        cert_id = self._need(p, "cert_id")
        holder = self._need(p, "holder_org_id")
        self._require_org(holder)
        if cert_id in self._state["certs"]:
            raise DomainError("CERT_EXISTS", "证书已登记", {"cert_id": cert_id}, 409)
        components = self._need(p, "components")
        if not components:
            raise DomainError("EMPTY_SCOPE", "证书至少包含一个服务组件")
        clean_components = []
        seen = set()
        for component in components:
            cid = self._need(component, "id", context="component")
            if cid in seen:
                raise DomainError("DUPLICATE_COMPONENT", "组件重复", {"component_id": cid}, 409)
            seen.add(cid)
            party = self._need(component, "responsible_party_org_id", context=f"component {cid}")
            self._require_org(party)
            clean_components.append(
                {
                    "id": cid,
                    "name": self._need(component, "name", context=f"component {cid}"),
                    "populations": [self._clean_segment(x, "population") for x in self._need(component, "populations", context=f"component {cid}")],
                    "locations": [self._clean_segment(x, "location") for x in self._need(component, "locations", context=f"component {cid}")],
                    "responsible_party_org_id": party,
                }
            )
        self._append(
            "CertificateRegistered",
            cert_id=cert_id,
            holder_org_id=holder,
            valid_from=self._need(p, "valid_from"),
            valid_to=self._need(p, "valid_to"),
            components=clean_components,
        )
        self._rebuild()
        return {"cert_id": cert_id, "status": "active", "components": [c["id"] for c in clean_components]}

    def _cmd_record_evidence(self, p):
        evidence_id = self._need(p, "evidence_id")
        cert_id = self._need(p, "cert_id")
        cert = self._require_cert(cert_id)
        component_id = self._need(p, "component_id")
        self._require_component(cert, component_id)
        if evidence_id in self._state["evidence"]:
            raise DomainError("EVIDENCE_EXISTS", "证据已登记", {"evidence_id": evidence_id}, 409)
        populations = self._need(p, "populations")
        locations = self._need(p, "locations")
        self._append(
            "EvidenceRecorded",
            evidence_id=evidence_id,
            cert_id=cert_id,
            component_id=component_id,
            populations=list(populations),
            locations=list(locations),
            summary=self._need(p, "summary"),
        )
        self._rebuild()
        return {"evidence_id": evidence_id}

    def _cmd_register_severe_incident(self, p):
        split = self._require_split(self._need(p, "split_id"))
        incident_id = self._need(p, "incident_id")
        component_ids = self._need(p, "component_ids")
        if any(i["incident_id"] == incident_id for i in split["incidents"]):
            raise DomainError("INCIDENT_EXISTS", "严重事件已登记", {"incident_id": incident_id}, 409)
        snapshot_ids = {c["id"] for c in split["snapshot"]["components"]}
        unknown = [c for c in component_ids if c not in snapshot_ids]
        if unknown:
            raise DomainError("UNKNOWN_COMPONENT", "事件关联了快照之外的组件", {"component_ids": unknown}, 404)
        self._append(
            "IncidentRegistered",
            split_id=split["split_id"],
            incident_id=incident_id,
            component_ids=list(component_ids),
            inseparable=bool(p.get("inseparable", False)),
            description=self._need(p, "description"),
        )
        self._rebuild()
        return {"incident_id": incident_id, "status": "open"}

    def _cmd_close_severe_incident(self, p):
        split = self._require_split(self._need(p, "split_id"))
        incident_id = self._need(p, "incident_id")
        incident = self._find_incident(split, incident_id)
        if incident["status"] == "closed":
            raise DomainError("INCIDENT_ALREADY_CLOSED", "严重事件已闭环", {"incident_id": incident_id}, 409)
        self._append(
            "IncidentClosed",
            split_id=split["split_id"],
            incident_id=incident_id,
            resolution=self._need(p, "resolution"),
        )
        self._rebuild()
        return {"incident_id": incident_id, "status": "closed"}

    def _cmd_record_purchase(self, p):
        consumer_id = self._need(p, "consumer_id")
        cert_id = self._need(p, "cert_id")
        cert = self._require_cert(cert_id)
        items = self._need(p, "items")
        clean = []
        for item in items:
            cid = self._need(item, "component_id")
            self._require_component(cert, cid)
            clean.append({"component_id": cid, "commitment": self._need(item, "commitment")})
        self._append(
            "PurchaseRecorded",
            consumer_id=consumer_id,
            cert_id=cert_id,
            items=clean,
        )
        self._rebuild()
        return {"consumer_id": consumer_id, "purchased_components": [i["component_id"] for i in clean]}

    # ------------------------------------------------------------- 拆分与承接

    def _cmd_open_split(self, p):
        cert = self._require_cert(self._need(p, "cert_id"))
        split_id = self._need(p, "split_id")
        if split_id in self._state["splits"]:
            raise DomainError("SPLIT_EXISTS", "拆分已存在", {"split_id": split_id}, 409)
        if cert["status"] != "active":
            raise DomainError(
                "CERT_NOT_FREEZABLE",
                "只有当前有效的证书可以冻结拆分",
                {"cert_id": cert["cert_id"], "status": cert["status"]},
                409,
            )
        self._append(
            "SplitOpened",
            split_id=split_id,
            cert_id=cert["cert_id"],
            holder_org_id=cert["holder_org_id"],
            valid_from=cert["valid_from"],
            valid_to=cert["valid_to"],
            components=[dict(c) for c in cert["components"]],
        )
        self._rebuild()
        return {
            "split_id": split_id,
            "cert_id": cert["cert_id"],
            "frozen_components": [c["id"] for c in cert["components"]],
            "status": "frozen",
        }

    def _cmd_declare_succession(self, p):
        split = self._require_split(self._need(p, "split_id"))
        org_id = self._need(p, "successor_org_id")
        self._require_org(org_id)
        if org_id in split["declarations"]:
            raise DomainError(
                "DECLARATION_EXISTS",
                "承接声明已存在，内容变化请使用修订",
                {"successor_org_id": org_id},
                409,
            )
        items = self._clean_items(split, p)
        self._assert_components_unclaimed(split, org_id, items)
        facts = [("SuccessionDeclared", self._declaration_payload(split["split_id"], org_id, items))]
        self._flush_declaration_change(split, org_id, items, facts)
        return self._declaration_result(split, org_id)

    def _cmd_amend_succession_declaration(self, p):
        split = self._require_split(self._need(p, "split_id"))
        org_id = self._need(p, "successor_org_id")
        decl = split["declarations"].get(org_id)
        if decl is None:
            raise DomainError("NO_DECLARATION", "承接声明不存在", {"successor_org_id": org_id}, 404)
        items = self._clean_items(split, p)
        self._assert_components_unclaimed(split, org_id, items)
        revision = decl["revision"] + 1
        facts = [
            (
                "SuccessionAmended",
                {
                    **self._declaration_payload(split["split_id"], org_id, items),
                    "revision": revision,
                    "previous_revision": decl["revision"],
                },
            )
        ]
        # 声明内容变化：先前评审与合规确认随修订失效，需要重新进入复审。
        self._flush_declaration_change(split, org_id, items, facts)
        result = self._declaration_result(split, org_id)
        result["revision"] = revision
        result["review_required"] = True
        return result

    def _cmd_record_rectification(self, p):
        split, decl = self._require_declaration(p)
        rectification_id = self._need(p, "rectification_id")
        component_id = self._need(p, "component_id")
        item = next((i for i in decl["items"] if i["component_id"] == component_id), None)
        if item is None:
            raise DomainError("UNKNOWN_COMPONENT", "组件不在承接声明中", {"component_id": component_id}, 404)
        existing = [r for r in decl["rectifications"] if r["rectification_id"] == rectification_id]
        if existing:
            raise DomainError("RECTIFICATION_EXISTS", "整改记录已存在", {"rectification_id": rectification_id}, 409)
        gap = p.get("gap")
        if gap is not None and gap not in item["gaps"]:
            raise DomainError("UNKNOWN_GAP", "整改目标不在声明缺口内", {"gap": gap}, 404)
        status = p.get("status", "completed")
        if status not in ("completed", "follow_up_required"):
            raise DomainError("INVALID_STATUS", "整改状态只能是 completed 或 follow_up_required")
        self._append(
            "RectificationRecorded",
            split_id=split["split_id"],
            successor_org_id=decl["successor_org_id"],
            rectification_id=rectification_id,
            component_id=component_id,
            gap=gap,
            blocker_id=p.get("blocker_id"),
            action=self._need(p, "action"),
            status=status,
        )
        self._rebuild()
        return {
            "rectification_id": rectification_id,
            "component_id": component_id,
            "status": status,
        }

    def _cmd_confirm_experience_coverage(self, p):
        split, decl = self._require_declaration(p)
        decisions = self._need(p, "decisions")
        by_component = {d["component_id"]: d for d in decisions}
        clean_decisions = []
        candidates = self._candidates(split, decl)
        for item in decl["items"]:
            cid = item["component_id"]
            decision = by_component.get(cid)
            if decision is None or decision.get("decision", "confirmed") != "confirmed":
                raise DomainError(
                    "EXPERIENCE_NOT_CONFIRMED",
                    "每个承接组件都需要评审员明确确认体验覆盖",
                    {"component_id": cid},
                )
            unresolved_gaps = [g for g in item["gaps"] if g not in decision.get("resolved_gaps", [])]
            if unresolved_gaps:
                raise DomainError(
                    "UNRESOLVED_GAPS",
                    "体验确认前声明缺口必须全部关闭或整改",
                    {"component_id": cid, "gaps": unresolved_gaps},
                    409,
                )
            accepted = list(decision.get("accepted_evidence_ids", []))
            applicable = set(candidates[cid]["applicable"])
            if not accepted:
                raise DomainError(
                    "NO_INHERITED_EVIDENCE",
                    "确认体验覆盖必须指明承接的适用证据",
                    {"component_id": cid},
                )
            invalid = [e for e in accepted if e not in applicable]
            if invalid:
                raise DomainError(
                    "EVIDENCE_NOT_APPLICABLE",
                    "只能接受系统判定适用范围内的证据",
                    {"component_id": cid, "evidence_ids": invalid},
                    409,
                )
            clean_decisions.append(
                {
                    "component_id": cid,
                    "decision": "confirmed",
                    "accepted_evidence_ids": accepted,
                    "resolved_gaps": list(decision.get("resolved_gaps", [])),
                    "note": decision.get("note", ""),
                }
            )
        self._append(
            "ExperienceCoverageConfirmed",
            split_id=split["split_id"],
            successor_org_id=decl["successor_org_id"],
            reviewer=self._need(p, "reviewer"),
            declaration_revision=decl["revision"],
            decisions=clean_decisions,
        )
        self._rebuild()
        return {"split_id": split["split_id"], "successor_org_id": decl["successor_org_id"], "experience_confirmed": True}

    def _cmd_confirm_responsibility_handover(self, p):
        split, decl = self._require_declaration(p)
        decisions = self._need(p, "decisions")
        by_component = {d["component_id"]: d for d in decisions}
        clean_decisions = []
        for item in decl["items"]:
            cid = item["component_id"]
            decision = by_component.get(cid)
            if decision is None or decision.get("decision", "confirmed") != "confirmed":
                raise DomainError(
                    "RESPONSIBILITY_NOT_CONFIRMED",
                    "每个承接组件都需要合规人员明确确认责任承接",
                    {"component_id": cid},
                )
            channel = decision.get("complaint_channel")
            if not channel:
                raise DomainError(
                    "MISSING_COMPLAINT_CHANNEL",
                    "责任确认必须说明投诉承接渠道",
                    {"component_id": cid},
                )
            if not decision.get("liability_acknowledged", False):
                raise DomainError(
                    "LIABILITY_NOT_ACKNOWLEDGED",
                    "承接主体必须明确承接相应责任",
                    {"component_id": cid},
                )
            clean_decisions.append(
                {
                    "component_id": cid,
                    "decision": "confirmed",
                    "complaint_channel": channel,
                    "liability_acknowledged": True,
                }
            )
        self._append(
            "ResponsibilityHandoverConfirmed",
            split_id=split["split_id"],
            successor_org_id=decl["successor_org_id"],
            compliance_officer=self._need(p, "compliance_officer"),
            declaration_revision=decl["revision"],
            decisions=clean_decisions,
        )
        self._rebuild()
        return {"split_id": split["split_id"], "successor_org_id": decl["successor_org_id"], "responsibility_confirmed": True}

    def _cmd_issue_certification(self, p):
        split = self._require_split(self._need(p, "split_id"))
        org_id = self._need(p, "successor_org_id")
        decl = split["declarations"].get(org_id)
        if decl is None:
            raise DomainError("NO_DECLARATION", "承接声明不存在", {"successor_org_id": org_id}, 404)

        experience = decl["experience"]
        responsibility = decl["responsibility"]
        stale = []
        if experience is None or experience["revision"] != decl["revision"]:
            stale.append("experience_coverage")
        if responsibility is None or responsibility["revision"] != decl["revision"]:
            stale.append("responsibility_handover")
        if stale:
            raise DomainError(
                "CONFIRMATIONS_STALE",
                "承接内容已变化，确认结果失效，需重新进入复审",
                {"successor_org_id": org_id, "stale_confirmations": stale},
                409,
            )

        blockers = self._active_blockers(split)
        exp_by_component = {d["component_id"]: d for d in experience["decisions"]}
        included, suspended, pending, excluded_revoked = [], [], [], []
        prior_revoked = set()
        prior_issuance = split["issuances"].get(org_id)
        if prior_issuance is not None:
            prior_revoked = set(self._state["certs"][prior_issuance["cert_id"]].get("revoked_components", []))
        snapshot = {c["id"]: c for c in split["snapshot"]["components"]}
        for item in decl["items"]:
            cid = item["component_id"]
            hit_blockers = [b for b in blockers if cid in b["component_ids"]]
            if hit_blockers:
                suspended.append(
                    {
                        "component_id": cid,
                        "reasons": [
                            {"kind": b["kind"], "blocker_id": b["blocker_id"], "detail": b["detail"]}
                            for b in hit_blockers
                        ],
                    }
                )
                continue
            # 证书级部分撤销跨版本生效：该范围不得在新版本中恢复。
            if cid in prior_revoked:
                excluded_revoked.append({"component_id": cid, "reason": "certification_scope_revoked"})
                continue
            # 确认存在且 revision 已校验；此处保留逐组件防御。
            if cid not in exp_by_component:
                pending.append({"component_id": cid, "reason": "experience_coverage"})
                continue
            accepted = exp_by_component[cid]["accepted_evidence_ids"]
            included.append(
                {
                    "component_id": cid,
                    "name": snapshot[cid]["name"],
                    "populations": list(item["populations"]),
                    "locations": list(item["locations"]),
                    "controls": list(item["controls"]),
                    "responsible_party_org_id": org_id,
                    "inherited_evidence_ids": accepted,
                    "valid_to": split["snapshot"]["valid_to"],
                }
            )

        if not included:
            raise DomainError(
                "NO_ELIGIBLE_SCOPE",
                "没有满足签发条件的组件，相关范围保持暂停、待确认或已撤销",
                {"suspended": suspended, "pending": pending, "revoked": excluded_revoked},
                409,
            )

        plan_hash = self._plan_hash(decl, blockers, experience, responsibility)
        issuance = split["issuances"].get(org_id)

        cert_id = p.get("cert_id")
        if issuance is not None:
            cert_id = issuance["cert_id"]
            prior_cert = self._state["certs"][cert_id]
            if prior_cert["status"] == "revoked":
                raise DomainError(
                    "CERT_REVOKED",
                    "证书已撤销，整改后请重新发起承接拆分而不是在旧证书上签发",
                    {"cert_id": cert_id},
                    409,
                )
            # 撤销之外，相同承接方案重放不重复签发。
            if issuance["last_plan_hash"] == plan_hash:
                return {
                    "replayed": True,
                    "cert_id": cert_id,
                    "version": prior_cert["versions"][-1]["version"],
                    "included_components": [c["component_id"] for c in included],
                    "suspended_components": suspended,
                    "pending_components": pending,
                    "revoked_components": excluded_revoked,
                }
            version = len(prior_cert["versions"]) + 1
        else:
            if cert_id is None:
                cert_id = f"cert-{split['cert_id']}-{org_id}-{hashlib.sha1(plan_hash.encode()).hexdigest()[:8]}"
            if cert_id in self._state["certs"]:
                raise DomainError("CERT_EXISTS", "证书编号已被占用", {"cert_id": cert_id}, 409)
            version = 1

        common_payload = dict(
            split_id=split["split_id"],
            source_cert_id=split["cert_id"],
            successor_org_id=org_id,
            cert_id=cert_id,
            version=version,
            valid_to=split["snapshot"]["valid_to"],
            included_components=included,
            suspended_components=suspended,
            pending_components=pending,
            revoked_components=excluded_revoked,
            plan_hash=plan_hash,
        )
        if version == 1:
            self._append("CertificationIssued", **common_payload)
        else:
            prior_cert = self._state["certs"][cert_id]
            prior_version = prior_cert["versions"][-1]["version"]
            self._append("CertificationVersionAdded", **common_payload)
            self._append(
                "CertificationSuperseded",
                cert_id=cert_id,
                version=prior_version,
                new_version=version,
            )
        self._append_consumer_notices(split, cert_id, version, org_id, included)
        self._rebuild()
        return {
            "replayed": False,
            "cert_id": cert_id,
            "version": version,
            "included_components": [c["component_id"] for c in included],
            "suspended_components": suspended,
            "pending_components": pending,
            "plan_hash": plan_hash,
        }

    def _cmd_revoke_certification(self, p):
        cert = self._require_cert(self._need(p, "cert_id"))
        reason = self._need(p, "reason")
        component_ids = p.get("component_ids")
        if cert["parent_cert_id"] is None:
            raise DomainError("UNSUPPORTED_REVOCATION_TARGET", "撤销仅作用于拆分后承接主体的新证书", 409)
        if component_ids:
            version = cert["versions"][-1]
            if version["status"] != "active":
                raise DomainError("NO_ACTIVE_VERSION", "当前版本不可部分撤销", {"status": version["status"]}, 409)
            # 可撤销范围为该承接证书任一时点覆盖过的组件（证书级撤销跨版本有效）。
            ever_included = {
                item["component_id"]
                for ver in cert["versions"]
                for item in ver["included_components"]
            }
            unknown = [c for c in component_ids if c not in ever_included]
            if unknown:
                raise DomainError("UNKNOWN_COMPONENT", "组件不在该证书承接范围内", {"component_ids": unknown}, 404)
            already = set(cert.get("revoked_components", []))
            fresh = [c for c in component_ids if c not in already]
            if not fresh:
                return {"cert_id": cert["cert_id"], "replayed": True,
                        "partially_revoked_components": sorted(already)}
            self._append(
                "CertificationScopePartiallyRevoked",
                cert_id=cert["cert_id"],
                version=version["version"],
                component_ids=list(fresh),
                reason=reason,
            )
            self._rebuild()
            already.update(fresh)
            return {"cert_id": cert["cert_id"], "version": version["version"],
                    "partially_revoked_components": sorted(already)}
        if cert["status"] == "revoked":
            return {"cert_id": cert["cert_id"], "replayed": True, "status": "revoked"}
        self._append("CertificationRevoked", cert_id=cert["cert_id"], reason=reason)
        self._rebuild()
        return {"cert_id": cert["cert_id"], "status": "revoked"}

    def _cmd_register_promotion_reference(self, p):
        promotion_id = self._need(p, "promotion_id")
        if promotion_id in self._state["promotions"]:
            raise DomainError("PROMOTION_EXISTS", "宣传引用已登记", {"promotion_id": promotion_id}, 409)
        cert = self._require_cert(self._need(p, "cert_id"))
        version = self._need(p, "version")
        component_ids = list(p.get("component_ids", []))
        if cert["status"] == "frozen":
            raise DomainError("CERT_FROZEN", "冻结中的原证书不得用于新宣传引用，请指向承接后的明确版本", 409)
        if cert["status"] == "revoked":
            raise DomainError("CERT_REVOKED", "证书已撤销，不得用于宣传引用", 409)
        target = next((v for v in cert["versions"] if v["version"] == version), None)
        if target is None:
            raise DomainError("UNKNOWN_VERSION", "宣传引用必须指向存在的明确版本", {"version": version}, 404)
        if target["status"] != "active":
            raise DomainError(
                "VERSION_NOT_ACTIVE",
                "宣传引用必须指向当前有效版本",
                {"version": version, "status": target["status"]},
                409,
            )
        available = {c["component_id"] for c in target["included_components"]} - set(
            cert.get("revoked_components", [])
        )
        unknown = [c for c in component_ids if c not in available]
        if unknown:
            raise DomainError("SCOPE_UNAVAILABLE", "引用范围已撤销或不在该版本内", {"component_ids": unknown}, 409)
        self._append(
            "PromotionReferenced",
            promotion_id=promotion_id,
            cert_id=cert["cert_id"],
            version=version,
            component_ids=component_ids,
        )
        self._rebuild()
        return {"promotion_id": promotion_id, "cert_id": cert["cert_id"], "version": version, "status": "registered"}

    def _cmd_acknowledge_notice(self, p):
        consumer_id = self._need(p, "consumer_id")
        notice_id = self._need(p, "notice_id")
        consumer = self._state["consumers"].get(consumer_id)
        notice = None
        if consumer is not None:
            notice = next((n for n in consumer["notices"] if n["notice_id"] == notice_id), None)
        if notice is None:
            raise DomainError("NOTICE_NOT_FOUND", "责任变更通知不存在", {"notice_id": notice_id}, 404)
        if notice["delivered"]:
            return {"notice_id": notice_id, "replayed": True, "delivered": True}
        self._append("NoticeDelivered", consumer_id=consumer_id, notice_id=notice_id)
        self._rebuild()
        return {"notice_id": notice_id, "delivered": True}

    # ----------------------------------------------------- 承接过程的内部规则

    def _flush_declaration_change(self, split, org_id, items, facts):
        """写入声明变化，同时把跨主体共用人员/设施的自动阻断差异追加为事实。"""
        before = self._shared_blockers(split)
        for fact_type, payload in facts:
            self._append(fact_type, **payload)
        self._rebuild()
        new_split = self._state["splits"][split["split_id"]]
        new_decl = dict(new_split["declarations"][org_id])
        new_decl["items"] = items
        after = self._shared_blockers({**new_split, "declarations": {**new_split["declarations"], org_id: new_decl}})
        before_ids = {b["blocker_id"]: b for b in before}
        after_ids = {b["blocker_id"]: b for b in after}
        for blocker_id, blocker in after_ids.items():
            if blocker_id not in before_ids:
                self._append(
                    "BlockerDetected",
                    split_id=split["split_id"],
                    blocker_id=blocker_id,
                    kind=blocker["kind"],
                    component_ids=blocker["component_ids"],
                    detail=blocker["detail"],
                )
        for blocker_id in before_ids:
            if blocker_id not in after_ids:
                self._append(
                    "BlockerCleared",
                    split_id=split["split_id"],
                    blocker_id=blocker_id,
                )
        self._rebuild()

    def _shared_blockers(self, split):
        """计算跨承接主体共用人员/设施导致的阻断（相关组件范围暂停）。"""
        usage = {"staff": {}, "facility": {}}
        for org_id, decl in split["declarations"].items():
            for item in decl["items"]:
                for staff_id in item["staff_ids"]:
                    usage["staff"].setdefault(staff_id, {}).setdefault(org_id, set()).add(item["component_id"])
                for facility_id in item["facility_ids"]:
                    usage["facility"].setdefault(facility_id, {}).setdefault(org_id, set()).add(item["component_id"])
        blockers = []
        labels = {"staff": ("shared_staff", "共用人员"), "facility": ("shared_facility", "共享设施")}
        for usage_kind, (kind, label) in labels.items():
            for resource_id, by_org in usage[usage_kind].items():
                if len(by_org) >= 2:
                    component_ids = sorted({cid for comps in by_org.values() for cid in comps})
                    blockers.append(
                        {
                            "blocker_id": f"auto:{kind}:{resource_id}",
                            "kind": kind,
                            "component_ids": component_ids,
                            "detail": f"承接主体之间存在{label} {resource_id}",
                        }
                    )
        return blockers

    def _active_blockers(self, split):
        blockers = []
        for incident in split["incidents"]:
            if incident["status"] == "open":
                blockers.append(
                    {
                        "blocker_id": f"incident:{incident['incident_id']}",
                        "kind": "severe_incident" + ("_inseparable" if incident["inseparable"] else ""),
                        "component_ids": list(incident["component_ids"]),
                        "detail": incident["description"],
                    }
                )
        blockers.extend(self._shared_blockers(split))
        return blockers

    def _candidates(self, split, decl):
        """按证据适用范围（组件/人群/地点）提出可继承候选与不适用原因。"""
        result = {}
        cert_id = split["cert_id"]
        evidence_by_component = {}
        for evidence in self._state["evidence"].values():
            if evidence["cert_id"] == cert_id:
                evidence_by_component.setdefault(evidence["component_id"], []).append(evidence)
        for item in decl["items"]:
            applicable, non_applicable = [], []
            for evidence in evidence_by_component.get(item["component_id"], []):
                missing_populations = [x for x in item["populations"] if x not in evidence["populations"]]
                missing_locations = [x for x in item["locations"] if x not in evidence["locations"]]
                reasons = []
                if missing_populations:
                    reasons.append({"code": "evidence_population_gap", "segments": missing_populations})
                if missing_locations:
                    reasons.append({"code": "evidence_location_gap", "segments": missing_locations})
                if reasons:
                    non_applicable.append({"evidence_id": evidence["evidence_id"], "reasons": reasons})
                else:
                    applicable.append(evidence["evidence_id"])
            result[item["component_id"]] = {"applicable": sorted(applicable), "non_applicable": non_applicable}
        return result

    def _plan_hash(self, decl, blockers, experience, responsibility):
        plan = {
            "revision": decl["revision"],
            "items": sorted(
                [
                    {
                        "component_id": i["component_id"],
                        "populations": sorted(i["populations"]),
                        "locations": sorted(i["locations"]),
                        "controls": sorted(i["controls"]),
                        "gaps": sorted(i["gaps"]),
                        "staff_ids": sorted(i["staff_ids"]),
                        "facility_ids": sorted(i["facility_ids"]),
                    }
                    for i in decl["items"]
                ],
                key=lambda x: x["component_id"],
            ),
            "blockers": sorted(
                [
                    {"id": b["blocker_id"], "components": sorted(b["component_ids"])}
                    for b in blockers
                ],
                key=lambda x: x["id"],
            ),
            "experience": {
                "reviewer": experience["reviewer"],
                "decisions": sorted(
                    [
                        {
                            "component_id": d["component_id"],
                            "accepted": sorted(d["accepted_evidence_ids"]),
                            "resolved_gaps": sorted(d["resolved_gaps"]),
                        }
                        for d in experience["decisions"]
                    ],
                    key=lambda x: x["component_id"],
                ),
            },
            "responsibility": {
                "officer": responsibility["officer"],
                "decisions": sorted(
                    [
                        {
                            "component_id": d["component_id"],
                            "complaint_channel": d["complaint_channel"],
                        }
                        for d in responsibility["decisions"]
                    ],
                    key=lambda x: x["component_id"],
                ),
            },
        }
        return hashlib.sha256(_canonical(plan).encode("utf-8")).hexdigest()

    def _append_consumer_notices(self, split, new_cert_id, version, org_id, included):
        # 责任变更对“消费者×承接证书×组件”只记录一次；新版本若纳入此前暂停的组件，
        # 该组件在此处首次产生通知。
        existing = {
            (fact["payload"]["new_cert_id"], cid, fact["payload"]["consumer_id"])
            for fact in self._facts
            if fact["type"] == "ResponsibilityChangeNoticed"
            for cid in fact["payload"]["component_ids"]
        }
        snapshot = {c["id"]: c for c in split["snapshot"]["components"]}
        for consumer in self._state["consumers"].values():
            owned = set()
            for purchase in consumer["purchases"]:
                if purchase["cert_id"] == split["cert_id"]:
                    owned.update(i["component_id"] for i in purchase["items"])
            touched = sorted(
                c["component_id"]
                for c in included
                if c["component_id"] in owned
            )
            fresh = [cid for cid in touched if (new_cert_id, cid, consumer["consumer_id"]) not in existing]
            if not fresh:
                continue
            old_parties = sorted({snapshot[cid]["responsible_party_org_id"] for cid in fresh})
            notice_id = f"notice-{len(self._facts) + 1}"
            self._append(
                "ResponsibilityChangeNoticed",
                notice_id=notice_id,
                consumer_id=consumer["consumer_id"],
                split_id=split["split_id"],
                source_cert_id=split["cert_id"],
                new_cert_id=new_cert_id,
                version=version,
                component_ids=fresh,
                old_responsible_party_org_id=old_parties[0] if len(old_parties) == 1 else old_parties,
                new_responsible_party_org_id=org_id,
            )

    # ------------------------------------------------------------- 查询视图

    def get_certificate(self, cert_id):
        cert = self._require_cert(cert_id)
        return self._certificate_view(cert)

    def get_versions(self, cert_id):
        cert = self._require_cert(cert_id)
        return {"cert_id": cert_id, "versions": self._certificate_view(cert)["versions"]}

    def _certificate_view(self, cert):
        versions = []
        for version in cert["versions"]:
            versions.append(
                {
                    "version": version["version"],
                    "status": version["status"],
                    "issued_at": version["issued_at"],
                    "included_components": [dict(c) for c in version["included_components"]],
                    "suspended_components": [dict(c) for c in version["suspended_components"]],
                    "pending_components": [dict(c) for c in version["pending_components"]],
                    "partially_revoked_components": list(version["partially_revoked_components"]),
                    "revoked_components": [dict(c) for c in version["revoked_components"]],
                    "plan_hash": version["plan_hash"],
                }
            )
        current_version = versions[-1]["version"] if versions else None
        return {
            "cert_id": cert["cert_id"],
            "holder_org_id": cert["holder_org_id"],
            "status": cert["status"],
            "valid_from": cert["valid_from"],
            "valid_to": cert["valid_to"],
            "split_id": cert["split_id"],
            "parent_cert_id": cert["parent_cert_id"],
            "current_version": current_version,
            "versions": versions,
            "successor_orgs": list(cert["split_successor_orgs"]),
            "revoked_components": list(cert.get("revoked_components", [])),
        }

    def get_split(self, split_id):
        split = self._require_split(split_id)
        active_blockers = self._active_blockers(split)
        successors = []
        for org_id, decl in split["declarations"].items():
            candidates = self._candidates(split, decl)
            issuance = split["issuances"].get(org_id)
            successors.append(
                {
                    "successor_org_id": org_id,
                    "revision": decl["revision"],
                    "items": [dict(i) for i in decl["items"]],
                    "evidence_candidates": candidates,
                    "experience_confirmation": decl["experience"],
                    "responsibility_confirmation": decl["responsibility"],
                    "rectifications": [dict(r) for r in decl["rectifications"]],
                    "issuance": None
                    if issuance is None
                    else {"cert_id": issuance["cert_id"], "last_plan_hash": issuance["last_plan_hash"]},
                }
            )
        return {
            "split_id": split["split_id"],
            "source_cert_id": split["cert_id"],
            "status": self._split_status(split),
            "frozen_at": split["frozen_at"],
            "frozen": dict(split["snapshot"]),
            "successors": successors,
            "active_blockers": active_blockers,
            "incidents": [dict(i) for i in split["incidents"]],
        }

    def _split_status(self, split):
        if not split["declarations"]:
            return "frozen"
        if split["issuances"]:
            return "issued"
        if any(d["experience"] or d["responsibility"] for d in split["declarations"].values()):
            return "in_review"
        return "declarations_open"

    def lineage(self, cert_id):
        """从任一（新或原）证书追溯原证据、未继承原因与消费者处置。"""
        cert = self._require_cert(cert_id)
        if cert["parent_cert_id"] is not None:
            source = self._state["certs"][cert["parent_cert_id"]]
            split = self._state["splits"][cert["split_id"]]
            inheritance, excluded_scope = [], []
            for version in cert["versions"]:
                for component in version["included_components"]:
                    inheritance.append(
                        {
                            "version": version["version"],
                            "component_id": component["component_id"],
                            "inherited_evidence": [
                                dict(self._state["evidence"][eid])
                                for eid in component["inherited_evidence_ids"]
                            ],
                        }
                    )
                for component in version["suspended_components"]:
                    excluded_scope.append(
                        {
                            "version": version["version"],
                            "component_id": component["component_id"],
                            "state": "suspended",
                            "reasons": component["reasons"],
                        }
                    )
                for component in version["pending_components"]:
                    excluded_scope.append(
                        {
                            "version": version["version"],
                            "component_id": component["component_id"],
                            "state": "pending",
                            "reason": component["reason"],
                        }
                    )
            not_inherited = []
            decl = split["declarations"].get(cert["holder_org_id"])
            if decl is not None:
                accepted_by_component = {
                    component["component_id"]: set(component["inherited_evidence_ids"])
                    for version in cert["versions"]
                    for component in version["included_components"]
                    if version["status"] in ("active", "superseded", "revoked")
                }
                for cid, candidate in self._candidates(split, decl).items():
                    for evidence in candidate["non_applicable"]:
                        not_inherited.append(
                            {
                                "component_id": cid,
                                "evidence_id": evidence["evidence_id"],
                                "reason_code": "outside_applicability",
                                "reasons": evidence["reasons"],
                            }
                        )
                    accepted = accepted_by_component.get(cid, set())
                    for evidence_id in candidate["applicable"]:
                        if evidence_id not in accepted:
                            not_inherited.append(
                                {
                                    "component_id": cid,
                                    "evidence_id": evidence_id,
                                    "reason_code": "applicable_but_not_accepted",
                                    "reasons": [{"code": "reviewer_did_not_accept"}],
                                }
                            )
            return {
                "cert_id": cert_id,
                "split_id": split["split_id"],
                "source_cert_id": source["cert_id"],
                "source_snapshot": dict(split["snapshot"]),
                "current": self._certificate_view(cert),
                "inheritance": inheritance,
                "not_inherited_evidence": not_inherited,
                "excluded_scope": excluded_scope,
                "consumers": self._consumer_dispositions(source, cert["cert_id"]),
            }
        # 从原证书方向查看全部后继。
        result = {
            "cert_id": cert_id,
            "split_id": cert["split_id"],
            "status": cert["status"],
            "successors": [],
            "consumers": self._consumer_dispositions(cert, None),
        }
        if cert["split_id"]:
            split = self._state["splits"][cert["split_id"]]
            for org_id, issuance in split["issuances"].items():
                child = self._state["certs"][issuance["cert_id"]]
                result["successors"].append(self._certificate_view(child))
        return result

    def _consumer_dispositions(self, source_cert, new_cert_id):
        dispositions = []
        for consumer in self._state["consumers"].values():
            purchases = [
                {"cert_id": pur["cert_id"], "items": [dict(i) for i in pur["items"]]}
                for pur in consumer["purchases"]
                if pur["cert_id"] == source_cert["cert_id"]
            ]
            if not purchases:
                continue
            notices = [
                dict(n)
                for n in consumer["notices"]
                if n["source_cert_id"] == source_cert["cert_id"]
                and (new_cert_id is None or n["new_cert_id"] == new_cert_id)
            ]
            dispositions.append(
                {
                    "consumer_id": consumer["consumer_id"],
                    "original_purchases": purchases,
                    "commitments_preserved": True,
                    "responsibility_changes": notices,
                }
            )
        return dispositions

    def get_consumer(self, consumer_id):
        consumer = self._state["consumers"].get(consumer_id)
        if consumer is None:
            raise DomainError("CONSUMER_NOT_FOUND", "消费者不存在", {"consumer_id": consumer_id}, 404)
        return {
            "consumer_id": consumer_id,
            "purchases": [dict(pur) for pur in consumer["purchases"]],
            "responsibility_changes": [dict(n) for n in consumer["notices"]],
        }

    def get_promotion(self, promotion_id):
        promotion = self._state["promotions"].get(promotion_id)
        if promotion is None:
            raise DomainError("PROMOTION_NOT_FOUND", "宣传引用不存在", {"promotion_id": promotion_id}, 404)
        cert = self._state["certs"][promotion["cert_id"]]
        version = next(v for v in cert["versions"] if v["version"] == promotion["version"])
        revoked_components = set(cert.get("revoked_components", []))
        promoted = set(promotion["component_ids"])
        if cert["status"] == "revoked" or version["status"] == "revoked":
            validity = "revoked"
        elif promoted & revoked_components:
            # 证书级部分撤销跨版本生效：即使引用的是旧版本，被点名组件的宣传也不再成立。
            validity = "scope_partially_revoked"
        elif version["status"] == "superseded":
            validity = "superseded"
        elif cert["status"] == "frozen":
            validity = "frozen"
        else:
            validity = "valid"
        return {**dict(promotion), "validity": validity}

    # ------------------------------------------------------------- 校验小工具

    def _clean_items(self, split, p):
        raw_items = self._need(p, "items")
        if not raw_items:
            raise DomainError("EMPTY_DECLARATION", "承接声明至少包含一个组件")
        snapshot = {c["id"]: c for c in split["snapshot"]["components"]}
        clean, seen = [], set()
        for raw in raw_items:
            cid = self._need(raw, "component_id")
            if cid not in snapshot:
                raise DomainError("UNKNOWN_COMPONENT", "承接组件不在冻结快照中", {"component_id": cid}, 404)
            if cid in seen:
                raise DomainError("DUPLICATE_COMPONENT", "承接组件重复", {"component_id": cid}, 409)
            seen.add(cid)
            component = snapshot[cid]
            populations = list(raw.get("populations", [seg["id"] for seg in component["populations"]]))
            locations = list(raw.get("locations", [seg["id"] for seg in component["locations"]]))
            allowed_pop = {seg["id"] for seg in component["populations"]}
            allowed_loc = {seg["id"] for seg in component["locations"]}
            extra_pop = [x for x in populations if x not in allowed_pop]
            extra_loc = [x for x in locations if x not in allowed_loc]
            if extra_pop or extra_loc:
                raise DomainError(
                    "SCOPE_OUTSIDE_FROZEN_BOUNDARY",
                    "承接范围不得超出冻结的人群分层或地点",
                    {"populations": extra_pop, "locations": extra_loc},
                    409,
                )
            clean.append(
                {
                    "component_id": cid,
                    "populations": populations,
                    "locations": locations,
                    "controls": list(raw.get("controls", [])),
                    "gaps": list(raw.get("gaps", [])),
                    "staff_ids": list(raw.get("staff_ids", [])),
                    "facility_ids": list(raw.get("facility_ids", [])),
                }
            )
        return clean

    def _assert_components_unclaimed(self, split, org_id, items):
        claimed = {}
        for other_org, decl in split["declarations"].items():
            if other_org != org_id:
                for item in decl["items"]:
                    claimed[item["component_id"]] = other_org
        conflicts = {i["component_id"]: claimed[i["component_id"]] for i in items if i["component_id"] in claimed}
        if conflicts:
            raise DomainError(
                "COMPONENT_CLAIMED_BY_OTHER",
                "同一组件不能由两个承接主体同时承接",
                {"conflicts": conflicts},
                409,
            )

    def _require_declaration(self, p):
        split = self._require_split(self._need(p, "split_id"))
        org_id = self._need(p, "successor_org_id")
        decl = split["declarations"].get(org_id)
        if decl is None:
            raise DomainError("NO_DECLARATION", "承接声明不存在", {"successor_org_id": org_id}, 404)
        return split, decl

    def _declaration_payload(self, split_id, org_id, items):
        return {
            "split_id": split_id,
            "successor_org_id": org_id,
            "items": [dict(i) for i in items],
        }

    def _declaration_result(self, split, org_id):
        live_split = self._state["splits"][split["split_id"]]
        decl = live_split["declarations"][org_id]
        blockers = self._active_blockers(live_split)
        return {
            "split_id": split["split_id"],
            "successor_org_id": org_id,
            "revision": decl["revision"],
            "items": [dict(i) for i in decl["items"]],
            "evidence_candidates": self._candidates(live_split, decl),
            "active_blockers": blockers,
        }

    def _require_org(self, org_id):
        org = self._state["orgs"].get(org_id)
        if org is None:
            raise DomainError("ORG_NOT_FOUND", "机构不存在", {"org_id": org_id}, 404)
        return org

    def _require_cert(self, cert_id):
        cert = self._state["certs"].get(cert_id)
        if cert is None:
            raise DomainError("CERT_NOT_FOUND", "证书不存在", {"cert_id": cert_id}, 404)
        return cert

    def _require_split(self, split_id):
        split = self._state["splits"].get(split_id)
        if split is None:
            raise DomainError("SPLIT_NOT_FOUND", "拆分不存在", {"split_id": split_id}, 404)
        return split

    def _require_component(self, cert, component_id):
        component = next((c for c in cert["components"] if c["id"] == component_id), None)
        if component is None:
            raise DomainError("UNKNOWN_COMPONENT", "组件不存在", {"component_id": component_id}, 404)
        return component

    def _find_incident(self, split, incident_id):
        incident = next((i for i in split["incidents"] if i["incident_id"] == incident_id), None)
        if incident is None:
            raise DomainError("INCIDENT_NOT_FOUND", "严重事件不存在", {"incident_id": incident_id}, 404)
        return incident

    @staticmethod
    def _clean_segment(segment, kind):
        if isinstance(segment, str):
            return {"id": segment, "name": segment}
        return {"id": CertificationService._need(segment, "id", context=kind), "name": segment.get("name", segment["id"])}

    @staticmethod
    def _need(params, key, context=None):
        if key not in params or params[key] in (None, ""):
            where = f"（{context}）" if context else ""
            raise DomainError("MISSING_PARAM", f"缺少必填参数: {key}{where}", {"param": key})
        return params[key]
