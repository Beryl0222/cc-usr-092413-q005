"""认证范围拆分与承接领域服务。

原证书在拆分前被冻结：服务组件、人群分层、地点、责任方和有效期全部固定为
不可变快照。新主体分别提交承接声明（承接项、控制措施、缺口），系统依据证据
的适用范围提出可继承候选；评审员确认体验覆盖、合规人员确认责任与投诉承接后，
按组件生成相互独立的新认证版本。无法分割的严重事件、共用人员、共享设施只令
相关范围暂停，不波及无关组件。

撤销、部分通过、后续整改均以追加事实保存；相同承接方案重放不重复签发，内容
变化进入复审。所有事实可从任一新证书追溯回原证据、未继承原因及历史消费者处置。
"""

import copy
import itertools
import threading
from datetime import date

# 证书状态
CERT_ACTIVE = "active"
CERT_FROZEN = "frozen"
CERT_PARTIAL = "partially_approved"
CERT_SUSPENDED = "suspended"
CERT_REVOKED = "revoked"
CERT_SUPERSEDED = "superseded"

# 证据适用性
EVIDENCE_APPLICABLE = "applicable"
EVIDENCE_PARTIAL = "partial"
EVIDENCE_OUT_OF_SCOPE = "out_of_scope"

# 评审结论
CONFIRM_PENDING = "pending"
CONFIRM_PASS = "pass"
CONFIRM_REJECT = "reject"

# 组件评审结论
OUTCOME_INHERITED = "inherited"          # 继承证据并通过，签发新版本
OUTCOME_PARTIAL = "partially_approved"  # 部分通过：范围收窄，其余缺口整改
OUTCOME_SUSPENDED = "suspended"         # 暂停：事件/共用人员/共享设施无法分割
OUTCOME_REJECTED = "rejected"           # 驳回：责任或投诉承接未成立

# 事实类型
FACT_FREEZE = "freeze"
FACT_DECLARATION = "declaration"
FACT_CONFIRMATION = "confirmation"
FACT_ISSUE = "issue"
FACT_REMEDIATION = "remediation"
FACT_REVOCATION = "revocation"
FACT_CITATION = "citation"
FACT_CONSUMER_NOTICE = "consumer_notice"

VALID_DECISIONS = (CONFIRM_PASS, CONFIRM_REJECT)
VALID_FACTS = (
    FACT_FREEZE, FACT_DECLARATION, FACT_CONFIRMATION, FACT_ISSUE,
    FACT_REMEDIATION, FACT_REVOCATION, FACT_CITATION, FACT_CONSUMER_NOTICE,
)


class CertificationError(ValueError):
    """请求与领域规则冲突。"""


def _today(value=None):
    return value or date.today().isoformat()


def _require(payload, key):
    if not isinstance(payload, dict) or payload.get(key) in (None, "", []):
        raise CertificationError(f"缺少必填字段: {key}")
    return payload[key]


