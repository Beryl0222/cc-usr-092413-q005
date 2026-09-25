"""手工冒烟：康养机构拆分双主体全流程。"""
from certification import CertificationService, DomainError

svc = CertificationService()
C = svc.command
Q = svc

# 三个主体：原机构、保留场地员工的 A、接手线上签约与健康支持的 B
for oid, name in [("org-original", "原康养连锁"), ("org-venue", "场地承接公司"), ("org-online", "线上健康公司")]:
    C("register_organization", {"org_id": oid, "name": name})

C("register_certificate", {
    "cert_id": "cert-1", "holder_org_id": "org-original",
    "valid_from": "2025-01-01", "valid_to": "2027-01-01",
    "components": [
        {"id": "stay", "name": "旅居住宿",
         "populations": ["self-care", "assisted"], "locations": ["beijing-base", "sanya-base"],
         "responsible_party_org_id": "org-original"},
        {"id": "signing", "name": "线上签约",
         "populations": ["self-care", "assisted"], "locations": ["online"],
         "responsible_party_org_id": "org-original"},
        {"id": "health", "name": "健康支持",
         "populations": ["self-care", "assisted"], "locations": ["online", "beijing-base"],
         "responsible_party_org_id": "org-original"},
    ],
})

# 证据：stay 覆盖全人群全地点；health 在 online 只覆盖 self-care（assisted 无证据）
C("record_evidence", {"evidence_id": "ev-stay-1", "cert_id": "cert-1", "component_id": "stay",
                      "populations": ["self-care", "assisted"], "locations": ["beijing-base", "sanya-base"],
                      "summary": "住宿体验暗访"})
C("record_evidence", {"evidence_id": "ev-sign-1", "cert_id": "cert-1", "component_id": "signing",
                      "populations": ["self-care", "assisted"], "locations": ["online"],
                      "summary": "线上签约流程测评"})
C("record_evidence", {"evidence_id": "ev-health-1", "cert_id": "cert-1", "component_id": "health",
                      "populations": ["self-care"], "locations": ["online"],
                      "summary": "在线健康咨询（自理老人）"})
C("record_evidence", {"evidence_id": "ev-health-2", "cert_id": "cert-1", "component_id": "health",
                      "populations": ["assisted"], "locations": ["beijing-base"],
                      "summary": "驻场护理（助养老人）"})

# 两个已购用户
C("record_purchase", {"consumer_id": "u1", "cert_id": "cert-1",
                      "items": [{"component_id": "stay", "commitment": "原合同价与房型"},
                                {"component_id": "health", "commitment": "原健康随访频次"}]})
C("record_purchase", {"consumer_id": "u2", "cert_id": "cert-1",
                      "items": [{"component_id": "signing", "commitment": "原退费规则"}]})

# 冻结
opened = C("open_split", {"split_id": "split-1", "cert_id": "cert-1"})
assert opened["status"] == "frozen", opened
assert C == C  # noqa
frozen = Q.get_split("split-1")
assert [c["id"] for c in frozen["frozen"]["components"]] == ["stay", "signing", "health"]

# 声明：A 承接 stay；B 承接 signing+health
d1 = C("declare_succession", {"split_id": "split-1", "successor_org_id": "org-venue", "items": [
    {"component_id": "stay", "controls": ["驻场安全巡检"], "gaps": [],
     "staff_ids": ["staff-li"], "facility_ids": ["sanya-base"]},
]})
assert d1["evidence_candidates"]["stay"]["applicable"] == ["ev-stay-1"], d1

d2 = C("declare_succession", {"split_id": "split-1", "successor_org_id": "org-online", "items": [
    {"component_id": "signing", "controls": ["电子签"], "gaps": [], "staff_ids": [], "facility_ids": []},
    {"component_id": "health", "populations": ["self-care"], "locations": ["online"],
     "controls": ["远程问诊"], "gaps": ["assisted-online"], "staff_ids": [], "facility_ids": []},
]})
# health 的 assisted/online 无证据：候选给 self-care+online 的 ev-health-1；
# 驻场证据 ev-health-2 因人群/地点不匹配列为不适用
cand_h = d2["evidence_candidates"]["health"]
assert cand_h["applicable"] == ["ev-health-1"], cand_h
assert cand_h["non_applicable"][0]["evidence_id"] == "ev-health-2", cand_h
assert {r["code"] for r in cand_h["non_applicable"][0]["reasons"]} == {
    "evidence_population_gap", "evidence_location_gap"}

# 同一组件不能两家同时承接
try:
    C("declare_succession", {"split_id": "split-1", "successor_org_id": "org-online", "items": [
        {"component_id": "stay", "controls": []}]})
    raise AssertionError("应拒绝重复组件")
except DomainError as e:
    assert e.code == "DECLARATION_EXISTS", e.code

# 缺口未关闭不能确认体验
try:
    C("confirm_experience_coverage", {"split_id": "split-1", "successor_org_id": "org-online",
        "reviewer": "r1", "decisions": [
            {"component_id": "signing", "accepted_evidence_ids": ["ev-sign-1"]},
            {"component_id": "health", "accepted_evidence_ids": ["ev-health-1"]},
        ]})
    raise AssertionError("应因未关闭缺口失败")
except DomainError as e:
    assert e.code == "UNRESOLVED_GAPS", e.code

# 整改后确认
C("record_rectification", {"split_id": "split-1", "successor_org_id": "org-online",
    "rectification_id": "rect-1", "component_id": "health", "gap": "assisted-online",
    "action": "assisted 人群在 online 暂不承接，从范围移除", "status": "completed"})
# 修订声明把 assisted-online 从范围移除（缺口随之消失），先前确认需重做（此时还没确认）
a2 = C("amend_succession_declaration", {"split_id": "split-1", "successor_org_id": "org-online", "items": [
    {"component_id": "signing", "controls": ["电子签"], "gaps": [], "staff_ids": [], "facility_ids": []},
    {"component_id": "health", "populations": ["self-care"], "locations": ["online"],
     "controls": ["远程问诊"], "gaps": [], "staff_ids": [], "facility_ids": []},
]})
assert a2["revision"] == 2, a2

# 双确认 B
C("confirm_experience_coverage", {"split_id": "split-1", "successor_org_id": "org-online",
    "reviewer": "r1", "decisions": [
        {"component_id": "signing", "accepted_evidence_ids": ["ev-sign-1"], "resolved_gaps": []},
        {"component_id": "health", "accepted_evidence_ids": ["ev-health-1"], "resolved_gaps": []},
    ]})
try:
    C("confirm_responsibility_handover", {"split_id": "split-1", "successor_org_id": "org-online",
        "compliance_officer": "c1", "decisions": [
            {"component_id": "signing", "complaint_channel": "app", "liability_acknowledged": True},
            {"component_id": "health", "complaint_channel": "app", "liability_acknowledged": False},
        ]})
    raise AssertionError("未承接责任应失败")
except DomainError as e:
    assert e.code == "LIABILITY_NOT_ACKNOWLEDGED", e.code
C("confirm_responsibility_handover", {"split_id": "split-1", "successor_org_id": "org-online",
    "compliance_officer": "c1", "decisions": [
        {"component_id": "signing", "complaint_channel": "app", "liability_acknowledged": True},
        {"component_id": "health", "complaint_channel": "app", "liability_acknowledged": True},
    ]})