class CertificationService:
    """内存型领域服务；所有变更经写锁串行化。"""

    def __init__(self, clock=None):
        self._lock = threading.RLock()
        self._clock = clock or (lambda: date.today().isoformat())
        self._ids = itertools.count(1)
        self.certificates = {}   # cert_id -> 证书记录（含冻结快照与追加事实）
        self.evidence = {}       # evidence_id -> 证据
        self.events = {}         # event_id -> 严重事件/共用人员/共享设施
        self.consumers = {}      # consumer_id -> 已购用户
        self.splits = {}         # split_id -> 拆分流程

    def _new_id(self, prefix):
        return f"{prefix}-{next(self._ids):04d}"

    def _get_cert(self, cert_id):
        cert = self.certificates.get(cert_id)
        if cert is None:
            raise CertificationError(f"证书不存在: {cert_id}")
        return cert

    def _get_split(self, split_id):
        split = self.splits.get(split_id)
        if split is None:
            raise CertificationError(f"拆分流程不存在: {split_id}")
        return split

    # ------------------------------------------------------------------
    # 原证书与证据登记
    # ------------------------------------------------------------------

    def register_certificate(self, payload):
        """登记一张可被拆分的原证书（服务组件、人群、地点、责任方、有效期）。"""
        cert_id = payload.get("certificate_id") or self._new_id("cert")
        components = _require(payload, "components")
        if not isinstance(components, list) or not all(isinstance(c, dict) and c.get("id") for c in components):
            raise CertificationError("components 必须为含 id 的对象列表")
        valid_until = _require(payload, "valid_until")
        with self._lock:
            if cert_id in self.certificates:
                raise CertificationError(f"证书已存在: {cert_id}")
            cert = {
                "certificate_id": cert_id,
                "holder": _require(payload, "holder"),
                "components": copy.deepcopy(components),
                "population_tiers": copy.deepcopy(payload.get("population_tiers", [])),
                "locations": copy.deepcopy(payload.get("locations", [])),
                "responsible_party": _require(payload, "responsible_party"),
                "valid_from": payload.get("valid_from", self._clock()),
                "valid_until": valid_until,
                "status": CERT_ACTIVE,
                "frozen": False,
                "snapshot": None,
                "facts": [],
                "version": 1,
                "generated_from": None,
                "scope": {"components": [c["id"] for c in components]},
            }
            self.certificates[cert_id] = cert
            return self.public_certificate(cert_id)

    def add_evidence(self, payload):
        """登记体验证据，并声明其对组件/人群/地点的适用范围。"""
        evidence_id = payload.get("evidence_id") or self._new_id("ev")
        component_id = _require(payload, "component_id")
        applicability = payload.get("applicability", EVIDENCE_APPLICABLE)
        if applicability not in (EVIDENCE_APPLICABLE, EVIDENCE_PARTIAL, EVIDENCE_OUT_OF_SCOPE):
            raise CertificationError(f"非法适用性: {applicability}")
        with self._lock:
            if evidence_id in self.evidence:
                raise CertificationError(f"证据已存在: {evidence_id}")
            record = {
                "evidence_id": evidence_id,
                "certificate_id": _require(payload, "certificate_id"),
                "component_id": component_id,
                "summary": _require(payload, "summary"),
                "applicability": applicability,
                "population_tiers": list(payload.get("population_tiers", [])),
                "locations": list(payload.get("locations", [])),
                "out_of_scope_reasons": list(payload.get("out_of_scope_reasons", [])),
            }
            if record["certificate_id"] not in self.certificates:
                raise CertificationError(f"证书不存在: {record['certificate_id']}")
            self.evidence[evidence_id] = record
            return copy.deepcopy(record)

    def add_severe_event(self, payload):
        """登记严重事件，或标记共用人员/共享设施。

        severability=False 表示该事项无法在主体间分割，会令相关组件暂停；
        涉事范围由 component_ids 给出（缺省波及整张原证书）。
        """
        event_id = payload.get("event_id") or self._new_id("evt")
        kind = payload.get("kind", "severe_event")
        if kind not in ("severe_event", "shared_staff", "shared_facility"):
            raise CertificationError(f"非法事件类型: {kind}")
        with self._lock:
            if event_id in self.events:
                raise CertificationError(f"事件已存在: {event_id}")
            cert_id = _require(payload, "certificate_id")
            if cert_id not in self.certificates:
                raise CertificationError(f"证书不存在: {cert_id}")
            record = {
                "event_id": event_id,
                "certificate_id": cert_id,
                "kind": kind,
                "summary": _require(payload, "summary"),
                "component_ids": list(payload.get("component_ids", [])),
                "severable": bool(payload.get("severable", False)),
                "resolved": False,
            }
            self.events[event_id] = record
            return copy.deepcopy(record)

    def register_consumer(self, payload):
        """登记已购用户；其原有承诺在拆分期间保留。"""
        consumer_id = payload.get("consumer_id") or self._new_id("csm")
        with self._lock:
            if consumer_id in self.consumers:
                raise CertificationError(f"用户已存在: {consumer_id}")
            cert_id = _require(payload, "certificate_id")
            if cert_id not in self.certificates:
                raise CertificationError(f"证书不存在: {cert_id}")
            component_id = _require(payload, "component_id")
            valid_components = {c["id"] for c in self.certificates[cert_id]["components"]}
            if component_id not in valid_components:
                raise CertificationError(f"组件不在证书范围内: {component_id}")
            record = {
                "consumer_id": consumer_id,
                "certificate_id": cert_id,
                "component_id": component_id,
                "commitment": _require(payload, "commitment"),
                "status": "commitment_retained",
                "responsibility_history": [],
                "notices": [],
            }
            self.consumers[consumer_id] = record
            return copy.deepcopy(record)

    # ------------------------------------------------------------------
    # 冻结
    # ------------------------------------------------------------------

    def freeze_certificate(self, payload):
        """冻结原证书的服务组件、人群分层、地点、责任方和有效期。"""
        cert_id = _require(payload, "certificate_id")
        with self._lock:
            cert = self._get_cert(cert_id)
            if cert["frozen"]:
                return self.public_certificate(cert_id)  # 冻结幂等
            snapshot = {
                "holder": cert["holder"],
                "components": copy.deepcopy(cert["components"]),
                "population_tiers": copy.deepcopy(cert["population_tiers"]),
                "locations": copy.deepcopy(cert["locations"]),
                "responsible_party": cert["responsible_party"],
                "valid_from": cert["valid_from"],
                "valid_until": cert["valid_until"],
            }
            cert["frozen"] = True
            cert["status"] = CERT_FROZEN
            cert["snapshot"] = snapshot
            self._append_fact(cert, FACT_FREEZE, {
                "certificate_id": cert_id,
                "snapshot": copy.deepcopy(snapshot),
            })
            return self.public_certificate(cert_id)

    # ------------------------------------------------------------------
    # 拆分流程与承接声明
    # ------------------------------------------------------------------

    def initiate_split(self, payload):
        """发起拆分：指定保留场地/员工的主体与接手线上签约/健康支持的主体。"""
        cert_id = _require(payload, "certificate_id")
        with self._lock:
            cert = self._get_cert(cert_id)
            if not cert["frozen"]:
                raise CertificationError("原证书必须先冻结才能拆分")
            split_id = payload.get("split_id") or self._new_id("spl")
            if split_id in self.splits:
                raise CertificationError(f"拆分流程已存在: {split_id}")
            split = {
                "split_id": split_id,
                "certificate_id": cert_id,
                "subjects": {
                    "venue": {
                        "subject_id": _require(payload, "venue_subject_id"),
                        "name": payload.get("venue_name", "场地与员工承接主体"),
                        "keeps": ["venue", "staff"],
                    },
                    "online": {
                        "subject_id": _require(payload, "online_subject_id"),
                        "name": payload.get("online_name", "线上签约与健康支持承接主体"),
                        "keeps": ["online_contracting", "health_support"],
                    },
                },
                "declarations": {},   # subject_id -> 声明
                "candidates": None,
                "review": {"experience": {}, "compliance": {}},
                "issued": {},
                "subject_fingerprints": {},
                "status": "declaring",
                "facts": [],
            }
            self.splits[split_id] = split
            return self.public_split(split_id)

    def submit_declaration(self, payload):
        """某一新主体声明承接项、控制措施与缺口（可多次修订）。"""
        split_id = _require(payload, "split_id")
        subject_id = _require(payload, "subject_id")
        with self._lock:
            split = self._get_split(split_id)
            subject_key = self._subject_key(split, subject_id)
            cert = self._get_cert(split["certificate_id"])
            component_ids = _require(payload, "component_ids")
            valid = {c["id"] for c in cert["snapshot"]["components"]}
            unknown = [c for c in component_ids if c not in valid]
            if unknown:
                raise CertificationError(f"承接组件不在冻结范围内: {unknown}")
            declaration = {
                "subject_id": subject_id,
                "subject_key": subject_key,
                "component_ids": list(component_ids),
                "controls": copy.deepcopy(_require(payload, "controls")),
                "gaps": copy.deepcopy(payload.get("gaps", [])),
                "liability_accepted": bool(payload.get("liability_accepted", False)),
                "complaint_handling_accepted": bool(
                    payload.get("complaint_handling_accepted", False)
                ),
                "revision": split["declarations"].get(subject_id, {}).get("revision", 0) + 1,
                "submitted_at": self._clock(),
            }
            split["declarations"][subject_id] = declaration
            # 声明内容变化后，既有评审结论一律失效，回到复审
            for track in ("experience", "compliance"):
                split["review"][track].pop(subject_id, None)
            split["candidates"] = None
            split["status"] = "declaring"
            self._append_split_fact(split, FACT_DECLARATION, copy.deepcopy(declaration))
            return self.public_split(split_id)

    def inheritance_candidates(self, split_id):
        """按证据适用范围提出可继承候选，并给出未继承原因。

        既接受拆分编号字符串，也接受 {"split_id": ...} 的请求体。
        """
        if isinstance(split_id, dict):
            split_id = _require(split_id, "split_id")
        with self._lock:
            split = self._get_split(split_id)
            cert = self._get_cert(split["certificate_id"])
            candidates = {}
            for subject_id, declaration in split["declarations"].items():
                per_component = {}
                for component_id in declaration["component_ids"]:
                    per_component[component_id] = self._candidates_for_component(
                        cert, component_id, declaration
                    )
                candidates[subject_id] = per_component
            split["candidates"] = candidates
            return copy.deepcopy(candidates)

    def _candidates_for_component(self, cert, component_id, declaration):
        """计算单个组件对单个主体的证据继承候选。"""
        evidence_rows = [
            e for e in self.evidence.values()
            if e["certificate_id"] == cert["certificate_id"] and e["component_id"] == component_id
        ]
        tier_names = {t.get("id", t) if isinstance(t, dict) else t
                      for t in cert["snapshot"]["population_tiers"]}
        location_names = set(cert["snapshot"]["locations"])

        inherited, non_inherited = [], []
        for evidence in evidence_rows:
            reasons = []
            if evidence["applicability"] == EVIDENCE_OUT_OF_SCOPE:
                reasons.append("证据标注为新主体适用范围之外")
            missing_tiers = [t for t in evidence["population_tiers"] if t not in tier_names]
            if missing_tiers:
                reasons.append(f"证据覆盖人群分层缺失: {missing_tiers}")
            missing_locations = [loc for loc in evidence["locations"] if loc not in location_names]
            if missing_locations:
                reasons.append(f"证据覆盖地点缺失: {missing_locations}")
            if evidence["applicability"] == EVIDENCE_PARTIAL:
                reasons.extend(evidence["out_of_scope_reasons"] or ["证据仅部分适用"])
            if reasons:
                non_inherited.append({
                    "evidence_id": evidence["evidence_id"],
                    "reasons": reasons,
                })
            else:
                inherited.append(evidence["evidence_id"])
        if not evidence_rows:
            non_inherited.append({
                "evidence_id": None,
                "reasons": ["原证书范围内不存在可适用证据"],
            })
        return {
            "component_id": component_id,
            "inheritable_evidence": inherited,
            "non_inherited": non_inherited,
        }

    # ------------------------------------------------------------------
    # 评审确认：体验覆盖 + 责任与投诉承接
    # ------------------------------------------------------------------

    def confirm_review(self, payload):
        """评审员确认体验覆盖，或合规人员确认责任与投诉承接。"""
        split_id = _require(payload, "split_id")
        subject_id = _require(payload, "subject_id")
        track = _require(payload, "track")
        if track not in ("experience", "compliance"):
            raise CertificationError("track 必须为 experience 或 compliance")
        decision = _require(payload, "decision")
        if decision not in VALID_DECISIONS:
            raise CertificationError(f"非法评审结论: {decision}")
        with self._lock:
            split = self._get_split(split_id)
            if subject_id not in split["declarations"]:
                raise CertificationError("该主体尚未提交承接声明")
            declaration = split["declarations"][subject_id]
            if track == "compliance" and decision == CONFIRM_PASS:
                if not declaration["liability_accepted"]:
                    raise CertificationError("责任承接未声明，合规不可通过")
                if not declaration["complaint_handling_accepted"]:
                    raise CertificationError("投诉承接未声明，合规不可通过")
            note = payload.get("note", "")
            split["review"][track][subject_id] = {
                "decision": decision,
                "reviewer": _require(payload, "reviewer"),
                "note": note,
                "decided_at": self._clock(),
            }
            self._append_split_fact(split, FACT_CONFIRMATION, {
                "subject_id": subject_id,
                "track": track,
                "decision": decision,
                "reviewer": split["review"][track][subject_id]["reviewer"],
                "note": note,
            })
            if all(
                split["review"]["experience"].get(sid, {}).get("decision")
                and split["review"]["compliance"].get(sid, {}).get("decision")
                for sid in split["declarations"]
            ):
                split["status"] = "reviewed"
            else:
                split["status"] = "in_review"
            return self.public_split(split_id)

    # ------------------------------------------------------------------
    # 签发：相互独立的新认证版本
    # ------------------------------------------------------------------

    def issue(self, payload):
        """按评审结果为每个主体签发相互独立的新认证版本。

        同一拆分以同一承接方案重放时不重复签发；某一主体声明内容变化后，只有
        该主体进入复审并生成新版本，另一主体的既有证书保持有效、不被拖累。
        """
        split_id = _require(payload, "split_id")
        with self._lock:
            split = self._get_split(split_id)
            declarations = split["declarations"]
            if not declarations:
                raise CertificationError("尚无承接声明")
            for subject_id in declarations:
                for track in ("experience", "compliance"):
                    if split["review"][track].get(subject_id, {}).get("decision") is None:
                        raise CertificationError(f"{subject_id} 的 {track} 评审尚未完成")

            # 始终按最新证据重算候选，避免候选生成后补登证据造成过期
            candidates = self.inheritance_candidates(split_id)
            unresolved_events = self._blocking_events(split["certificate_id"])

            # 同一组件只允许由一个新主体承接，责任边界必须唯一
            owners = {}
            for subject_id, declaration in declarations.items():
                for component_id in declaration["component_ids"]:
                    if component_id in owners and owners[component_id] != subject_id:
                        raise CertificationError(
                            f"组件 {component_id} 被多个主体同时承接: "
                            f"{owners[component_id]} 与 {subject_id}"
                        )
                    owners[component_id] = subject_id

            current_certs, fresh_ids = [], []
            for subject_id, declaration in declarations.items():
                experience = split["review"]["experience"][subject_id]["decision"]
                compliance = split["review"]["compliance"][subject_id]["decision"]
                component_results = {}
                for component_id in declaration["component_ids"]:
                    component_results[component_id] = self._evaluate_component(
                        split, subject_id, component_id, candidates,
                        unresolved_events, experience, compliance,
                    )
                fingerprint = self._subject_fingerprint(
                    split, subject_id, declaration, unresolved_events
                )
                existing_id = split["issued"].get(subject_id)
                if existing_id and split["subject_fingerprints"].get(subject_id) == fingerprint:
                    # 该主体承接方案未变：复用既有版本，不重复签发
                    current_certs.append(self.certificates[existing_id])
                    continue
                new_cert = self._create_successor(
                    split, subject_id, declaration, component_results, fingerprint
                )
                current_certs.append(new_cert)
                fresh_ids.append(new_cert["certificate_id"])

            replayed = not fresh_ids
            split["issued"] = {c["holder_subject_id"]: c["certificate_id"] for c in current_certs}
            split["status"] = "issued"
            self._append_split_fact(split, FACT_ISSUE, {
                "certificate_ids": [c["certificate_id"] for c in current_certs],
                "new_certificate_ids": fresh_ids,
                "replayed": replayed,
                "subject_fingerprints": copy.deepcopy(split["subject_fingerprints"]),
            })
            self._notify_consumers(split, current_certs, fresh_ids)
            return {"replayed": replayed, "split": self.public_split(split_id),
                    "issued": [self.public_certificate(c["certificate_id"]) for c in current_certs]}

    def _evaluate_component(self, split, subject_id, component_id, candidates,
                            unresolved_events, experience, compliance):
        """单个组件的结论：暂停 / 驳回 / 部分通过 / 继承通过。"""
        blockers = [
            e["event_id"] for e in unresolved_events
            if not e["component_ids"] or component_id in e["component_ids"]
        ]
        if blockers:
            return {
                "component_id": component_id,
                "outcome": OUTCOME_SUSPENDED,
                "blocking_events": blockers,
                "reasons": ["存在无法分割的严重事件、共用人员或共享设施"],
            }
        if experience == CONFIRM_REJECT or compliance == CONFIRM_REJECT:
            reasons = []
            if experience == CONFIRM_REJECT:
                reasons.append("评审员未确认体验覆盖")
            if compliance == CONFIRM_REJECT:
                reasons.append("合规人员未确认责任与投诉承接")
            return {"component_id": component_id, "outcome": OUTCOME_REJECTED,
                    "blocking_events": [], "reasons": reasons}

        candidate = candidates[subject_id][component_id]
        inherited = candidate["inheritable_evidence"]
        non_inherited = candidate["non_inherited"]
        gaps = [g for g in split["declarations"][subject_id]["gaps"]
                if (isinstance(g, dict) and g.get("component_id") == component_id)]
        if inherited and (non_inherited or gaps):
            outcome = OUTCOME_PARTIAL
        elif inherited:
            outcome = OUTCOME_INHERITED
        else:
            outcome = OUTCOME_REJECTED
        return {
            "component_id": component_id,
            "outcome": outcome,
            "blocking_events": [],
            "inherited_evidence": inherited,
            "non_inherited": copy.deepcopy(non_inherited),
            "open_gaps": copy.deepcopy(gaps),
            "reasons": [r for item in non_inherited for r in item["reasons"]],
        }

    def _create_successor(self, split, subject_id, declaration, component_results,
                          fingerprint):
        """为一个主体生成独立新认证版本；暂停组件不进入有效范围。"""
        original_id = split["certificate_id"]
        original = self._get_cert(original_id)
        subject = self._subject_info(split, subject_id)

        active_components = [cid for cid, r in component_results.items()
                             if r["outcome"] in (OUTCOME_INHERITED, OUTCOME_PARTIAL)]
        suspended_components = [cid for cid, r in component_results.items()
                                if r["outcome"] == OUTCOME_SUSPENDED]
        rejected_components = [cid for cid, r in component_results.items()
                               if r["outcome"] == OUTCOME_REJECTED]

        if not active_components and suspended_components:
            status = CERT_SUSPENDED
        elif not active_components and rejected_components:
            status = CERT_REVOKED
        elif suspended_components or rejected_components:
            status = CERT_PARTIAL
        else:
            status = CERT_ACTIVE

        prior_versions = [
            cid for cid, cert in self.certificates.items()
            if (cert.get("generated_from") or {}).get("split_id") == split["split_id"]
            and (cert.get("generated_from") or {}).get("subject_id") == subject_id
        ]
        version = len(prior_versions) + 1
        cert_id = self._new_id("cert")
        new_cert = {
            "certificate_id": cert_id,
            "holder": subject["name"],
            "holder_subject_id": subject_id,
            "components": [
                copy.deepcopy(c) for c in original["snapshot"]["components"]
                if c["id"] in active_components
            ],
            "population_tiers": copy.deepcopy(original["snapshot"]["population_tiers"]),
            "locations": copy.deepcopy(original["snapshot"]["locations"]),
            "responsible_party": subject_id,
            "valid_from": self._clock(),
            "valid_until": original["snapshot"]["valid_until"],
            "status": status,
            "frozen": False,
            "snapshot": None,
            "facts": [],
            "version": version,
            "generated_from": {
                "split_id": split["split_id"],
                "original_certificate_id": original_id,
                "subject_id": subject_id,
            },
            "scope": {"components": active_components},
            "component_results": component_results,
            "suspended_components": suspended_components,
            "rejected_components": rejected_components,
            "declaration": copy.deepcopy(declaration),
            "plan_fingerprint": fingerprint,
        }
        self.certificates[cert_id] = new_cert
        split["subject_fingerprints"][subject_id] = fingerprint
        self._append_fact(new_cert, FACT_ISSUE, {
            "certificate_id": cert_id,
            "split_id": split["split_id"],
            "original_certificate_id": original_id,
            "subject_id": subject_id,
            "version": version,
            "component_results": copy.deepcopy(component_results),
        })
        # 内容变化进入复审后重新签发：同一主体的旧版本被取代并留痕，不再可引用
        for prior_id in prior_versions:
            prior = self.certificates[prior_id]
            if prior["status"] != CERT_REVOKED:
                prior["status"] = CERT_SUPERSEDED
            self._append_fact(prior, FACT_ISSUE, {
                "superseded_by": cert_id,
                "reason": "承接内容变化进入复审，新版本已签发",
            })
        for cid in rejected_components:
            self._append_fact(new_cert, FACT_REVOCATION, {
                "component_id": cid,
                "reason": "评审未通过，该范围不予签发",
            })
        return new_cert

    def _blocking_events(self, cert_id):
        """未解决且不可分割的事件；按其 component_ids 限定波及面。"""
        return [
            e for e in self.events.values()
            if e["certificate_id"] == cert_id and not e["severable"] and not e["resolved"]
        ]

    def _subject_fingerprint(self, split, subject_id, declaration, unresolved_events):
        """单主体承接方案指纹：声明内容、评审结论、相关证据与阻塞事件共同决定结果。"""
        original_id = split["certificate_id"]
        evidence_state = sorted(
            (
                e["evidence_id"], e["applicability"],
                tuple(e["population_tiers"]), tuple(e["locations"]),
                tuple(e["out_of_scope_reasons"]),
            )
            for e in self.evidence.values()
            if e["certificate_id"] == original_id
            and e["component_id"] in declaration["component_ids"]
        )
        material = {
            "component_ids": declaration["component_ids"],
            "controls": declaration["controls"],
            "gaps": declaration["gaps"],
            "liability_accepted": declaration["liability_accepted"],
            "complaint_handling_accepted": declaration["complaint_handling_accepted"],
            "review": {
                "experience": split["review"]["experience"].get(subject_id, {}).get("decision"),
                "compliance": split["review"]["compliance"].get(subject_id, {}).get("decision"),
            },
            "evidence_state": evidence_state,
            "blocking_events": sorted(
                e["event_id"] for e in unresolved_events
                if not e["component_ids"]
                or any(cid in e["component_ids"] for cid in declaration["component_ids"])
            ),
        }
        return repr(material)

    # ------------------------------------------------------------------
    # 追加事实：整改、事件处置、撤销、宣传引用
    # ------------------------------------------------------------------

    def submit_remediation(self, payload):
        """后续整改以追加事实保存；可解决暂停所依赖的事件/缺口。"""
        cert_id = _require(payload, "certificate_id")
        with self._lock:
            cert = self._get_cert(cert_id)
            record = {
                "component_id": payload.get("component_id"),
                "summary": _require(payload, "summary"),
                "resolves_events": list(payload.get("resolves_events", [])),
                "submitted_by": _require(payload, "submitted_by"),
                "submitted_at": self._clock(),
            }
            for event_id in record["resolves_events"]:
                event = self.events.get(event_id)
                if event is None:
                    raise CertificationError(f"事件不存在: {event_id}")
                event["resolved"] = True
            self._append_fact(cert, FACT_REMEDIATION, record)
            return copy.deepcopy(record)

    def revoke_scope(self, payload):
        """撤销（整张或某组件）以追加事实保存；组件级撤销不影响其余组件。"""
        cert_id = _require(payload, "certificate_id")
        with self._lock:
            cert = self._get_cert(cert_id)
            component_id = payload.get("component_id")
            if component_id:
                if component_id not in cert["scope"]["components"]:
                    raise CertificationError(f"组件不在当前有效范围: {component_id}")
                cert["scope"]["components"].remove(component_id)
                cert["components"] = [c for c in cert["components"] if c["id"] != component_id]
                if not cert["scope"]["components"]:
                    cert["status"] = CERT_REVOKED
            else:
                cert["scope"]["components"] = []
                cert["components"] = []
                cert["status"] = CERT_REVOKED
            record = {
                "component_id": component_id,
                "reason": _require(payload, "reason"),
                "revoked_by": _require(payload, "revoked_by"),
                "revoked_at": self._clock(),
            }
            self._append_fact(cert, FACT_REVOCATION, record)
            return copy.deepcopy(record)

    def register_citation(self, payload):
        """拆分期间的宣传引用必须指向明确版本；禁止引用冻结/暂停/撤销版本。"""
        cert_id = _require(payload, "certificate_id")
        with self._lock:
            cert = self._get_cert(cert_id)
            version = payload.get("version", cert["version"])
            if version != cert["version"]:
                raise CertificationError(
                    f"引用必须指向当前明确版本 v{cert['version']}，不能指向 v{version}"
                )
            allowed_statuses = (CERT_ACTIVE, "partially_approved")
            if cert["status"] not in allowed_statuses:
                raise CertificationError(
                    f"证书状态 {cert['status']} 不允许对外宣传引用"
                )
            record = {
                "citation": _require(payload, "citation"),
                "certificate_id": cert_id,
                "version": version,
                "registered_by": _require(payload, "registered_by"),
                "registered_at": self._clock(),
            }
            self._append_fact(cert, FACT_CITATION, record)
            return copy.deepcopy(record)

    # ------------------------------------------------------------------
    # 已购用户：承诺保留与责任变更记录
    # ------------------------------------------------------------------

    def _notify_consumers(self, split, current_certs, fresh_ids):
        """签发后：已购用户保留原承诺，并收到责任变更记录。

        仅对本次新签发（fresh）证书涉及的用户追加通知；方案未变、证书被复用
        的主体不重复打扰。暂停范围首次签发时记录“暂停但承诺保留”，整改后重签
        再补发责任转移记录。
        """
        fresh_ids = set(fresh_ids)
        fresh = [c for c in current_certs if c["certificate_id"] in fresh_ids]
        component_owner = {
            component_id: new_cert
            for new_cert in fresh
            for component_id in new_cert["scope"]["components"]
        }

        for consumer in self.consumers.values():
            if consumer["certificate_id"] != split["certificate_id"]:
                continue
            component_id = consumer["component_id"]
            owner = component_owner.get(component_id)
            if owner is not None:
                subject_id = owner["holder_subject_id"]
                new_cert_id = owner["certificate_id"]
                last = (consumer["responsibility_history"][-1]
                        if consumer["responsibility_history"] else None)
                restored = consumer["status"] == "service_suspended_retained"
                if last and last["to_subject"] == subject_id:
                    # 同一主体复审重签：只把新版本挂到既有记录，不重复推送
                    last["versioned_certificate_ids"] = sorted(
                        set(last.get("versioned_certificate_ids",
                                     [last["new_certificate_id"]])) | {new_cert_id}
                    )
                else:
                    consumer["responsibility_history"].append({
                        "at": self._clock(),
                        "split_id": split["split_id"],
                        "from_subject": self._get_cert(
                            split["certificate_id"])["snapshot"]["responsible_party"],
                        "to_subject": subject_id,
                        "new_certificate_id": new_cert_id,
                        "versioned_certificate_ids": [new_cert_id],
                        "change": "责任转移至承接主体，原承诺保留",
                    })
                    consumer["notices"].append({
                        "at": self._clock(),
                        "type": ("responsibility_restored" if restored
                                 else "responsibility_change"),
                        "message": f"您的服务责任已由 {subject_id} 承接，原承诺保持不变",
                        "new_certificate_id": new_cert_id,
                    })
                consumer["status"] = "commitment_retained"
                continue

            result = next(
                (c["component_results"][component_id] for c in fresh
                 if component_id in c["component_results"]),
                None,
            )
            if result is None:
                continue  # 该主体承接方案未变，证书复用，不重复通知
            if result["outcome"] == OUTCOME_SUSPENDED:
                consumer["status"] = "service_suspended_retained"
                consumer["notices"].append({
                    "at": self._clock(),
                    "type": "scope_suspended",
                    "message": "相关范围因无法分割事项暂停，您的原承诺予以保留，等待整改",
                    "blocking_events": result["blocking_events"],
                })
            else:
                consumer["status"] = "commitment_retained_pending"
                consumer["notices"].append({
                    "at": self._clock(),
                    "type": "commitment_retained",
                    "message": "承接范围未通过签发，您的原承诺继续保留",
                })

    # ------------------------------------------------------------------
    # 追溯：从任一新证书回到原证据、未继承原因、消费者处置
    # ------------------------------------------------------------------

    def trace(self, cert_id):
        origin = self._get_cert(cert_id)
        generated = origin.get("generated_from")
        if not generated:
            raise CertificationError("该证书不是拆分承接产生的新证书")
        original_id = generated["original_certificate_id"]
        split_id = generated["split_id"]
        split = self._get_split(split_id)
        original = self._get_cert(original_id)

        component_traces = []
        for component_id, result in origin.get("component_results", {}).items():
            related_evidence = [
                e for e in self.evidence.values()
                if e["certificate_id"] == original_id and e["component_id"] == component_id
            ]
            component_traces.append({
                "component_id": component_id,
                "outcome": result["outcome"],
                "inherited_evidence": result.get("inherited_evidence", []),
                "non_inherited": result.get("non_inherited", []),
                "blocking_events": result.get("blocking_events", []),
                "source_evidence": [
                    {
                        "evidence_id": e["evidence_id"],
                        "summary": e["summary"],
                        "applicability": e["applicability"],
                        "population_tiers": e["population_tiers"],
                        "locations": e["locations"],
                    }
                    for e in related_evidence
                ],
            })

        consumer_dispositions = []
        for consumer in self.consumers.values():
            if consumer["certificate_id"] != original_id:
                continue
            history = [
                h for h in consumer["responsibility_history"]
                if h.get("new_certificate_id") == cert_id
            ]
            if consumer["component_id"] in origin.get("component_results", {}) or history:
                consumer_dispositions.append({
                    "consumer_id": consumer["consumer_id"],
                    "component_id": consumer["component_id"],
                    "status": consumer["status"],
                    "commitment": consumer["commitment"],
                    "responsibility_history": copy.deepcopy(
                        history or consumer["responsibility_history"]
                    ),
                    "notices": copy.deepcopy(consumer["notices"]),
                })

        # 同主体历史版本链：整改/撤销等事实可能挂在被取代的旧版本上，仍可追溯
        version_history = []
        for other_id, other in sorted(self.certificates.items()):
            gen = other.get("generated_from") or {}
            if (gen.get("split_id") == split_id
                    and gen.get("subject_id") == generated["subject_id"]):
                version_history.append({
                    "certificate_id": other_id,
                    "version": other["version"],
                    "status": other["status"],
                    "facts": copy.deepcopy(other["facts"]),
                })

        return {
            "certificate_id": cert_id,
            "version": origin["version"],
            "generated_from": generated,
            "original_snapshot": copy.deepcopy(original["snapshot"]),
            "subject": self._subject_info(split, generated["subject_id"]),
            "declaration": copy.deepcopy(origin.get("declaration")),
            "review_decisions": {
                "experience": split["review"]["experience"].get(generated["subject_id"]),
                "compliance": split["review"]["compliance"].get(generated["subject_id"]),
            },
            "components": component_traces,
            "consumer_dispositions": consumer_dispositions,
            "facts": copy.deepcopy(origin["facts"]),
            "version_history": version_history,
            "split_facts": copy.deepcopy(split["facts"]),
        }

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------

    def list_certificates(self):
        with self._lock:
            return [self.public_certificate(cid) for cid in sorted(self.certificates)]

    def public_certificate(self, cert_id):
        cert = self._get_cert(cert_id)
        return {
            "certificate_id": cert["certificate_id"],
            "holder": cert["holder"],
            "status": cert["status"],
            "frozen": cert["frozen"],
            "version": cert["version"],
            "valid_from": cert["valid_from"],
            "valid_until": cert["valid_until"],
            "responsible_party": cert["responsible_party"],
            "scope": copy.deepcopy(cert["scope"]),
            "population_tiers": copy.deepcopy(cert["population_tiers"]),
            "locations": copy.deepcopy(cert["locations"]),
            "generated_from": copy.deepcopy(cert.get("generated_from")),
            "holder_subject_id": cert.get("holder_subject_id"),
            "component_results": copy.deepcopy(cert.get("component_results", {})),
            "suspended_components": copy.deepcopy(cert.get("suspended_components", [])),
            "rejected_components": copy.deepcopy(cert.get("rejected_components", [])),
            "facts": copy.deepcopy(cert["facts"]),
        }

    def public_split(self, split_id):
        split = self._get_split(split_id)
        return {
            "split_id": split["split_id"],
            "certificate_id": split["certificate_id"],
            "status": split["status"],
            "subjects": copy.deepcopy(split["subjects"]),
            "declarations": copy.deepcopy(split["declarations"]),
            "candidates": copy.deepcopy(split["candidates"]),
            "review": copy.deepcopy(split["review"]),
            "issued": copy.deepcopy(split["issued"]),
        }

    def _subject_key(self, split, subject_id):
        for key, subject in split["subjects"].items():
            if subject["subject_id"] == subject_id:
                return key
        raise CertificationError(f"主体不在本拆分中: {subject_id}")

    def _subject_info(self, split, subject_id):
        key = self._subject_key(split, subject_id)
        return copy.deepcopy(split["subjects"][key])

    def _append_fact(self, cert, fact_type, detail):
        fact = {"seq": len(cert["facts"]) + 1, "type": fact_type,
                "at": self._clock(), "detail": copy.deepcopy(detail)}
        cert["facts"].append(fact)
        return fact

    def _append_split_fact(self, split, fact_type, detail):
        fact = {"seq": len(split["facts"]) + 1, "type": fact_type,
                "at": self._clock(), "detail": copy.deepcopy(detail)}
        split["facts"].append(fact)
        return fact