# B 签发
iss_b = C("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
assert iss_b["replayed"] is False and iss_b["version"] == 1, iss_b
cert_b_id = iss_b["cert_id"]
assert set(iss_b["included_components"]) == {"signing", "health"}, iss_b
# 重放：幂等，不重复签发
iss_b2 = C("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
assert iss_b2["replayed"] is True and iss_b2["cert_id"] == cert_b_id, iss_b2

# A：存在无法分割严重事件关联 stay -> stay 暂停，且不影响 B 的 signing/health
C("register_severe_incident", {"split_id": "split-1", "incident_id": "inc-1",
    "component_ids": ["stay"], "inseparable": True, "description": "场地事故责任无法在两主体间分割"})
# A 双确认
C("confirm_experience_coverage", {"split_id": "split-1", "successor_org_id": "org-venue",
    "reviewer": "r1", "decisions": [
        {"component_id": "stay", "accepted_evidence_ids": ["ev-stay-1"], "resolved_gaps": []}]})
C("confirm_responsibility_handover", {"split_id": "split-1", "successor_org_id": "org-venue",
    "compliance_officer": "c1", "decisions": [
        {"component_id": "stay", "complaint_channel": "onsite-desk", "liability_acknowledged": True}]})
# A 无可签发范围 -> 报错且 stay 保持暂停
try:
    C("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"})
    raise AssertionError("stay 被阻断时不应签发")
except DomainError as e:
    assert e.code == "NO_ELIGIBLE_SCOPE", e.code
    assert e.details["suspended"][0]["component_id"] == "stay"

# 闭环事件后 A 签发 v1
C("close_severe_incident", {"split_id": "split-1", "incident_id": "inc-1", "resolution": "责任协议签订并公证"})
iss_a = C("issue_certification", {"split_id": "split-1", "successor_org_id": "org-venue"})
assert iss_a["version"] == 1 and iss_a["included_components"] == ["stay"], iss_a
cert_a_id = iss_a["cert_id"]
assert cert_a_id != cert_b_id  # 相互独立的新证书

# 新宣传引用必须钉版本；冻结原证书禁止新引用
try:
    C("register_promotion_reference", {"promotion_id": "promo-x", "cert_id": "cert-1", "version": 1})
    raise AssertionError("冻结证书不得用于宣传")
except DomainError as e:
    assert e.code == "CERT_FROZEN", e.code
try:
    C("register_promotion_reference", {"promotion_id": "promo-x", "cert_id": cert_b_id, "version": 99})
    raise AssertionError("不存在版本应拒绝")
except DomainError as e:
    assert e.code == "UNKNOWN_VERSION", e.code
C("register_promotion_reference", {"promotion_id": "promo-b", "cert_id": cert_b_id, "version": 1})

# B 内容变化 -> 确认失效 -> 复审 -> 整改后签发 v2（新版本、旧版 superseded）
C("amend_succession_declaration", {"split_id": "split-1", "successor_org_id": "org-online", "items": [
    {"component_id": "signing", "controls": ["电子签", "人脸识别双录"], "gaps": [], "staff_ids": [], "facility_ids": []},
    {"component_id": "health", "populations": ["self-care"], "locations": ["online"],
     "controls": ["远程问诊"], "gaps": [], "staff_ids": [], "facility_ids": []},
]})
try:
    C("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
    raise AssertionError("内容变化后应要求复审")
except DomainError as e:
    assert e.code == "CONFIRMATIONS_STALE", e.code
C("confirm_experience_coverage", {"split_id": "split-1", "successor_org_id": "org-online",
    "reviewer": "r1", "decisions": [
        {"component_id": "signing", "accepted_evidence_ids": ["ev-sign-1"], "resolved_gaps": []},
        {"component_id": "health", "accepted_evidence_ids": ["ev-health-1"], "resolved_gaps": []},
    ]})
C("confirm_responsibility_handover", {"split_id": "split-1", "successor_org_id": "org-online",
    "compliance_officer": "c1", "decisions": [
        {"component_id": "signing", "complaint_channel": "app", "liability_acknowledged": True},
        {"component_id": "health", "complaint_channel": "app", "liability_acknowledged": True},
    ]})
iss_b_v2 = C("issue_certification", {"split_id": "split-1", "successor_org_id": "org-online"})
assert iss_b_v2["version"] == 2, iss_b_v2
bview = Q.get_certificate(cert_b_id)
assert [v["status"] for v in bview["versions"]] == ["superseded", "active"], bview["versions"]
# v1 的宣传引用现在指向被取代版本
assert Q.get_promotion("promo-b")["validity"] == "superseded"

# 部分撤销：撤销 health（追加事实），signing 仍有效
rev = C("revoke_certification", {"cert_id": cert_b_id, "component_ids": ["health"], "reason": "健康支持投诉激增"})
assert rev["partially_revoked_components"] == ["health"], rev
try:
    C("register_promotion_reference", {"promotion_id": "promo-b2", "cert_id": cert_b_id,
                                       "version": 2, "component_ids": ["health"]})
    raise AssertionError("已撤销范围不得宣传")
except DomainError as e:
    assert e.code == "SCOPE_UNAVAILABLE", e.code

# 消费者：承诺保留 + 责任变更记录。u1 两条：A.stay 与 B.health（v2 不重复通知）
cu1 = Q.get_consumer("u1")
assert len(cu1["responsibility_changes"]) == 2, cu1
comps = {n["component_ids"][0] for n in cu1["responsibility_changes"]}
assert comps == {"stay", "health"}, comps
assert all(n["commitments_preserved"] for n in cu1["responsibility_changes"])
# 通知确认（送达）追加事实
nid = cu1["responsibility_changes"][0]["notice_id"]
C("acknowledge_notice", {"consumer_id": "u1", "notice_id": nid})
assert Q.get_consumer("u1")["responsibility_changes"][0]["delivered"] is True

# 从新证书 B 追溯原证据与未继承原因
lin = Q.lineage(cert_b_id)
assert lin["source_cert_id"] == "cert-1"
inherited = {(x["component_id"], tuple(sorted(x["inherited_evidence"], key=lambda e: e["evidence_id"]))[0]["evidence_id"])
             for x in lin["inheritance"]}
assert ("signing", "ev-sign-1") in inherited
assert ("health", "ev-health-1") in inherited
codes = {(x["component_id"], x["evidence_id"]): x["reason_code"] for x in lin["not_inherited_evidence"]}
assert codes.get(("health", "ev-health-2")) == "outside_applicability", codes
# v1 与 v2 各含 signing+health，继承记录共 4 条
assert len(lin["inheritance"]) == 4, len(lin["inheritance"])
# 消费者处置可追溯
con = {c["consumer_id"]: c for c in lin["consumers"]}
assert "u1" in con and con["u1"]["commitments_preserved"] is True

# 从原证书反向追两个独立新证书
src = Q.lineage("cert-1")
child_ids = {s["cert_id"] for s in src["successors"]}
assert child_ids == {cert_a_id, cert_b_id}, child_ids

# 全部事实为追加式
types = [f["type"] for f in svc.facts()]
for t in ["SplitOpened", "SuccessionDeclared", "CertificationIssued", "CertificationVersionAdded",
          "CertificationSuperseded", "CertificationScopePartiallyRevoked", "ResponsibilityChangeNoticed",
          "NoticeDelivered", "RectificationRecorded", "IncidentRegistered", "IncidentClosed"]:
    assert t in types, t

# 独立拆分验证共用人员阻断：同一 staff 同时服务两个承接主体，
# 只暂停涉及该人员的组件，无关组件照常签发
C("register_certificate", {
    "cert_id": "cert-2", "holder_org_id": "org-original",
    "valid_from": "2025-01-01", "valid_to": "2027-01-01",
    "components": [
        {"id": "c-stay", "name": "住宿", "populations": ["p1"], "locations": ["l1"],
         "responsible_party_org_id": "org-original"},
        {"id": "c-sign", "name": "签约", "populations": ["p1"], "locations": ["l1"],
         "responsible_party_org_id": "org-original"},
    ],
})
C("record_evidence", {"evidence_id": "ev2-1", "cert_id": "cert-2", "component_id": "c-stay",
                      "populations": ["p1"], "locations": ["l1"], "summary": "s"})
C("record_evidence", {"evidence_id": "ev2-2", "cert_id": "cert-2", "component_id": "c-sign",
                      "populations": ["p1"], "locations": ["l1"], "summary": "s"})
C("open_split", {"split_id": "split-2", "cert_id": "cert-2"})
C("declare_succession", {"split_id": "split-2", "successor_org_id": "org-venue", "items": [
    {"component_id": "c-stay", "controls": [], "gaps": [], "staff_ids": ["staff-wang"], "facility_ids": []}]})
C("declare_succession", {"split_id": "split-2", "successor_org_id": "org-online", "items": [
    {"component_id": "c-sign", "controls": [], "gaps": [], "staff_ids": ["staff-wang"], "facility_ids": []}]})
assert any(f["type"] == "BlockerDetected" and f["payload"]["kind"] == "shared_staff"
           and set(f["payload"]["component_ids"]) == {"c-stay", "c-sign"}
           for f in svc.facts()), [f for f in svc.facts() if f["type"] == "BlockerDetected"]
# 任一方即使双确认也无法签发（相关范围保持暂停，且不影响 split-1 的证书）
C("confirm_experience_coverage", {"split_id": "split-2", "successor_org_id": "org-venue",
    "reviewer": "r1", "decisions": [{"component_id": "c-stay", "accepted_evidence_ids": ["ev2-1"]}]})
C("confirm_responsibility_handover", {"split_id": "split-2", "successor_org_id": "org-venue",
    "compliance_officer": "c1",
    "decisions": [{"component_id": "c-stay", "complaint_channel": "x", "liability_acknowledged": True}]})
try:
    C("issue_certification", {"split_id": "split-2", "successor_org_id": "org-venue"})
    raise AssertionError("共用人员未解除前应暂停")
except DomainError as e:
    assert e.code == "NO_ELIGIBLE_SCOPE", e.code
# B 修订移除共用人员后阻断清除，A 可签发
C("amend_succession_declaration", {"split_id": "split-2", "successor_org_id": "org-online", "items": [
    {"component_id": "c-sign", "controls": [], "gaps": [], "staff_ids": [], "facility_ids": []}]})
assert any(f["type"] == "BlockerCleared" for f in svc.facts())
# A 的旧确认已在 BlockerDetected 前存在且未修订——阻断已清除，原确认仍有效，直接签发
iss2 = C("issue_certification", {"split_id": "split-2", "successor_org_id": "org-venue"})
assert iss2["included_components"] == ["c-stay"], iss2
# split-1 的两证书不受影响
assert Q.get_certificate(cert_a_id)["status"] == "active"

print("SMOKE OK:", cert_a_id, cert_b_id, "facts=", len(svc.facts()))
